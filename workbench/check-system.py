#!/usr/bin/env python3
"""Check private system services and report capabilities without requiring KVM to boot."""
import argparse
import ctypes
import fcntl
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import tomllib
import uuid


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def output(*command):
    return subprocess.check_output(command, text=True, timeout=30).strip()


def check(host_namespaces):
    require(set(host_namespaces) == {'pid', 'uts', 'ipc', 'mnt', 'net', 'cgroup'}, 'all six host namespace identities are required')
    arch = platform.machine()
    require(arch in ('x86_64', 'aarch64'), f'unsupported native architecture: {arch}')
    require(Path('/proc/1/comm').read_text().strip() == 'systemd', 'systemd must be PID 1')
    namespaces = {kind: os.readlink('/proc/self/ns/' + kind) for kind in host_namespaces}
    require(all(namespaces[kind] != host_namespaces[kind] for kind in namespaces), 'workbench shares a required host namespace')
    require(output('stat', '-fc', '%T', '/sys/fs/cgroup') == 'cgroup2fs', 'cgroup v2 is required')
    controllers = set(Path('/sys/fs/cgroup/cgroup.controllers').read_text().split())
    require({'cpu', 'memory', 'pids'} <= controllers, 'delegate cpu/memory/pids cgroup controllers for the requested budgets')
    libc = ctypes.CDLL(None, use_errno=True)
    def syscall(number, *args):
        result = libc.syscall(ctypes.c_long(number), *args)
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return result

    def probe_kvm():
        kvm = os.open('/dev/kvm', os.O_RDWR | os.O_CLOEXEC)
        try:
            require(fcntl.ioctl(kvm, 0xAE00) == 12, 'KVM_GET_API_VERSION must return 12')
        finally:
            os.close(kvm)

    def probe_tun():
        tun = os.open('/dev/net/tun', os.O_RDWR | os.O_CLOEXEC)
        try:
            fcntl.ioctl(tun, 0x400454CA, struct.pack('16sH', ('wb' + uuid.uuid4().hex[:10]).encode(), 0x0002 | 0x1000))
        finally:
            os.close(tun)

    def probe_uffd():
        uffd = syscall(323 if arch == 'x86_64' else 282, os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            api = bytearray(struct.pack('QQQ', 0xAA, 0, 0))
            fcntl.ioctl(uffd, 0xC018AA3F, api, True)
        finally:
            os.close(uffd)

    def probe_bpf():
        require(output('findmnt', '-n', '-o', 'FSTYPE', '--target', '/sys/fs/bpf') == 'bpf', 'private bpffs is unavailable')
        bpf_number = 321 if arch == 'x86_64' else 280
        attr = ctypes.create_string_buffer(struct.pack('IIII', 2, 4, 8, 1) + bytes(128))
        bpf = syscall(bpf_number, 0, ctypes.byref(attr), len(attr))
        pin = Path('/sys/fs/bpf/workbench-check-' + uuid.uuid4().hex)
        try:
            name = ctypes.create_string_buffer(os.fsencode(pin))
            attr = ctypes.create_string_buffer(struct.pack('QII', ctypes.addressof(name), bpf, 0))
            syscall(bpf_number, 6, ctypes.byref(attr), 16)
            require(pin.exists(), 'BPF_OBJ_PIN failed to create the private pin')
        finally:
            pin.unlink(missing_ok=True)
            os.close(bpf)

    capabilities, errors = {}, {}
    for name, operation, expected in (
            ('kvm', probe_kvm, 'api-12'), ('tun', probe_tun, 'create-close'),
            ('uffd', probe_uffd, 'api-ioctl'), ('bpf', probe_bpf, 'create-pin-remove')):
        try:
            operation()
        except (OSError, RuntimeError) as error:
            capabilities[name] = 'unavailable'
            errors[name] = str(error)
        else:
            capabilities[name] = expected

    info = json.loads(output('docker', 'info', '--format', '{{json .}}'))
    require(info['Driver'] == 'overlay2' and info['DockerRootDir'] == '/var/lib/docker',
            'private Docker must use overlay2 at /var/lib/docker; no storage fallback is accepted')
    require(str(info['CgroupVersion']) == '2', 'inner Docker must use cgroup v2')
    config = tomllib.loads(output('containerd', 'config', 'dump'))
    require(config['root'] == '/var/lib/containerd' and config['state'] == '/run/containerd',
            'containerd needs its private root and state paths independently of Docker data-root')
    for service in ('containerd', 'docker'):
        require(output('systemctl', 'is-active', service) == 'active', f'private {service} service is not active')
    # Exercise delegation in a task-owned transient unit, without host cgroups.
    program = '''import os
from pathlib import Path
group=Path('/sys/fs/cgroup') / Path('/proc/self/cgroup').read_text().strip().split('::')[1].lstrip('/')
child=group/'probe'
child.mkdir()
(child/'cgroup.procs').write_text(str(os.getpid()))
(group/'cgroup.subtree_control').write_text('+cpu +memory +pids')
assert {'cpu','memory','pids'} <= set((child/'cgroup.controllers').read_text().split())
'''
    subprocess.run(['systemd-run', '--quiet', '--wait', '--pipe', '--collect',
                    '--unit=workbench-check-' + uuid.uuid4().hex, '--property=Delegate=yes',
                    'python3', '-B', '-c', program], check=True, timeout=30)
    plugins = [line.split() for line in output('ctr', 'plugins', 'ls').splitlines()[1:]]
    require(not any(row[0] == 'io.containerd.cri.v1' or row[:2] == ['io.containerd.grpc.v1', 'cri'] for row in plugins),
            'private containerd must not start CRI services')
    return {'arch': arch, 'page_size': os.sysconf('SC_PAGE_SIZE'), 'namespaces': namespaces,
            'machine_id': Path('/etc/machine-id').read_text().strip(), 'controllers': sorted(controllers),
            **capabilities, 'capability_errors': errors,
            'docker': {'version': info['ServerVersion'], 'driver': info['Driver'], 'root': info['DockerRootDir']},
            'containerd': {'root': config['root'], 'state': config['state']}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-namespaces', required=True, type=json.loads)
    args = parser.parse_args()
    print(json.dumps(check(args.host_namespaces), sort_keys=True))
