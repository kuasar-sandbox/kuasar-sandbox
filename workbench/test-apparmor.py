#!/usr/bin/env python3
"""Real enforcing Ubuntu mount/inner-Docker test, separate from KVM qualification."""
import argparse
import datetime
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
loader = importlib.machinery.SourceFileLoader('apparmor_launcher', str(ROOT / 'workbench'))
spec = importlib.util.spec_from_loader(loader.name, loader)
launcher = importlib.util.module_from_spec(spec)
loader.exec_module(launcher)
sys.path.insert(0, str(ROOT.parent / 'test/e2e/lib'))
import workspace


def qualify(image, root):
    root.mkdir(parents=True, exist_ok=False)
    require = launcher.require
    require(launcher.apparmor_enabled(), 'this qualification requires host AppArmor enabled; it cannot skip')
    profiles_before = launcher.apparmor_profiles()
    args = argparse.Namespace(root=root / 'instances', name='lsm-' + uuid.uuid4().hex[:12],
                              mode='system', inputs=root / 'inputs', source=None,
                              cpus=2, memory_gib=4, pids=2048)
    args.inputs.mkdir()
    checked = launcher.host_check(image, 'build')
    checked['mode'] = 'system'
    result = {'qualification': 'enforcing-AppArmor-mounts-and-private-Docker', 'kvm_tested': False,
              'image_id': checked['image_id'], 'conclusion': 'failure', 'host_apparmor_before': 'Y'}
    started = time.monotonic()
    since = datetime.datetime.now(datetime.timezone.utc).isoformat()
    state = data = None
    try:
        # Acquire this small test input with the host's configured Docker
        # transport. This is a tools/LSM check, not release image selection.
        request = workspace.external_image_requests(['sandbox.lifecycle.sh'], checked['arch'])['busybox']
        launcher.docker('pull', '--platform', request['platform'], request['reference'], timeout=180)
        archive = args.inputs / 'inner.tar'
        launcher.docker('save', '-o', archive, request['reference'], timeout=120)
        verified = workspace.verify_image_archive(archive, request['platform'])
        result['inner_image'] = {'image_id': verified['image_id'], 'sha256': workspace.digest(archive)}
        with launcher.locked(args, create=True) as state:
            data = checked | {'id': uuid.uuid4().hex, 'owner_uid': os.getuid(), 'owner_gid': os.getgid(),
                              'directory': str(state), 'container_name': 'kuasar-workbench-' + args.name, 'status': 'creating'}
            launcher.save(state, data)
            launcher.load_apparmor(state, data)
            result['profile'] = dict(data['apparmor'])
            command = launcher.create_command(args, state, data)
            # Intentionally test only the LSM/mount/daemon part on hosted
            # machines without KVM. Public start still requires every device
            # and runs the full check-system.py; no public skip flag is added.
            for device in ('/dev/kvm', '/dev/net/tun'):
                index = command.index(device)
                require(command[index - 1] == '--device', 'unexpected device option')
                del command[index - 1:index + 1]
            data['container_id'] = launcher.docker(*command)
            launcher.save(state, data)
            launcher.docker('start', data['container_id'])
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            container = launcher.owned_container(data)
            require(container and container['State']['Running'], 'confined system startup exited')
            ready = subprocess.run(['docker', 'exec', data['container_id'], 'systemctl', 'is-active', 'docker', 'containerd'],
                                   capture_output=True, timeout=10)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise TimeoutError('confined private daemons did not start')
        launcher.check_apparmor(data)
        def execute(*command):
            return launcher.docker('exec', data['container_id'], *command, timeout=60)
        require(execute('stat', '-fc', '%T', '/sys/fs/cgroup') == 'cgroup2fs', 'missing private cgroup mount')
        require(execute('findmnt', '-n', '-o', 'FSTYPE', '--target', '/sys/fs/bpf') == 'bpf', 'missing bpffs mount')
        require(execute('docker', 'info', '--format', '{{.Driver}} {{.DockerRootDir}}') == 'overlay2 /var/lib/docker',
                'private daemon storage differs')
        execute('ip', 'netns', 'add', 'same-netns')
        execute('ip', '-n', 'same-netns', 'link', 'set', 'lo', 'up')
        execute('ip', 'netns', 'delete', 'same-netns')
        result['network_namespace'] = 'create-configure-delete'
        result['outer_context'] = execute('cat', '/proc/1/attr/current')
        result['inner_detection'] = execute('cat', str(launcher.APPARMOR_ENABLED))
        # Startup unit denials can exhaust the kernel audit burst. A bounded
        # quiet interval preserves the next nested-runtime denial without
        # changing the host audit rate or any enforcement setting.
        time.sleep(6)
        execute('docker', 'load', '-i', '/inputs/release/inner.tar')
        execute('docker', 'run', '-d', '--name', 'same-inner', '--network=none', verified['image_id'], 'sleep', '300')
        inner_pid = execute('docker', 'inspect', 'same-inner', '--format', '{{.State.Pid}}')
        require(inner_pid.isdecimal() and int(inner_pid) > 1, 'invalid inner PID')
        expected = data['apparmor']['name'] + ' (enforce)'
        require(execute('cat', '/proc/' + inner_pid + '/attr/current') == expected, 'inner PID escaped outer AppArmor profile')
        require(execute('docker', 'exec', 'same-inner', 'cat', '/proc/self/attr/current') == expected, 'inner exec changed confinement')
        outer_pid = launcher.owned_container(data)['State']['Pid']
        require(launcher.host_command(['cat', f'/proc/{outer_pid}/attr/current']).strip() == expected,
                'host does not see the outer PID confined')
        # Both mounts are valid with SYS_ADMIN and this seccomp policy; their
        # denial exercises the AppArmor destination/filesystem restrictions.
        execute('mkdir', '-p', '/work/forbidden-mount')
        for filesystem in ('tmpfs', 'securityfs'):
            attempt = subprocess.run(['docker', 'exec', data['container_id'], 'mount', '-t', filesystem,
                                      filesystem, '/work/forbidden-mount'], capture_output=True, text=True, timeout=10)
            if attempt.returncode == 0:
                execute('umount', '/work/forbidden-mount')
                raise AssertionError(f'AppArmor allowed forbidden {filesystem} mount')
        require(launcher.apparmor_enabled(), 'host AppArmor was disabled')
        result.update(conclusion='success', profile=data['apparmor'], outer_pid=outer_pid,
                      inner_pid_in_outer_namespace=int(inner_pid), outer_context=expected, inner_context=expected,
                      host_apparmor_after='Y', inner_detection=execute('cat', str(launcher.APPARMOR_ENABLED)),
                      forbidden_mounts='denied')
    except BaseException as error:
        result['error'] = str(error)
        raise
    finally:
        if data is not None:
            launcher.diagnostics(state, data)
            # Keep only this instance's audit events, never unrelated host logs.
            # systemd can exit before Docker has any stdout/stderr to collect.
            for name, command in (
                ('apparmor-audit.log', ['journalctl', '-k', '--since', since, '--no-pager', '-n', '2000']),
                ('system-journal.log', ['journalctl', '--directory', str(state / 'journal'), '--no-pager', '-n', '500']),
            ):
                try:
                    text = launcher.host_command(command)
                    if name == 'apparmor-audit.log':
                        profile = data.get('apparmor', {}).get('name', '')
                        text = '\n'.join(line for line in text.splitlines() if profile and profile in line) + '\n'
                    (state / 'output' / name).write_text(text)
                except Exception as error:
                    result.setdefault('diagnostic_errors', []).append(f'{name}: {error}')
            try:
                subprocess.run([sys.executable, '-B', ROOT / 'workbench', '--root', args.root,
                                '--name', args.name, 'cleanup'], check=True, timeout=420)
                if data.get('apparmor'):
                    require(data['apparmor']['name'] not in launcher.apparmor_profiles(), 'owned profile leaked')
                result['cleanup'] = 'owned container, daemon data and profile removed; output retained'
            except Exception as error:
                result.update(conclusion='failure', cleanup_error=str(error))
        require(launcher.apparmor_profiles().get('docker-default') == profiles_before.get('docker-default'),
                'host docker-default state changed')
        result['wall_seconds'] = time.monotonic() - started
        (root / 'result.json').write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    require(result['conclusion'] == 'success', 'confined qualification failed; inspect retained evidence')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(qualify(args.image, args.root.absolute()), sort_keys=True))
