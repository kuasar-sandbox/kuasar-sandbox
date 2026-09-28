"""Collector identity and launcher ownership contracts; real system tests are separate."""
import argparse
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

    def test_cases_without_external_images_need_no_registry(self):
        with patch.object(collector.subprocess, 'check_output') as inspect, patch.object(collector.subprocess, 'run') as copy:
            collector.collect(['storage.cache.sh'], ['x86_64', 'aarch64'], self.output)
            inspect.assert_not_called()
            copy.assert_not_called()
        self.assertEqual(json.loads((self.output / 'images.json').read_text()), [])


class LauncherTests(unittest.TestCase):
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
        self.assertFalse(any(arg.startswith('--cap-add') for arg in command))
        self.assertNotIn('--device', command)
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
