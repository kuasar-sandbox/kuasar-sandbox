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
systems = module('workbench_system_tests', ROOT / 'test-system.py')



class StartupFixtureTests(unittest.TestCase):
    def test_exact_local_image_id_needs_no_build_or_pull(self):
        image = 'sha256:' + 'a' * 64
        entrypoint = ['sh', '-c', 'exit 42']
        with patch.object(systems, 'run', side_effect=['owned-container', 'derived-id', '']) as call:
            self.assertEqual(systems.startup_fixture(image, 'fixture:test', entrypoint, 'owner'), 'derived-id')
        commands = [item.args[0] for item in call.call_args_list]
        self.assertEqual(commands, [
            ['docker', 'create', '--pull=never', '--network=none', '--label',
             'org.kuasar.workbench.test=owner', '--entrypoint', '/bin/true', image],
            ['docker', 'commit', '--change', 'ENTRYPOINT ' + json.dumps(entrypoint),
             '--change', 'CMD []', 'owned-container', 'fixture:test'],
            ['docker', 'rm', '-v', 'owned-container'],
        ])

    def test_failed_commit_still_removes_only_its_never_started_container(self):
        with patch.object(systems, 'run', side_effect=['owned-container', RuntimeError('commit failed'), '']) as call:
            with self.assertRaisesRegex(RuntimeError, 'commit failed'):
                systems.startup_fixture('sha256:' + 'a' * 64, 'fixture:test', ['sh'], 'owner')
        self.assertEqual(call.call_args_list[-1].args[0], ['docker', 'rm', '-v', 'owned-container'])

    def test_failed_create_cannot_remove_unowned_resources(self):
        with patch.object(systems, 'run', side_effect=RuntimeError('missing local image')) as call:
            with self.assertRaisesRegex(RuntimeError, 'missing local image'):
                systems.startup_fixture('sha256:' + 'a' * 64, 'fixture:test', ['sh'], 'owner')
        self.assertEqual(call.call_count, 1)


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
        Path(path).chmod(0o600)  # Fixture transport permissions must not depend on the invoking umask.
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

    def test_system_uses_admin_privileges_without_host_policy_dependencies(self):
        command = self.command()
        self.assertIn('--cgroupns=private', command)
        self.assertIn('nofile=1048576:1048576', command)
        self.assertIn('--privileged', command)
        self.assertFalse(any(arg.startswith('--cap-add=') for arg in command))
        self.assertNotIn('--cpuset-cpus', command)
        self.assertNotIn('--security-opt', command)
        text = ' '.join(command)
        for forbidden in ('/var/run/docker.sock', '/run/docker.sock', 'src=/sys/fs/cgroup',
                          '--network=host', '--pid=host', '--ipc=host', '--cgroupns=host'):
            self.assertNotIn(forbidden, text)
        self.assertIn(f'type=bind,src={self.inputs},dst=/inputs/release,readonly', command)
        self.assertIn(f'type=bind,src={self.state}/docker,dst=/var/lib/docker', command)
        self.assertIn(f'type=bind,src={self.state}/containerd,dst=/var/lib/containerd', command)
        self.assertEqual((self.state / 'machine-id').read_text().strip(), self.data['id'])

    def test_nested_services_use_the_outer_task_budget(self):
        recipe = (ROOT / 'Dockerfile').read_text()
        self.assertIn('DefaultTasksMax=infinity', recipe)
        command = self.command(pids=512)
        self.assertEqual(command[command.index('--pids-limit') + 1], '512')

    def test_temporary_binaries_can_execute_in_both_workbench_modes(self):
        for mode in ('build', 'system'):
            with self.subTest(mode=mode):
                command = self.command(mode=mode, source=self.source if mode == 'build' else None)
                mounts = [command[i + 1] for i, arg in enumerate(command) if arg == '--tmpfs']
                self.assertIn('/tmp:exec', mounts)
                self.assertIn('/run', mounts)
                self.assertIn('/run/lock', mounts)

    def test_system_check_needs_no_host_policy_tool_or_kvm(self):
        info = {'OSType': 'linux', 'KernelVersion': 'kernel', 'Architecture': 'aarch64',
                'OperatingSystem': 'test Linux', 'CgroupVersion': '2', 'SecurityOptions': ['name=apparmor']}
        image = {'Os': 'linux', 'Architecture': 'arm64', 'Id': self.data['image_id'],
                 'Config': {'Labels': {'org.opencontainers.image.source':
                                      'https://github.com/kuasar-sandbox/kuasar-sandbox'}}}
        with patch.dict(os.environ, {'DOCKER_HOST': 'unix:///var/run/docker.sock'}), patch.object(
                launcher.platform, 'system', return_value='Linux'), patch.object(
                launcher.platform, 'release', return_value='kernel'), patch.object(
                launcher.platform, 'machine', return_value='aarch64'), patch.object(
                launcher, 'docker', return_value=json.dumps(info)), patch.object(
                launcher, 'inspect', return_value=image), patch.object(
                launcher.shutil, 'which', side_effect=AssertionError('host policy-tool lookup')), patch.object(
                launcher.os, 'sysconf', return_value=65536):
            checked = launcher.host_check('image', 'system')
        self.assertEqual(checked['mode'], 'system')
        self.assertFalse(hasattr(launcher, 'load_apparmor'))
        self.assertFalse(hasattr(launcher, 'seccomp_profile'))

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
                launcher, 'owned_container', return_value=None), patch.object(
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


    def test_system_halt_accepts_namespace_sigint_but_not_kill_or_oom(self):
        self.data['mode'] = 'system'
        for code, oom, accepted in ((0, False, True), (130, False, True), (137, False, False),
                                    (130, True, False), (1, False, False), (143, False, False)):
            containers = [{'Id': 'owned', 'State': {'Running': True}},
                          {'Id': 'owned', 'State': {'Running': False, 'ExitCode': code, 'OOMKilled': oom}}]
            with self.subTest(code=code, oom=oom), patch.object(
                    launcher, 'owned_container', side_effect=containers), patch.object(
                    launcher, 'diagnostics'), patch.object(launcher, 'docker'):
                if accepted:
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




if __name__ == '__main__':
    unittest.main()
