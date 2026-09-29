"""Collector identity and launcher ownership contracts; real system tests are separate."""
import argparse
from contextlib import nullcontext, redirect_stdout
import errno
import io
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent


def module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


collector = module('workbench_collector', ROOT / 'collect-inputs.py')
launcher = module('workbench_launcher', ROOT / 'workbench')
fixtures = module('offline_test_images', ROOT.parent / 'test/e2e/test_offline_inputs.py')
diagnostics = module('workbench_diagnostics', ROOT / 'collect-diagnostics.py')


class DiagnosticTests(unittest.TestCase):
    def test_root_only_outputs_are_bounded_regular_files_and_links_are_ignored(self):
        import tarfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'instances/owned/output'
            output.mkdir(parents=True)
            (output / 'result.log').write_bytes(b'0123456789')
            (output / 'result.log').chmod(0o600)
            (root / 'secret').write_text('unrelated')
            (output / 'link').symlink_to(root / 'secret')
            (output / 'linked-directory').symlink_to(root, target_is_directory=True)
            os.mkfifo(output / 'pipe')
            stream = io.BytesIO()
            diagnostics.collect(root, stream, per_file=4, total=8)
            stream.seek(0)
            with tarfile.open(fileobj=stream) as archive:
                self.assertEqual(set(archive.getnames()), {'instances/owned/output/result.log', 'diagnostics-index.json'})
                self.assertEqual(archive.extractfile('instances/owned/output/result.log').read(), b'6789')
                index = json.load(archive.extractfile('diagnostics-index.json'))
                self.assertEqual(index['instances/owned/output/result.log'], {'original_size': 10, 'retained_tail_size': 4})
                self.assertTrue(all(item.isfile() and item.mode == 0o644 for item in archive.getmembers()))


class CollectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixtures = self.root / 'fixtures'
        self.fixtures.mkdir()
        self.output = self.root / 'collected'
        self.images = {}
        for arch, go_arch in [('x86_64', 'amd64'), ('aarch64', 'arm64')]:
            record = fixtures.image_fixture(self.fixtures, label=arch, arch=go_arch)
            self.images[arch] = record
        self.index = json.dumps({'schemaVersion': 2, 'manifests': [
            {'platform': {'os': 'linux', 'architecture': record['platform'].split('/')[1]},
             'digest': record['manifest_digest'], 'size': len(record['manifest'].encode())}
            for record in self.images.values()]}).encode()

    def inspect(self, command, **kwargs):
        reference = command[-1]
        if reference == 'docker://docker.io/library/python:3.12-slim':
            return self.index
        return next(record['manifest'].encode() for record in self.images.values()
                    if reference.endswith('@' + record['manifest_digest']))

    def copy(self, command, **kwargs):
        record = next(record for record in self.images.values() if command[-2].endswith('@' + record['manifest_digest']))
        path = command[-1].removeprefix('docker-archive:').split(':', 1)[0]
        self.assertIn(':workbench-' + record['image_id'].split(':')[1], command[-1])
        shutil.copyfile(self.fixtures / record['archive'], path)
        return subprocess.CompletedProcess(command, 0)

    def test_one_tag_resolution_supplies_both_architectures_and_reuse_is_offline(self):
        with patch.object(collector.subprocess, 'check_output', side_effect=self.inspect) as inspect, patch.object(
                collector.subprocess, 'run', side_effect=self.copy):
            collector.collect(['basic.demo.sh'], ['x86_64', 'aarch64'], self.output)
        self.assertEqual(sum(call.args[0][-1].endswith('python:3.12-slim') for call in inspect.call_args_list), 1)
        records = json.loads((self.output / 'images.json').read_text())
        self.assertEqual({record['platform'] for record in records}, {'linux/amd64', 'linux/arm64'})
        self.assertEqual(len({record['registry_digest'] for record in records}), 1)
        for record in records:
            self.assertEqual((self.output / record['archive']).stat().st_mode & 0o777, 0o444)
        with patch.object(collector.subprocess, 'run') as copy, patch.object(collector.subprocess, 'check_output') as inspect:
            collector.collect(['basic.demo.sh'], ['x86_64', 'aarch64'], self.output)
            copy.assert_not_called()
            inspect.assert_not_called()

    def test_retry_keeps_original_tag_resolution(self):
        with patch.object(collector.subprocess, 'check_output', side_effect=self.inspect), patch.object(
                collector.subprocess, 'run', side_effect=subprocess.CalledProcessError(23, ['skopeo'])):
            with self.assertRaises(subprocess.CalledProcessError):
                collector.collect(['basic.demo.sh'], ['x86_64'], self.output)
        self.assertFalse((self.output / 'images.json').exists())
        self.assertTrue((self.output / '.images.pending.json').is_file())
        with patch.object(collector.subprocess, 'check_output') as inspect, patch.object(
                collector.subprocess, 'run', side_effect=self.copy):
            collector.collect(['basic.demo.sh'], ['x86_64'], self.output)
            inspect.assert_not_called()

    def test_corrupt_existing_input_is_never_downloaded_again(self):
        with patch.object(collector.subprocess, 'check_output', side_effect=self.inspect), patch.object(
                collector.subprocess, 'run', side_effect=self.copy):
            collector.collect(['basic.demo.sh'], ['x86_64'], self.output)
        archive = self.output / 'images/python-x86_64.tar'
        archive.chmod(0o644)
        archive.write_bytes(b'corrupt')
        with patch.object(collector.subprocess, 'check_output') as inspect, patch.object(collector.subprocess, 'run') as copy:
            with self.assertRaisesRegex(ValueError, 'sha256 mismatch'):
                collector.collect(['basic.demo.sh'], ['x86_64'], self.output)
            inspect.assert_not_called()
            copy.assert_not_called()

    def test_interrupted_collection_rejects_unsafe_archive_paths_before_copy(self):
        with patch.object(collector.subprocess, 'check_output', side_effect=self.inspect), patch.object(
                collector.subprocess, 'run', side_effect=subprocess.CalledProcessError(23, ['skopeo'])):
            with self.assertRaises(subprocess.CalledProcessError):
                collector.collect(['basic.demo.sh'], ['x86_64'], self.output)
        images = self.output / 'images'
        archive = images / 'python-x86_64.tar'
        archive.symlink_to(self.root / 'absent')
        with patch.object(collector.subprocess, 'run') as copy:
            with self.assertRaisesRegex(ValueError, 'symbolic link'):
                collector.collect(['basic.demo.sh'], ['x86_64'], self.output)
            copy.assert_not_called()
        archive.unlink()
        images.rmdir()
        images.symlink_to(self.fixtures, target_is_directory=True)
        with patch.object(collector.subprocess, 'run') as copy:
            with self.assertRaisesRegex(ValueError, 'unsafe image archive directory'):
                collector.collect(['basic.demo.sh'], ['x86_64'], self.output)
            copy.assert_not_called()

    def test_resolution_request_digest_is_not_a_self_claim(self):
        request = [('x86_64', 'python', {'reference': 'python@sha256:' + 'f' * 64, 'platform': 'linux/amd64'})]
        with patch.object(collector.subprocess, 'check_output', return_value=self.index):
            with self.assertRaisesRegex(ValueError, 'differs from requested digest'):
                collector.resolve(request, {})

    def test_tag_and_digest_transport_preserves_verified_request(self):
        identity = collector.workspace.sha256_bytes(self.index)
        reference = 'prom/prometheus:v3.5.0@' + identity
        requests = [(arch, 'prometheus', {'reference': reference, 'platform': record['platform']})
                    for arch, record in self.images.items()]

        def inspect(command, **kwargs):
            if command[-1] == 'docker://docker.io/prom/prometheus@' + identity:
                return self.index
            return self.inspect(command, **kwargs)

        with patch.object(collector.subprocess, 'check_output', side_effect=inspect) as calls:
            records = collector.resolve(requests, {})
        self.assertEqual(len(calls.call_args_list), 3)
        for record in records:
            self.assertEqual(record['reference'], reference)
            self.assertEqual(record['registry_digest'], identity)
        for call in calls.call_args_list:
            self.assertNotIn(':v3.5.0', call.args[0][-1])
        with patch.object(collector.subprocess, 'check_output', return_value=self.index + b' '):
            with self.assertRaisesRegex(ValueError, 'differs from requested digest'):
                collector.resolve(requests, {})

    def test_reference_normalization_preserves_ports_tags_and_digests(self):
        digest = '@sha256:' + 'a' * 64
        for reference, transport, repository in [
            ('python:3.12-slim', 'docker.io/library/python:3.12-slim', 'docker.io/library/python'),
            ('busybox' + digest, 'docker.io/library/busybox' + digest, 'docker.io/library/busybox'),
            ('localhost:5000/team/image:v1' + digest, 'localhost:5000/team/image' + digest,
             'localhost:5000/team/image'),
            ('ghcr.io/team/image:v1', 'ghcr.io/team/image:v1', 'ghcr.io/team/image'),
        ]:
            with self.subTest(reference=reference):
                self.assertEqual(collector.registry_reference(reference), (transport, repository))

    def test_cases_without_external_images_need_no_registry(self):
        with patch.object(collector.subprocess, 'check_output') as inspect, patch.object(collector.subprocess, 'run') as copy:
            collector.collect(['storage.cache.sh'], ['x86_64', 'aarch64'], self.output)
            inspect.assert_not_called()
            copy.assert_not_called()
        self.assertEqual(json.loads((self.output / 'images.json').read_text()), [])


