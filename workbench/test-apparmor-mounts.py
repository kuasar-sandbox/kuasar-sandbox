#!/usr/bin/env python3
"""Real enforcing mount regressions; this does not qualify nested Docker or KVM."""
import argparse
import ctypes
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent


def child():
    # All mount/network changes occur in unshare's private namespaces. Preload
    # ctypes before pivot_root detaches the Python runtime's original root.
    libc = ctypes.CDLL(None, use_errno=True)
    def call(name, *arguments):
        if getattr(libc, name)(*arguments):
            raise OSError(ctypes.get_errno(), name + ' failed')
    context = Path('/proc/self/attr/current').read_text().strip()
    assert context.endswith(' (enforce)'), context
    subprocess.run(['mount', '-t', 'tmpfs', '-o', 'nosuid,nodev', 'tmpfs', '/tmp'], check=True)
    subprocess.run(['mount', '--bind', '/dev/null', '/sys/module/apparmor/parameters/enabled'], check=True)
    subprocess.run(['mount', '-o', 'remount,bind,ro', '/sys/module/apparmor/parameters/enabled'], check=True)
    assert Path('/sys/module/apparmor/parameters/enabled').read_text() == ''
    subprocess.run(['ip', 'netns', 'add', 'same-netns'], check=True)
    subprocess.run(['ip', '-n', 'same-netns', 'link', 'set', 'lo', 'up'], check=True)
    subprocess.run(['ip', 'netns', 'delete', 'same-netns'], check=True)
    assert Path('/sys/module/apparmor/parameters/enabled').read_text() == ''
    for filesystem in ('tmpfs', 'securityfs'):
        result = subprocess.run(['mount', '-t', filesystem, filesystem, '/mnt'], capture_output=True)
        assert result.returncode != 0, 'profile allowed unrelated ' + filesystem + ' mount'
    result = subprocess.run(['unshare', '--mount', '--fork', '--kill-child=KILL', '--propagation', 'private',
                             'mount', '-t', 'tmpfs', 'tmpfs', '/'], capture_output=True)
    assert result.returncode != 0, 'profile allowed an unrelated filesystem over root'
    # Current runc opens sysctls through a detached proc clone. AppArmor sees
    # /sys/net/... there, so test the actual fd-reopen operation in this private
    # network namespace, and ensure sensitive detached proc entries stay denied.
    proc = libc.syscall(428, -100, b'/proc', 1 | os.O_CLOEXEC)  # open_tree, CLONE
    assert proc >= 0, os.strerror(ctypes.get_errno())
    name = 'sys/net/ipv4/ip_unprivileged_port_start'
    value = Path('/proc/' + name).read_text()
    fd = os.open(name, os.O_PATH, dir_fd=proc)
    with open('/proc/self/fd/' + str(fd), 'w') as stream:
        stream.write(value)
    os.close(fd)
    for name, flags in [('sysrq-trigger', os.O_WRONLY), ('kcore', os.O_RDONLY)]:
        fd = os.open(name, os.O_PATH, dir_fd=proc)
        try:
            try:
                reopened = os.open('/proc/self/fd/' + str(fd), flags)
            except PermissionError:
                pass
            else:
                os.close(reopened)
                raise AssertionError('detached protected proc entry allowed: ' + name)
        finally:
            os.close(fd)
    os.close(proc)
    for name in ('asound', 'bus', 'fs', 'irq', 'sys', 'sysrq-trigger'):
        target = '/proc/' + name
        if not Path(target).exists():
            continue
        subprocess.run(['mount', '--rbind', target, target], check=True)
        subprocess.run(['mount', '-o', 'remount,bind,ro', target], check=True)
        forbidden = subprocess.run(['mount', '-o', 'remount,bind,rw', target], capture_output=True)
        assert forbidden.returncode != 0, 'profile allowed writable proc remount: ' + target
    # OCI's default masked paths use read-only tmpfs for directories and
    # /dev/null bind mounts for files. Prove only those exact destinations.
    if Path('/proc/acpi').is_dir():
        subprocess.run(['mount', '-t', 'tmpfs', '-o', 'ro', 'tmpfs', '/proc/acpi'], check=True)
        forbidden = subprocess.run(['mount', '-o', 'remount,rw', '/proc/acpi'], capture_output=True)
        assert forbidden.returncode != 0, 'profile allowed writable masked proc directory'
    subprocess.run(['mount', '--bind', '/dev/null', '/proc/kcore'], check=True)
    Path('/run/docker/netns').mkdir(parents=True, exist_ok=True)
    netns = Path('/run/docker/netns/workbench-test')
    netns.touch()
    subprocess.run(['mount', '--bind', '/proc/self/ns/net', str(netns)], check=True)
    subprocess.run(['umount', str(netns)], check=True)
    root = b'/var/lib/docker/pivot-test'
    os.mkdir(root)
    call('mount', b'tmpfs', root, b'tmpfs', 0, None)
    os.chdir(root)
    old = b'.pivot_root1234567890'
    os.mkdir(old)
    syscall = {'x86_64': 155, 'aarch64': 41}[os.uname().machine]
    call('syscall', syscall, b'.', old)
    os.chdir('/')
    call('mount', None, b'/' + old, None, ctypes.c_ulong((1 << 18) | (1 << 14)), None)
    call('umount2', b'/' + old, 2)
    print(json.dumps({'context': context, 'mask': 'empty', 'netns': 'create-configure-delete',
                      'forbidden_mounts': 'denied', 'pivot_old_root': 'private',
                      'detached_proc': 'private network sysctl allowed; sysrq/kcore denied'}))


