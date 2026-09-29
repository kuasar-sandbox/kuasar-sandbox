"""Local dependency verification; Docker calls are mocked only in call-contract tests."""
import copy
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / 'lib'))
import workspace


def image_fixture(directory, label='python', reference='python:3.12-slim', arch='amd64', index=False):
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode='w') as archive:
        entry = tarfile.TarInfo('example')
        entry.size = len(b'example content')
        archive.addfile(entry, io.BytesIO(b'example content'))
    layer = layer.getvalue()
    config = json.dumps({'architecture': arch, 'os': 'linux', 'rootfs': {
        'type': 'layers', 'diff_ids': [workspace.sha256_bytes(layer)]}}).encode()
    identity = workspace.sha256_bytes(config)
    path = directory / (label + '.tar')
    manifest = [{'Config': 'config.json', 'RepoTags': [reference], 'Layers': ['layer.tar']}]
    with tarfile.open(path, 'w') as archive:
        for name, data in {'manifest.json': json.dumps(manifest).encode(),
                           'config.json': config, 'layer.tar': layer}.items():
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(data), 0o644
            archive.addfile(entry, io.BytesIO(data))
    registry = json.dumps({'schemaVersion': 2, 'config': {'digest': identity, 'size': len(config)},
                           'layers': [{'digest': workspace.sha256_bytes(layer), 'size': len(layer)}]})
    record = {'reference': reference, 'platform': 'linux/' + arch, 'image_id': identity,
              'archive': path.name, 'sha256': workspace.digest(path), 'manifest': registry,
              'manifest_digest': workspace.sha256_bytes(registry.encode())}
    record['registry_digest'] = record['manifest_digest']
    if index:
        record['index'] = json.dumps({'schemaVersion': 2, 'manifests': [{
            'digest': record['manifest_digest'], 'size': len(registry.encode()),
            'platform': {'os': 'linux', 'architecture': arch}}]})
        record['index_digest'] = workspace.sha256_bytes(record['index'].encode())
        record['registry_digest'] = record['index_digest']
    return record


class InputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.deps = self.root / 'deps'
        self.deps.mkdir()
        self.work = self.root / 'prepared'
        self.work.mkdir()
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.record = image_fixture(self.deps)
        self.requests = {'python': {'reference': self.record['reference'], 'platform': self.record['platform']}}

    def write_records(self, records=None):
        (self.deps / 'images.json').write_text(json.dumps(records if records is not None else [self.record]))

    def local(self, offline=True):
        self.write_records()
        return workspace.local_image_inputs(self.requests, self.deps, offline)

    def test_existing_selection_and_both_architectures(self):
        cases = {'storage.cache.sh': [], 'network.tap.sh': [], 'image.manifest.sh': [],
                 'image.flatten.sh': [], 'image.registry.sh': [], 'basic.demo.sh': ['python'],
                 'orchestrator.exec.sh': ['python'], 'builder.copy.sh': ['python'],
                 'sandbox.lifecycle.sh': ['python', 'busybox'], 'snapshot.restore.sh': ['python', 'busybox'],
                 'network.tapfd.sh': ['python', 'busybox'], 'image.manifest-boot.sh': ['python', 'busybox'],
                 'image.sandbox-assembly.sh': ['python', 'busybox'],
                 'telemetry.backends.sh': ['python', 'busybox', 'prometheus', 'clickhouse']}
        for arch, target in [('x86_64', 'linux/amd64'), ('aarch64', 'linux/arm64')]:
            for case, labels in cases.items():
                with self.subTest(arch=arch, case=case):
                    requests = workspace.external_image_requests([case], arch)
                    self.assertEqual(list(requests), labels)
                    self.assertTrue(all(record['platform'] == target for record in requests.values()))

    def test_option_precedence_defaults_and_invalid_values(self):
        self.assertEqual(workspace.dependency_options(), (None, False))
        self.assertEqual(workspace.dependency_options(self.deps, True, {'E2E_OFFLINE': '0'}), (self.deps, True))
        self.assertEqual(workspace.dependency_options(environment={'E2E_DEPS_DIR': str(self.deps),
                                                                 'E2E_OFFLINE': '1'}), (self.deps, True))
        self.assertEqual(workspace.dependency_options(self.deps, environment={'E2E_DEPS_DIR': '/missing',
                                                                            'E2E_OFFLINE': '0'}), (self.deps, False))
        for value in ['', 'true', 'false', 'yes', '2']:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'E2E_OFFLINE'):
                workspace.dependency_options(offline=True, environment={'E2E_OFFLINE': value})
        for value in ['', str(self.root / 'missing')]:
            with self.assertRaises(ValueError):
                workspace.dependency_options(value)
        link = self.root / 'link'
        link.symlink_to(self.deps, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'unsafe deps-dir'):
            workspace.dependency_options(link)

    def test_verified_local_tags_and_digest_pins(self):
        for index in (False, True):
            with self.subTest(index=index):
                self.record = image_fixture(self.deps, index=index)
                self.assertEqual(self.local()['python'][1], self.record)
                self.record['reference'] += '@' + self.record['registry_digest']
                self.requests['python']['reference'] = self.record['reference']
                self.assertEqual(self.local()['python'][1]['image_id'], self.record['image_id'])
                self.requests['python']['reference'] = 'python:3.12-slim'

    def test_selected_local_inputs_copy_without_docker_or_network(self):
        busybox = image_fixture(self.deps, 'busybox', 'busybox:latest')
        self.write_records([self.record, busybox, {'reference': 'unused', 'platform': 'linux/amd64', 'archive': '../bad'}])
        before = workspace.files(self.deps)
        with patch.object(workspace.subprocess, 'run') as run, patch.object(workspace.subprocess, 'check_output') as output:
            result = workspace.prepare_fixtures(self.work, 'x86_64', ['sandbox.lifecycle.sh'], self.deps, True)
        run.assert_not_called()
        output.assert_not_called()
        self.assertEqual(set(result['images']), {'python', 'busybox'})
        self.assertEqual(workspace.files(self.deps), before)
        source, copied = self.deps / 'python.tar', self.work / 'images/python.tar'
        self.assertEqual(copied.read_bytes(), source.read_bytes())
        self.assertNotEqual(source.stat().st_ino, copied.stat().st_ino)
        self.assertFalse(copied.is_symlink())
        self.assertEqual(copied.stat().st_mode & 0o777, 0o444)
        workspace.seal(self.work, result)
        source.write_bytes(b'changed source')
        workspace.verify(self.work)

    def test_unselected_images_are_not_prerequisites(self):
        (self.deps / 'images.json').write_text('invalid but unneeded')
        result = workspace.prepare_fixtures(self.work, 'aarch64', ['storage.cache.sh'], self.deps, True)
        self.assertEqual(result['images'], {})

    def test_missing_offline_input_names_reference_platform_and_location(self):
        for directory in (None, self.deps):
            with self.assertRaisesRegex(ValueError, 'missing offline image: python:3.12-slim linux/amd64') as failure:
                workspace.local_image_inputs(self.requests, directory, True)
            self.assertIn(str(self.deps / 'images.json') if directory else 'no deps-dir', str(failure.exception))

    def test_other_platform_or_reference_is_not_a_substitute(self):
        for field, value in [('platform', 'linux/arm64'), ('reference', 'python:other')]:
            record = dict(self.record, **{field: value})
            self.write_records([record])
            with self.assertRaisesRegex(ValueError, 'missing offline image'):
                workspace.local_image_inputs(self.requests, self.deps, True)
            self.assertEqual(workspace.local_image_inputs(self.requests, self.deps, False), {})

    def test_ambiguous_or_malformed_description_fails(self):
        self.write_records([self.record, self.record])
        with self.assertRaisesRegex(ValueError, 'ambiguous local image'):
            workspace.local_image_inputs(self.requests, self.deps, False)
        for value in ['{', '{}', '[1]']:
            (self.deps / 'images.json').write_text(value)
            with self.assertRaises(ValueError):
                workspace.local_image_inputs(self.requests, self.deps, False)

    def test_archive_hash_and_config_id_cannot_be_substituted(self):
        for field, value in [('sha256', 'a' * 64), ('image_id', 'sha256:' + 'b' * 64)]:
            saved = self.record[field]
            self.record[field] = value
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                self.local(offline=False)
            self.record[field] = saved

    def test_self_asserted_registry_digest_does_not_prove_identity(self):
        self.record['reference'] += '@sha256:' + 'c' * 64
        self.requests['python']['reference'] = self.record['reference']
        self.record['registry_digest'] = 'sha256:' + 'c' * 64
        with self.assertRaisesRegex(ValueError, 'resolved registry digest mismatch'):
            self.local()
        self.record['registry_digest'] = self.record['manifest_digest']
        with self.assertRaisesRegex(ValueError, 'requested registry digest'):
            self.local()
        self.record['manifest_digest'] = 'sha256:' + 'c' * 64
        with self.assertRaisesRegex(ValueError, 'manifest digest mismatch'):
            self.local()

    def test_manifest_must_bind_the_verified_archive_config(self):
        manifest = json.loads(self.record['manifest'])
        manifest['config']['digest'] = 'sha256:' + 'd' * 64
        self.record['manifest'] = json.dumps(manifest)
        self.record['registry_digest'] = self.record['manifest_digest'] = workspace.sha256_bytes(self.record['manifest'].encode())
        with self.assertRaisesRegex(ValueError, 'does not bind the archive'):
            self.local()

    def test_index_requires_unique_platform_digest_and_size(self):
        record = image_fixture(self.deps, index=True)
        for mutation in ('platform', 'digest', 'size', 'duplicate'):
            self.record = copy.deepcopy(record)
            index = json.loads(record['index'])
            if mutation == 'duplicate':
                index['manifests'] *= 2
            elif mutation == 'platform':
                index['manifests'][0]['platform']['architecture'] = 'arm64'
            else:
                index['manifests'][0][mutation] = 0
            self.record['index'] = json.dumps(index)
            self.record['registry_digest'] = self.record['index_digest'] = workspace.sha256_bytes(self.record['index'].encode())
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'unique target manifest'):
                self.local()

    def test_unsafe_dependency_paths_and_symlinks_fail(self):
        for name in ['/etc/passwd', '../python.tar', 'a/../../python.tar', './python.tar', 'a//b', 'a\\b']:
            self.record['archive'] = name
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'unsafe dependency path'):
                self.local()
        self.record['archive'] = 'link/python.tar'
        (self.deps / 'link').symlink_to(self.deps, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.local()
        self.record['archive'] = 'python.tar'
        self.write_records()
        (self.deps / 'images.json').unlink()
        (self.deps / 'images.json').symlink_to(self.root / 'absent')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            workspace.local_image_inputs(self.requests, self.deps, False)

    def test_wrong_platform_actual_archive_fails_despite_claim(self):
        self.record = image_fixture(self.deps, arch='arm64')
        self.record['platform'] = 'linux/amd64'
        with self.assertRaisesRegex(ValueError, 'wrong image platform'):
            self.local()

    def test_archive_member_safety_and_duplicate_paths(self):
        original = (self.deps / 'python.tar').read_bytes()
        for name, kind in [('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                           ('link', tarfile.SYMTYPE), ('hardlink', tarfile.LNKTYPE),
                           ('device', tarfile.CHRTYPE), ('manifest.json', tarfile.REGTYPE)]:
            path = self.deps / 'python.tar'
            path.write_bytes(original)
            with tarfile.open(path, 'a') as archive:
                member = tarfile.TarInfo(name)
                member.type, member.linkname = kind, 'config.json'
                archive.addfile(member)
            self.record['sha256'] = workspace.digest(path)
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.local()

    def test_layer_bytes_are_bound_even_when_outer_hash_is_updated(self):
        path = self.deps / 'python.tar'
        data = path.read_bytes().replace(b'example content', b'altered content')
        path.write_bytes(data)
        self.record['sha256'] = workspace.digest(path)
        with self.assertRaisesRegex(ValueError, 'layer differs from config'):
            self.local()

    def test_skopeo_legacy_alias_only_targets_a_verified_regular_layer(self):
        path = self.deps / 'python.tar'
        with tarfile.open(path) as archive:
            config = archive.extractfile('config.json').read()
            layer = archive.extractfile('layer.tar').read()
        layer_name = workspace.sha256_bytes(layer).split(':')[1] + '.tar'
        alias = 'a' * 64 + '/layer.tar'
        extra = 'b' * 64 + '.tar'

        def save(target, *, selected_alias=False, corrupt=False):
            manifest = [{'Config': 'config.json', 'RepoTags': [],
                         'Layers': [alias if selected_alias else layer_name]}]
            with tarfile.open(path, 'w') as archive:
                for name, data in {'manifest.json': json.dumps(manifest).encode(), 'config.json': config,
                                   layer_name: layer + (b'corrupt' if corrupt else b''), extra: layer}.items():
                    member = tarfile.TarInfo(name)
                    member.size, member.mode = len(data), 0o444
                    archive.addfile(member, io.BytesIO(data))
                member = tarfile.TarInfo(alias)
                member.type, member.linkname, member.mode = tarfile.SYMTYPE, target, 0
                archive.addfile(member)
            self.record['sha256'] = workspace.digest(path)

        save('../' + layer_name)
        self.local()
        for target in ('../../' + layer_name, '/' + layer_name, '../config.json',
                       '../' + extra, '../' + 'c' * 64 + '.tar', alias):
            save(target)
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.local()
        save('../' + layer_name, selected_alias=True)
        with self.assertRaisesRegex(ValueError, 'missing image archive member'):
            self.local()
        save('../' + layer_name, corrupt=True)
        with self.assertRaisesRegex(ValueError, 'layer differs from config'):
            self.local()

    def test_docker_archive_with_compressed_oci_blobs_binds_both_manifests(self):
        path = self.deps / 'python.tar'
        with tarfile.open(path) as source:
            config = source.extractfile('config.json').read()
            layer = gzip.compress(source.extractfile('layer.tar').read(), mtime=0)
        def descriptor(data):
            return {'digest': workspace.sha256_bytes(data), 'size': len(data)}
        def blob_path(data):
            return 'blobs/sha256/' + workspace.sha256_bytes(data).split(':')[1]
        config_path, layer_path = blob_path(config), blob_path(layer)
        manifest = json.dumps({'schemaVersion': 2, 'config': descriptor(config), 'layers': [descriptor(layer)]}).encode()
        def save_oci(contradict=False):
            index_manifest = manifest if not contradict else json.dumps({
                'schemaVersion': 2, 'config': descriptor(layer), 'layers': [descriptor(layer)]}).encode()
            contents = {config_path: config, layer_path: layer, blob_path(index_manifest): index_manifest,
                'index.json': json.dumps({'schemaVersion': 2, 'manifests': [descriptor(index_manifest)]}).encode(),
                'oci-layout': b'{"imageLayoutVersion":"1.0.0"}',
                'manifest.json': json.dumps([{'Config': config_path, 'Layers': [layer_path], 'RepoTags': []}]).encode()}
            with tarfile.open(path, 'w') as archive:
                for name, data in contents.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
            self.record['sha256'] = workspace.digest(path)
        save_oci()
        self.local()
        save_oci(contradict=True)
        with self.assertRaisesRegex(ValueError, 'manifests disagree'):
            self.local()

    def test_local_derived_recipe_loads_only_its_python_base(self):
        self.write_records()
        helper = self.work / 'test/e2e/lib/orchestrator/prepare_base_image.sh'
        helper.parent.mkdir(parents=True)
        helper.write_text('# recipe call is mocked in this unit test\n')
        def execute(command, **kwargs):
            if command[:3] == ['docker', 'image', 'save']:
                shutil.copyfile(self.deps / self.record['archive'], command[4])
            return subprocess.CompletedProcess(command, 0)
        inspected = json.dumps([{'Id': self.record['image_id'], 'Os': 'linux', 'Architecture': 'amd64'}]).encode()
        with patch.object(workspace.platform, 'machine', return_value='x86_64'), patch.object(
                workspace.subprocess, 'run', side_effect=execute) as run, patch.object(
                workspace.subprocess, 'check_output', return_value=inspected):
            result = workspace.prepare_fixtures(self.work, 'x86_64', ['orchestrator.exec.sh'], self.deps, True)
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual([call for call in calls if call[:3] == ['docker', 'image', 'load']],
                         [['docker', 'image', 'load', '--input', str(self.work / 'images/python.tar')]])
        self.assertEqual([call[-1] for call in calls if call[0] == 'bash'], ['base', 'execute'])
        self.assertEqual([call[-1] for call in calls if call[:3] == ['docker', 'image', 'save']],
                         ['kuasar-e2e-orchestrator-' + variant + ':' + self.record['image_id'].split(':')[1][:16]
                          for variant in ('base', 'execute')])
        self.assertFalse(any('pull' in call for call in calls))
        self.assertEqual(set(result['images']), {'python', 'orchestrator-base', 'orchestrator-execute'})

    def test_missing_or_corrupt_selected_input_prevents_any_remote_repair(self):
        for offline in (True, False):
            self.record['sha256'] = 'f' * 64
            self.write_records()
            with patch.object(workspace.subprocess, 'run') as run, patch.object(workspace.subprocess, 'check_output') as output:
                with self.assertRaisesRegex(ValueError, 'sha256 mismatch'):
                    workspace.prepare_fixtures(self.work, 'x86_64', ['sandbox.lifecycle.sh'], self.deps, offline)
                run.assert_not_called()
                output.assert_not_called()
        # Missing busybox fails before even a valid Python archive is loaded.
        self.record = image_fixture(self.deps)
        self.write_records()
        with patch.object(workspace.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'missing offline image: busybox'):
                workspace.prepare_fixtures(self.work, 'x86_64', ['sandbox.lifecycle.sh'], self.deps, True)
            run.assert_not_called()

    def test_online_default_preserves_exported_references_and_checks_config_id(self):
        records = {self.record['reference']: self.record,
                   'busybox:latest': image_fixture(self.deps, 'busybox', 'busybox:latest')}
        def inspect(command, **kwargs):
            record = records[command[-1]]
            return json.dumps([{'Id': record['image_id'], 'RepoDigests': ['example@' + record['registry_digest']]}]).encode()
        def execute(command, **kwargs):
            if command[:3] == ['docker', 'image', 'save']:
                record = records[command[-1]]
                shutil.copyfile(self.deps / record['archive'], command[4])
            return subprocess.CompletedProcess(command, 0)
        with patch.object(workspace.subprocess, 'run', side_effect=execute) as run, patch.object(
                workspace.subprocess, 'check_output', side_effect=inspect):
            result = workspace.prepare_fixtures(self.work, 'x86_64', ['sandbox.lifecycle.sh'])
        pulls = [call.args[0] for call in run.call_args_list if call.args[0][0] == 'timeout']
        self.assertEqual(pulls, [['timeout', '3m', 'docker', 'pull', '--platform=linux/amd64', reference]
                                 for reference in records])
        self.assertEqual(set(result['images']), {'python', 'busybox'})
        self.assertEqual([call.args[0][-1] for call in run.call_args_list
                          if call.args[0][:3] == ['docker', 'image', 'save']], list(records))


if __name__ == '__main__':
    unittest.main()