class LauncherTests(unittest.TestCase):
    def test_startup_diagnostics_retain_exit_state_without_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            container = {'Id': 'owned-id', 'State': {'Running': False, 'ExitCode': 1,
                         'OOMKilled': False, 'Error': ''}, 'AppArmorProfile': 'owned-profile'}
            with patch.object(launcher, 'owned_container', return_value=container), patch.object(
                    launcher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'', b'')):
                launcher.diagnostics(state, {})
            records = list((state / 'output').glob('container-*.json'))
            self.assertEqual(len(records), 1)
            evidence = json.loads(records[0].read_text())
            self.assertEqual(evidence['state']['ExitCode'], 1)
            self.assertEqual(evidence['apparmor'], 'owned-profile')
            self.assertEqual(len(list((state / 'output').glob('startup-*.log'))), 1)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.source = self.root / 'source'
        self.source.mkdir()
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        self.data = {'id': 'a' * 32, 'image_id': 'sha256:' + 'b' * 64, 'container_name': 'test-workbench',
                     'owner_uid': os.getuid(), 'directory': str(self.state)}

    def args(self, **values):
        return argparse.Namespace(**({'cpus': 1, 'memory_gib': 2, 'pids': 256, 'mode': 'system',
                                      'inputs': self.inputs, 'source': None} | values))

    def command(self, **values):
        usage = shutil.disk_usage(self.root)._replace(free=10 * 1024**3)
        with patch.object(launcher.shutil, 'disk_usage', return_value=usage):
            return launcher.create_command(self.args(**values), self.state, self.data)

    def test_system_has_private_inputs_daemons_and_narrow_privileges(self):
        command = self.command()
        self.assertIn('--cgroupns=private', command)
        self.assertIn('nofile=1048576:1048576', command)
        self.assertEqual({arg for arg in command if arg.startswith('--cap-add=')},
                         {'--cap-add=SYS_ADMIN', '--cap-add=NET_ADMIN', '--cap-add=SYS_PTRACE'})
        text = ' '.join(command)
        for forbidden in ('--privileged', 'seccomp=unconfined', 'apparmor=unconfined', '/var/run/docker.sock',
                          'src=/sys/fs/cgroup', '--network=host', '--pid=host', '--ipc=host'):
            self.assertNotIn(forbidden, text)
        self.assertIn(f'type=bind,src={self.inputs},dst=/inputs/release,readonly', command)
        self.assertIn(f'type=bind,src={self.state}/docker,dst=/var/lib/docker', command)
        self.assertIn(f'type=bind,src={self.state}/containerd,dst=/var/lib/containerd', command)
        self.assertEqual((self.state / 'machine-id').read_text().strip(), self.data['id'])

    def test_ordinary_uid_build_drops_capabilities_and_needs_no_devices(self):
        command = self.command(mode='build', source=self.source, inputs=None)
        self.assertIn('--cap-drop=ALL', command)
        self.assertIn('--init', command)
        self.assertFalse(any(arg.startswith('--cap-add') for arg in command))
        self.assertNotIn('--device', command)
        self.assertNotIn('--ulimit', command)
        self.assertIn(f'{os.getuid()}:{os.getgid()}', command)
        self.assertIn(f'type=bind,src={self.source},dst=/src', command)
        self.assertEqual(command[-3:], ['build', 'sleep', 'infinity'])

    def test_seccomp_keeps_default_deny_and_argument_restrictions(self):
        base = json.loads((ROOT / 'seccomp-default.json').read_text())
        result = launcher.seccomp_profile()
        self.assertEqual(result['defaultAction'], 'SCMP_ACT_ERRNO')
        self.assertEqual(result['syscalls'][:len(base['syscalls'])], base['syscalls'])
        added = result['syscalls'][len(base['syscalls']):]
        self.assertEqual([row['args'][0]['value'] for row in added if row['names'] == ['keyctl']], [1, 5, 6])
        self.assertTrue(all(row['includes']['caps'] for row in added if row['names'] != ['keyctl']))

    def test_foreign_container_or_image_cannot_be_stopped(self):
        self.data['container_id'] = 'c' * 64
        container = {'Id': self.data['container_id'], 'Image': self.data['image_id'],
                     'Config': {'Labels': {launcher.LABEL: self.data['id'],
                                          'org.kuasar.workbench.uid': str(os.getuid())}}}
        with patch.object(launcher, 'inspect', return_value=container):
            self.assertEqual(launcher.owned_container(self.data), container)
            container['Config']['Labels'][launcher.LABEL] = 'foreign'
            with self.assertRaisesRegex(ValueError, 'foreign container'):
                launcher.owned_container(self.data)

    def test_symlink_state_and_mismatched_ownership_records_fail(self):
        state = self.root / 'owned'
        state.mkdir()
        launcher.save(state, self.data)
        with self.assertRaisesRegex(ValueError, 'foreign instance'):
            launcher.record(state)
        link = self.root / 'link'
        link.symlink_to(state)
        with self.assertRaisesRegex(ValueError, 'cannot be a symlink'):
            with launcher.locked(argparse.Namespace(root=link, name='x'), create=True):
                self.fail('unsafe lock opened')


    def test_cleanup_unreadable_daemon_directory_uses_bounded_helper(self):
        directory = self.state / 'docker'
        directory.mkdir()
        original = Path.iterdir
        def entries(path):
            if path == directory:
                raise PermissionError('root-owned daemon data')
            return original(path)
        with patch.object(launcher.sys, 'argv', ['workbench', '--root', str(self.root), '--name', 'state', 'cleanup']), patch.object(
                launcher, 'locked', return_value=nullcontext(self.state)), patch.object(
                launcher, 'record', return_value=self.data), patch.object(launcher, 'stop_owned'), patch.object(
                launcher, 'owned_container', return_value=None), patch.object(launcher, 'remove_apparmor'), patch.object(
                Path, 'iterdir', entries), patch.object(launcher, 'docker', return_value='') as docker:
            self.assertEqual(launcher.main(), 0)
        command = docker.call_args.args
        self.assertEqual(command[:4], ('run', '--rm', '--pull=never', '--network=none'))
        self.assertIn('--cap-drop=ALL', command)
        self.assertIn(f'type=bind,src={directory},dst=/owned', command)
        self.assertFalse(directory.exists())
        self.assertEqual(self.data['status'], 'cleaned')

    def test_build_stop_accepts_term_but_rejects_forced_kill(self):
        self.data['mode'] = 'build'
        for exit_code in (143, 137):
            containers = [{'Id': 'owned', 'State': {'Running': True}},
                          {'Id': 'owned', 'State': {'Running': False, 'ExitCode': exit_code}}]
            with patch.object(launcher, 'owned_container', side_effect=containers), patch.object(
                    launcher, 'diagnostics'), patch.object(launcher, 'docker'):
                if exit_code == 143:
                    launcher.stop_owned(self.state, self.data)
                else:
                    with self.assertRaisesRegex(ValueError, 'did not stop gracefully'):
                        launcher.stop_owned(self.state, self.data)


