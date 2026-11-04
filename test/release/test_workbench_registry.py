import copy
import json
import io
import tarfile
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import workbench_registry as subject

VERSION = 'release-v1.2.3-preview.20260928.2'
REVISION = 'a' * 40


def image(arch):
    config = json.dumps({'architecture': subject.PLATFORMS[arch], 'os': 'linux', 'config': {'Labels': {
        'org.opencontainers.image.version': VERSION, 'org.opencontainers.image.revision': REVISION}}}).encode()
    receipt = {'arch': arch, 'aggregate_version': VERSION, 'source_revision': REVISION,
               'image_id': subject.digest(config), 'sha256': 'b' * 64, 'archive': arch + '.tar.gz'}
    manifest = json.dumps({'schemaVersion': 2, 'config': {'digest': subject.digest(config), 'size': len(config)},
                           'layers': []}).encode()
    return config, manifest, receipt


class RegistryTests(unittest.TestCase):
    def test_inspection_requests_raw_config_bytes_without_json_reformatting(self):
        config, manifest, receipt = image('x86_64')
        reference = subject.REPOSITORY + '@' + subject.digest(manifest)
        def inspect(command, **kwargs):
            self.assertIn('--raw', command)
            payload = config if '--config' in command else manifest
            return subprocess.CompletedProcess(command, 0, payload, b'')
        with patch.object(subject.subprocess, 'run', side_effect=inspect):
            raw = subject.inspect_raw(reference, Path('/anonymous'))
            actual_config = subject.inspect_raw(reference, Path('/anonymous'), config=True)
        self.assertEqual(subject.check_manifest(raw, actual_config, receipt)['image_id'], receipt['image_id'])

    def test_raw_config_bytes_must_match_verified_archive_id(self):
        config, manifest, receipt = image('x86_64')
        result = subject.check_manifest(manifest, config, receipt)
        self.assertEqual(result['digest'], subject.digest(manifest))
        with self.assertRaisesRegex(ValueError, 'config differs'):
            subject.check_manifest(manifest, config + b' ', receipt)
        with self.assertRaisesRegex(ValueError, 'config differs'):
            subject.check_manifest(manifest, config, receipt | {'image_id': 'sha256:' + 'c' * 64})
        with self.assertRaisesRegex(ValueError, 'platform differs'):
            subject.check_manifest(manifest, config, receipt | {'arch': 'aarch64'})
        with self.assertRaisesRegex(ValueError, 'source/version differs'):
            subject.check_manifest(manifest, config, receipt | {'source_revision': 'c' * 40})

    def test_multiarch_index_exactly_binds_both_verified_manifests(self):
        children = {arch: subject.check_manifest(manifest, config, receipt)
                    for arch in subject.PLATFORMS for config, manifest, receipt in [image(arch)]}
        index = {'schemaVersion': 2, 'manifests': [
            {'digest': record['digest'], 'size': record['size'], 'platform': {'os': 'linux', 'architecture': subject.PLATFORMS[arch]}}
            for arch, record in children.items()]}
        raw = json.dumps(index).encode()
        self.assertEqual(subject.check_index(raw, children), subject.digest(raw))
        for mutation in ('missing', 'duplicate', 'digest', 'size'):
            broken = copy.deepcopy(index)
            if mutation == 'missing': broken['manifests'].pop()
            if mutation == 'duplicate': broken['manifests'][1] = broken['manifests'][0]
            if mutation == 'digest': broken['manifests'][0]['digest'] = 'sha256:' + 'd' * 64
            if mutation == 'size': broken['manifests'][0]['size'] += 1
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                subject.check_index(json.dumps(broken).encode(), children)

    def test_only_explicit_registry_absence_authorizes_a_write(self):
        for error in (b'unauthorized', b'connection refused', b'HTTP 403', b'timeout'):
            with patch.object(subject.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', error)):
                with self.assertRaisesRegex(ValueError, 'cannot inspect'):
                    subject.inspect_raw('ghcr.io/kuasar-sandbox/workbench:v1', Path('/auth'), missing=True)
        with patch.object(subject.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', b'manifest unknown')):
            self.assertIsNone(subject.inspect_raw('ghcr.io/kuasar-sandbox/workbench:v1', Path('/auth'), missing=True))
            with self.assertRaises(ValueError):
                subject.inspect_raw('ghcr.io/kuasar-sandbox/workbench:v1', Path('/auth'))

    def test_public_readback_checks_real_layer_bytes_even_when_config_matches(self):
        # Only registry transport is mocked. The Docker archive/config/layers
        # are real bytes passed through the production archive verifier.
        layer_output = io.BytesIO()
        with tarfile.open(fileobj=layer_output, mode='w') as layer:
            member = tarfile.TarInfo('identity')
            member.size = 7
            layer.addfile(member, io.BytesIO(b'offline'))
        layer_bytes = layer_output.getvalue()
        config, _, receipt = image('x86_64')
        config = json.loads(config)
        config['rootfs'] = {'type': 'layers', 'diff_ids': [subject.digest(layer_bytes)]}
        config = json.dumps(config).encode()
        receipt['image_id'] = subject.digest(config)
        reference = subject.REPOSITORY + '@sha256:' + 'f' * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / 'anonymous.json'
            auth.write_text('{"auths":{}}')
            for corrupt in (False, True):
                destination = root / ('corrupt.tar' if corrupt else 'valid.tar')
                def transfer(command, **kwargs):
                    self.assertEqual(command, ['skopeo', 'copy', '--authfile', str(auth),
                        'docker://' + reference, 'docker-archive:' + str(destination)])
                    manifest = [{'Config': 'config.json', 'Layers': ['layer.tar']}]
                    with tarfile.open(destination, 'w') as archive:
                        for name, data in {'config.json': config, 'manifest.json': json.dumps(manifest).encode(),
                                           'layer.tar': layer_bytes + (b'changed' if corrupt else b'')}.items():
                            member = tarfile.TarInfo(name)
                            member.size, member.mode = len(data), 0o644
                            archive.addfile(member, io.BytesIO(data))
                with self.subTest(corrupt=corrupt), patch.object(subject.subprocess, 'run', side_effect=transfer) as run:
                    if corrupt:
                        with self.assertRaisesRegex(ValueError, 'layer differs from config'):
                            subject.verify_public_layers(reference, receipt, auth, destination)
                    else:
                        subject.verify_public_layers(reference, receipt, auth, destination)
                        self.assertFalse(destination.exists())
                    run.assert_called_once()
            with self.assertRaisesRegex(ValueError, 'immutable digest'):
                subject.verify_public_layers(subject.REPOSITORY + ':moving-tag', receipt, auth, root / 'unused.tar')

    def test_conflicting_existing_tag_never_triggers_copy_or_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = image('x86_64')[2]
            (root / 'release-notes.md').write_text('Human release notes.\n')
            wrong_config, wrong_manifest, _ = image('aarch64')
            with patch.object(subject, 'validate_stage', return_value={'x86_64': receipt}), patch.dict(
                    os.environ, {'GH_TOKEN': 'unit-test-only', 'GITHUB_ACTOR': 'unit-test'}), patch.object(
                    subject, 'inspect_raw', side_effect=[wrong_manifest, wrong_config]), patch.object(
                    subject.subprocess, 'run') as command:
                with self.assertRaisesRegex(ValueError, 'config differs'):
                    subject.publish(root, VERSION, REVISION, {}, {}, {})
                command.assert_not_called()
            self.assertEqual((root / 'release-notes.md').read_text(), 'Human release notes.\n')

    def test_full_body_limit_stops_before_registry_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = 'x' * (subject.publication_body.MAX_CHARACTERS + 1)
            notes = root / 'release-notes.md'
            notes.write_text(body)
            with patch.object(subject, 'inspect_raw') as inspect, patch.object(subject.subprocess, 'run') as command:
                with self.assertRaisesRegex(ValueError, 'must not be truncated'):
                    subject.publish(root, VERSION, REVISION, {'baseline': {'version': VERSION, 'sha': REVISION}}, {}, {})
                inspect.assert_not_called()
                command.assert_not_called()
            self.assertEqual(notes.read_text(), body)

    def test_resume_reuses_matching_images_and_index_and_reads_public_layers(self):
        images = {arch: image(arch) for arch in subject.PLATFORMS}
        children = {arch: subject.check_manifest(raw, config, receipt)
                    for arch, (config, raw, receipt) in images.items()}
        index = json.dumps({'schemaVersion': 2, 'manifests': [
            {'digest': row['digest'], 'size': row['size'],
             'platform': {'os': 'linux', 'architecture': subject.PLATFORMS[arch]}}
            for arch, row in children.items()]}).encode()
        reference = subject.REPOSITORY + ':' + VERSION.removeprefix('release-')

        def inspect(ref, auth, *, config=False, missing=False):
            if ref == reference:
                self.assertFalse(config)
                return index
            for arch, (image_config, raw, _) in images.items():
                if ref in (reference + '-' + arch, subject.REPOSITORY + '@' + subject.digest(raw)):
                    return image_config if config else raw
            self.fail('unexpected registry lookup: ' + ref)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / 'release-notes.md'
            notes.write_text('Original notes\n')
            with patch.object(subject, 'validate_stage',
                    return_value={arch: row[2] for arch, row in images.items()}), patch.dict(
                    os.environ, {'GH_TOKEN': 'unit-test-only', 'GITHUB_ACTOR': 'unit-test'}), patch.object(
                    subject, 'inspect_raw', side_effect=inspect), patch.object(
                    subject, 'verify_public_layers') as read_layers, patch.object(subject.subprocess, 'run') as command:
                first = subject.publish(root, VERSION, REVISION, {}, {}, {})
                first_body = notes.read_text()
                self.assertEqual(subject.publish(root, VERSION, REVISION, {}, {}, {}, read_only=True), first)
                self.assertEqual(notes.read_text(), first_body)
                command.assert_not_called()
                self.assertEqual(read_layers.call_count, 4)
                for call in read_layers.call_args_list:
                    ref, receipt, auth, destination = call.args
                    self.assertEqual(ref, subject.REPOSITORY + '@' + children[receipt['arch']]['digest'])
                    self.assertEqual(auth.name, 'anonymous.json')
            self.assertEqual(first_body, 'Original notes\n')
            self.assertEqual(first, {'reference': reference, 'digest': subject.digest(index), 'architectures': children})

    def test_read_only_recovery_never_creates_a_missing_image(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(subject, 'validate_stage', return_value={'x86_64': image('x86_64')[2]}), \
                 patch.dict(os.environ, GH_TOKEN='test-only', GITHUB_ACTOR='fixture'), \
                 patch.object(subject, 'inspect_raw', return_value=None), \
                 patch.object(subject.subprocess, 'run') as command:
                with self.assertRaisesRegex(ValueError, 'missing its Workbench image'):
                    subject.publish(Path(directory), VERSION, REVISION, {}, {}, {}, read_only=True)
                command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