def qualify(root):
    loader = importlib.machinery.SourceFileLoader('mount_test_launcher', str(ROOT / 'workbench'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    launcher = importlib.util.module_from_spec(spec)
    loader.exec_module(launcher)
    launcher.require(launcher.apparmor_enabled(), 'this test requires enforcing host AppArmor; no skip')
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    data = {'id': uuid.uuid4().hex, 'owner_uid': os.getuid()}
    source = None
    try:
        launcher.load_apparmor(root, data)
        source = launcher.apparmor_source(root, data)
        # Set up disposable backing directories before entering confinement.
        command = ['unshare', '--mount', '--net', '--fork', '--kill-child=KILL', '--propagation', 'private',
                   '/bin/sh', '-ceu', 'mount -t tmpfs tmpfs /run; mount -t tmpfs tmpfs /var/lib; '
                   'mkdir /var/lib/docker; exec aa-exec -p "$1" -- "$2" -B "$3" --child',
                   'mount-test', data['apparmor']['name'], sys.executable, str(Path(__file__).resolve())]
        if os.geteuid() != 0:
            command = ['sudo', '-n', *command]
        result = subprocess.run(command, text=True, capture_output=True, timeout=30)
        (root / 'stdout.log').write_text(result.stdout)
        (root / 'stderr.log').write_text(result.stderr)
        launcher.require(result.returncode == 0, 'enforcing mount probe failed; inspect retained logs')
        record = json.loads(result.stdout)
        launcher.require(record['context'] == data['apparmor']['name'] + ' (enforce)', 'unexpected profile')
        launcher.require(launcher.apparmor_enabled(), 'host AppArmor changed')
        (root / 'result.json').write_text(json.dumps(record | {'host_apparmor': 'Y'}, indent=2) + '\n')
    finally:
        if source is not None:
            # Only the joined child ever used this unique test profile. Verify
            # its recorded source before removing exactly that owned profile.
            launcher.apparmor_source(root, data)
            launcher.apparmor_parser('--remove', source)
            launcher.require(data['apparmor']['name'] not in launcher.apparmor_profiles(), 'owned profile leaked')
            data['apparmor']['loaded'] = False
            launcher.save(root, data)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child()
    else:
        parser.error('--root is required') if args.root is None else qualify(args.root.absolute())