class NativeSelectionTests(unittest.TestCase):
    def test_native_build_uses_admitted_base_and_cannot_skip_on_lookup_failure(self):
        sys.path.insert(0, str(ROOT.parent / 'ci/integration'))
        resolver = module('workbench_resolver_test', ROOT.parent / 'ci/integration/resolve-artifacts.py')
        record = {'repository': resolver.PLATFORM, 'base_sha': 'a' * 40, 'candidate_sha': 'b' * 40}
        plan = {'mode': 'source', 'candidate_records': [record]}
        with patch.object(resolver, 'changed_files', return_value=['workbench/Dockerfile']) as changes:
            self.assertTrue(resolver.workbench_requested(plan))
            changes.assert_called_once_with(resolver.PLATFORM, record['base_sha'], record['candidate_sha'])
        with patch.object(resolver, 'changed_files', return_value=['docs/ci.md']):
            self.assertFalse(resolver.workbench_requested(plan))
        with patch.object(resolver, 'changed_files', side_effect=ValueError('lookup failed')):
            with self.assertRaisesRegex(ValueError, 'lookup failed'):
                resolver.workbench_requested(plan)
        with patch.object(resolver, 'changed_files') as changes:
            self.assertFalse(resolver.workbench_requested(plan | {'mode': 'exact-assets'}))
            changes.assert_not_called()


