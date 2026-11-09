"""Bounded startup checks in the actual E2E execution environment.

Only selected requirements are checked. Each temporary kernel object belongs
to this invocation and is released on failure as well as success. These checks
diagnose environment failures; the product cases still determine acceptance.
"""
from contextlib import contextmanager
import ctypes
import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import stat
import subprocess
import tempfile
import uuid

import capabilities

OPERATIONS = {
    'kvm': 'api-12-create-vm', 'tun': 'create-close', 'userfaultfd': 'api-ioctl',
    'bpf-map': 'create-pin-remove', 'bpf-tc': 'sched-cls-load-attach-remove',
    'netns': 'create-veth-remove', 'geneve': 'create-link-remove',
    'systemd': 'manager-connected', 'journald': 'journal-socket-sync',
    'cgroup-v2': 'create-writable-child', 'docker': 'daemon-connected',
    'podman': 'rootful-info-cgroup-v2', 'quadlet': 'generate-private-delegated-service',
}


def run(*command):
    return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT, timeout=30).strip()


def syscall(number, *arguments):
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(ctypes.c_long(number), *arguments)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def bpf(command, data):
    value = ctypes.create_string_buffer(data)
    return syscall(321 if platform.machine() == 'x86_64' else 280,
                   command, ctypes.byref(value), len(data))


@contextmanager
def pinned(fd):
    # No reliance on a host-global bpffs mount, and no mount over existing data.
    try:
        with tempfile.TemporaryDirectory(prefix='e2e-bpf-') as directory:
            run('mount', '-t', 'bpf', 'bpf', directory)
            try:
                path = Path(directory) / 'object'
                name = ctypes.create_string_buffer(os.fsencode(path))
                bpf(6, struct.pack('QII', ctypes.addressof(name), fd, 0))  # BPF_OBJ_PIN
                try:
                    yield str(path)
                finally:
                    path.unlink(missing_ok=True)
            finally:
                run('umount', directory)
    finally:
        os.close(fd)


@contextmanager
def network():
    name = 'ec-' + uuid.uuid4().hex[:12]
    run('ip', 'netns', 'add', name)
    try:
        yield lambda *args: run('ip', 'netns', 'exec', name, *args)
    finally:
        run('ip', 'netns', 'delete', name)