class AppArmorTests(LauncherTests):
    def test_load_restart_remove_only_the_owned_enforcing_profile(self):
        profiles = {'docker-default': 'enforce)'}
        calls = []
        def parser(action, source):
            name = self.data['apparmor']['name']
            self.assertNotEqual(name, 'docker-default')
            calls.append(action)
            if action == '--add':
                self.assertNotIn(name, profiles)
                profiles[name] = 'enforce)'
            elif action == '--remove':
                del profiles[name]
            else:
                self.fail('profile replacement is forbidden')
        with patch.object(launcher, 'apparmor_enabled', return_value=True), patch.object(
                launcher, 'apparmor_profiles', side_effect=lambda: dict(profiles)), patch.object(
                launcher, 'apparmor_parser', side_effect=parser), patch.object(launcher, 'docker', return_value=''), patch.object(launcher, 'wait_apparmor_tasks') as wait:
            launcher.load_apparmor(self.state, self.data)
            launcher.load_apparmor(self.state, self.data)
            command = self.command()
            self.assertIn('apparmor=' + self.data['apparmor']['name'], command)
            self.assertNotIn('apparmor=unconfined', command)
            launcher.remove_apparmor(self.state, self.data)
            launcher.remove_apparmor(self.state, self.data)
        wait.assert_called_once_with(self.data['apparmor']['name'])
        self.assertEqual(calls, ['--add', '--remove'])
        self.assertEqual(profiles, {'docker-default': 'enforce)'})


    def test_task_wait_is_bounded_and_read_failures_are_not_empty_state(self):
        with patch.object(launcher, 'host_command', side_effect=['["123"]', '[]']) as command, patch.object(
                launcher.time, 'sleep'):
            launcher.wait_apparmor_tasks('owned')
            self.assertEqual(command.call_count, 2)
        with patch.object(launcher, 'host_command', return_value='["123"]'), patch.object(
                launcher.time, 'monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(ValueError, 'still has live tasks.*123'):
                launcher.wait_apparmor_tasks('owned')
        with patch.object(launcher, 'host_command', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                launcher.wait_apparmor_tasks('owned')

    def test_task_scan_checks_threads_and_tolerates_only_exit_races(self):
        # Execute the actual fixed scanner with simulated proc entries; no
        # Docker behavior is replaced in runtime qualification.
        paths = [Path('/proc/123/task/123'), Path('/proc/123/task/124')]
        for error in (FileNotFoundError(errno.ENOENT, 'exited'),
                      ProcessLookupError(errno.ESRCH, 'exited'), OSError(errno.EINVAL, 'kernel thread')):
            output = io.StringIO()
            with patch('os.listdir', return_value=['123', 'self']), patch.object(Path, 'iterdir', return_value=iter(paths)), patch.object(
                    Path, 'read_text', side_effect=[error, 'owned (enforce)']), patch.object(sys, 'argv', ['scan', 'owned']), redirect_stdout(output):
                exec(launcher.APPARMOR_TASK_SCAN, {})
            self.assertEqual(json.loads(output.getvalue()), ['124'])
        with patch('os.listdir', return_value=['123']), patch.object(Path, 'iterdir', return_value=iter(paths)), patch.object(
                Path, 'read_text', side_effect=PermissionError(errno.EACCES, 'denied')), patch.object(sys, 'argv', ['scan', 'owned']):
            with self.assertRaises(PermissionError):
                exec(launcher.APPARMOR_TASK_SCAN, {})
        output = io.StringIO()
        with patch('os.listdir', return_value=['123']), patch.object(Path, 'iterdir', return_value=iter(paths)), patch.object(
                Path, 'read_text', side_effect=['owned-other (enforce)', 'unconfined']), patch.object(sys, 'argv', ['scan', 'owned']), redirect_stdout(output):
            exec(launcher.APPARMOR_TASK_SCAN, {})
        self.assertEqual(json.loads(output.getvalue()), [])

    def test_retry_retains_profile_until_tasks_exit(self):
        with patch.object(launcher, 'apparmor_enabled', return_value=True), patch.object(
                launcher, 'apparmor_profiles', side_effect=[{}, {f"kuasar-workbench-v1-u{os.getuid()}-{self.data['id']}": 'enforce)'}]), patch.object(launcher, 'apparmor_parser'):
            launcher.load_apparmor(self.state, self.data)
        with patch.object(launcher, 'apparmor_profiles', return_value={self.data['apparmor']['name']: 'enforce)'}), patch.object(
                launcher, 'docker', return_value=''), patch.object(launcher, 'wait_apparmor_tasks', side_effect=ValueError('live tasks')), patch.object(
                launcher, 'apparmor_parser') as parser:
            with self.assertRaisesRegex(ValueError, 'live tasks'):
                launcher.remove_apparmor(self.state, self.data)
            parser.assert_not_called()
            self.assertTrue(self.data['apparmor']['loaded'])

    def test_foreign_profile_collision_and_missing_manager_fail_closed(self):
        name = f"kuasar-workbench-v1-u{os.getuid()}-{self.data['id']}"
        with patch.object(launcher, 'apparmor_enabled', return_value=True), patch.object(
                launcher, 'apparmor_profiles', return_value={name: 'enforce)'}), patch.object(launcher, 'apparmor_parser') as parser:
            with self.assertRaisesRegex(ValueError, 'foreign AppArmor'):
                launcher.load_apparmor(self.state, self.data)
            parser.assert_not_called()
        with patch.object(launcher, 'apparmor_enabled', return_value=True), patch.object(
                launcher, 'apparmor_profiles', return_value={}), patch.object(launcher.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'needs host apparmor_parser'):
                launcher.load_apparmor(self.state, self.data)
        self.assertFalse(self.data['apparmor']['loaded'])

    def test_changed_profile_and_foreign_container_prevent_removal(self):
        with patch.object(launcher, 'apparmor_enabled', return_value=True), patch.object(
                launcher, 'apparmor_profiles', side_effect=[{}, {f"kuasar-workbench-v1-u{os.getuid()}-{self.data['id']}": 'enforce)'}]), patch.object(launcher, 'apparmor_parser'):
            launcher.load_apparmor(self.state, self.data)
        profiles = {self.data['apparmor']['name']: 'enforce)'}
        with patch.object(launcher, 'apparmor_profiles', return_value=profiles), patch.object(
                launcher, 'docker', side_effect=['foreign', json.dumps([{'AppArmorProfile': self.data['apparmor']['name']}])]), patch.object(
                launcher, 'apparmor_parser') as parser:
            with self.assertRaisesRegex(ValueError, 'still belongs to a container'):
                launcher.remove_apparmor(self.state, self.data)
            parser.assert_not_called()
        (self.state / 'apparmor.profile').write_text('changed')
        with patch.object(launcher, 'apparmor_parser') as parser:
            with self.assertRaisesRegex(ValueError, 'profile changed'):
                launcher.remove_apparmor(self.state, self.data)
            parser.assert_not_called()

    def test_outer_context_must_enforce_and_inner_detection_mask_is_required(self):
        self.data['apparmor'] = {'name': 'owned'}
        self.data['container_id'] = 'container'
        with patch.object(launcher, 'apparmor_enabled', return_value=True), patch.object(
                launcher, 'apparmor_profiles', return_value={'owned': 'enforce)'}):
            for output in ('unconfined', 'owned (complain)'):
                with patch.object(launcher, 'docker', return_value=output):
                    with self.assertRaisesRegex(ValueError, 'not confined'):
                        launcher.check_apparmor(self.data)
            with patch.object(launcher, 'docker', side_effect=['owned (enforce)', 'Y']):
                with self.assertRaisesRegex(ValueError, 'detection was not masked'):
                    launcher.check_apparmor(self.data)
            with patch.object(launcher, 'docker', side_effect=['owned (enforce)', '']):
                launcher.check_apparmor(self.data)

    def test_profile_keeps_host_security_restrictions_and_build_mode_unchanged(self):
        source = (ROOT / 'apparmor.profile').read_text()
        self.assertIn('deny /sys/kernel/security/** rwklx,', source)
        self.assertIn('deny /proc/sysrq-trigger rwklx,', source)
        self.assertNotIn('change_profile ->', source)
        self.assertNotIn('\n  mount,', source)
        # runc's read-only proc remount flags vary by kernel/runtime; keep the
        # destination exact while accepting only a remount on those paths.
        self.assertIn('remount options=(ro,bind,nosuid,nodev,noexec) /proc/{asound,bus,fs,irq,sys}/,', source)
        self.assertIn('remount options=(ro,bind,nosuid,nodev,noexec,relatime) /proc/{asound,bus,fs,irq,sys}/,', source)
        self.assertIn('mount fstype=tmpfs options=(ro) -> /proc/{acpi,scsi}/,', source)
        self.assertIn('mount options=(rw,bind) /dev/null -> /proc/{interrupts,kcore,keys,latency_stats,sched_debug,timer_list,timer_stats},', source)
        self.assertIn('mount options=(rw,bind) / -> /run/docker/netns/*,', source)
        self.assertNotIn('  remount /proc/{bus,fs,irq,sys}/,', source)
        dockerfile = (ROOT / 'Dockerfile').read_text()
        self.assertIn('systemd-logind.service redis-server.service', dockerfile)
        self.data['apparmor'] = {'name': 'system-only'}
        command = self.command(mode='build', source=self.source, inputs=None)
        self.assertFalse(any(value.startswith('apparmor=') for value in command))
        self.assertFalse(any(value.startswith('--cap-add') for value in command))


if __name__ == '__main__':
    unittest.main()