def check_one(capability):
    if capability == 'kvm':
        fd = os.open('/dev/kvm', os.O_RDWR | os.O_CLOEXEC)
        try:
            capabilities.require(fcntl.ioctl(fd, 0xAE00) == 12, 'unexpected KVM API')
            vm = fcntl.ioctl(fd, 0xAE01, 0)  # KVM_CREATE_VM; no guest code runs.
            os.close(vm)
        finally:
            os.close(fd)
    elif capability == 'tun':
        fd = os.open('/dev/net/tun', os.O_RDWR | os.O_CLOEXEC)
        try:
            fcntl.ioctl(fd, 0x400454CA, struct.pack('16sH', ('ec' + uuid.uuid4().hex[:10]).encode(), 0x1002))
        finally:
            os.close(fd)
    elif capability == 'userfaultfd':
        fd = syscall(323 if platform.machine() == 'x86_64' else 282, os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            api = bytearray(struct.pack('QQQ', 0xAA, 0, 0))
            fcntl.ioctl(fd, 0xC018AA3F, api, True)
        finally:
            os.close(fd)
    elif capability == 'bpf-map':
        with pinned(bpf(0, struct.pack('IIII', 2, 4, 8, 1) + bytes(128))):
            pass
    elif capability == 'bpf-tc':
        # Fixed SCHED_CLS program: r0 = TC_ACT_OK; exit. No compiler or product
        # source is needed in a runtime-only container.
        code = ctypes.create_string_buffer(bytes.fromhex('b7000000000000009500000000000000'))
        license = ctypes.create_string_buffer(b'GPL\0')
        fd = bpf(5, struct.pack('IIQQ', 3, 2, ctypes.addressof(code), ctypes.addressof(license)) + bytes(120))
        with pinned(fd) as path, network() as execute:
            execute('ip', 'link', 'add', 'probe0', 'type', 'dummy')
            execute('tc', 'qdisc', 'add', 'dev', 'probe0', 'clsact')
            execute('tc', 'filter', 'add', 'dev', 'probe0', 'ingress', 'bpf', 'direct-action', 'object-pinned', path)
    elif capability in {'netns', 'geneve'}:
        with network() as execute:
            if capability == 'netns':
                execute('ip', 'link', 'add', 'probe0', 'type', 'veth', 'peer', 'name', 'probe1')
            else:
                execute('ip', 'link', 'add', 'probe0', 'type', 'geneve', 'id', '1', 'remote', '192.0.2.1', 'dstport', '6081')
    elif capability == 'systemd':
        capabilities.require(Path('/proc/1/comm').read_text().strip() == 'systemd', 'systemd must be PID 1')
        run('systemctl', 'show-environment')
    elif capability == 'journald':
        capabilities.require(stat.S_ISSOCK(Path('/run/systemd/journal/socket').stat().st_mode), 'journal socket is missing')
        run('journalctl', '--sync')
    elif capability == 'cgroup-v2':
        root = Path('/sys/fs/cgroup')
        capabilities.require({'cpu', 'memory', 'pids'} <= set((root / 'cgroup.controllers').read_text().split()),
                             'required cgroup v2 controllers are missing')
        child = root / ('e2e-check-' + uuid.uuid4().hex)
        child.mkdir()
        try:
            (child / 'memory.max').write_text('67108864')
        finally:
            child.rmdir()
    elif capability == 'docker':
        run('docker', 'info', '--format', '{{.OSType}}')
    elif capability == 'podman':
        info = json.loads(run('podman', 'info', '--format', 'json'))
        host = info.get('host', {})
        capabilities.require(os.geteuid() == 0 and not host.get('security', {}).get('rootless', True),
                             'rootful Podman is required')
        capabilities.require(host.get('cgroupVersion') == 'v2' and host.get('os') == 'linux',
                             'Podman must use Linux cgroup v2')
    elif capability == 'quadlet':
        candidates = [shutil.which('podman-system-generator'),
                      '/usr/lib/systemd/system-generators/podman-system-generator',
                      '/usr/libexec/podman/quadlet']
        generator = next((p for p in candidates if p and os.access(p, os.X_OK)), None)
        capabilities.require(generator is not None, 'Quadlet generator is not installed')
        with tempfile.TemporaryDirectory(prefix='e2e-quadlet-') as directory:
            root = Path(directory)
            units, output = root / 'units', root / 'output'
            units.mkdir()
            output.mkdir()
            (units / 'e2e-probe.container').write_text(
                '[Container]\nImage=localhost/node-infra-probe:local\nPull=never\n'
                'Network=ns:/run/netns/node-infra-probe\nPodmanArgs=--systemd=always --cgroupns=private\n')
            subprocess.check_output([generator, str(output)], env={**os.environ, 'QUADLET_UNIT_DIRS': str(units)},
                                    text=True, stderr=subprocess.STDOUT, timeout=30)
            service = (output / 'e2e-probe.service').read_text()
            capabilities.require('Delegate=yes' in service and '--cgroups=split' in service,
                                 'Quadlet did not generate cgroup delegation/split')
    else:
        raise ValueError('unknown required capability: ' + capability)


def check(required):
    capabilities.require(platform.machine() in ('x86_64', 'aarch64'), 'unsupported capability-check architecture')
    evidence = {}
    for capability in capabilities.requirements(required):
        try:
            check_one(capability)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise RuntimeError('required E2E capability unavailable: ' + capability + ': ' + str(error)) from error
        evidence[capability] = OPERATIONS[capability]
    return evidence
