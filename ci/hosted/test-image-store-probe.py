#!/usr/bin/env python3
"""Check fixture integrity and diagnostic retention without a Docker daemon."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('image_store_probe', Path(__file__).with_name('image-store-probe.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
NONCE = '1' * 32


class ImageStoreProbeTests(unittest.TestCase):
    def test_both_archive_layouts_bind_the_same_layer_free_config(self):
        for arch in probe.workbench.PLATFORMS:
            raw, selection, facts = probe.fixture(arch, NONCE)
            self.assertLessEqual(len(raw), 16 * 1024)
            self.assertNotEqual(facts['config_digest'], facts['manifest_digest'])
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                files = {entry.name: archive.extractfile(entry).read() for entry in archive}
            docker = json.loads(files['manifest.json'])[0]
            config = files[docker['Config']]
            self.assertEqual(probe.digest(config), selection['architectures'][arch]['image_id'])
            self.assertEqual(docker['Layers'], [])
            index = json.loads(files['index.json'])
            descriptor = index['manifests'][0]
            manifest = files['blobs/sha256/' + descriptor['digest'].split(':')[1]]
            self.assertEqual(probe.digest(manifest), facts['manifest_digest'])
            self.assertEqual(len(manifest), descriptor['size'])
            self.assertEqual(json.loads(manifest)['config']['digest'], probe.digest(config))
            self.assertEqual(json.loads(manifest)['layers'], [])
            self.assertEqual(json.loads(config)['rootfs']['diff_ids'], [])

    def run_probe(self, *, config_lookup=True, image_change=None, collision=False, cleanup_error=False):
        _, selection, facts = probe.fixture('x86_64', NONCE)
        image = {'Id': facts['config_digest'] if config_lookup else facts['manifest_digest'],
                 'Os': 'linux', 'Architecture': 'amd64',
                 'Config': json.loads(facts['config_raw'])['config']}
        if image_change:
            image_change(image)
        loaded, calls = False, []

        def command(*arguments):
            nonlocal loaded
            calls.append(arguments)
            record = {'exit_code': 0, 'stdout': '', 'stderr': ''}
            if arguments[0] == 'version':
                record['stdout'] = '{}'
            elif arguments[0] == 'info':
                record['stdout'] = '{"Driver":"fixture"}'
            elif arguments[:2] == ('image', 'load'):
                loaded = True
            elif arguments[:2] == ('image', 'inspect'):
                found = loaded or collision
                if arguments[2] == facts['config_digest']:
                    found = found and config_lookup
                if found:
                    record['stdout'] = json.dumps([image])
                else:
                    record.update(exit_code=1, stderr='Error: No such image')
            elif arguments[:2] == ('image', 'rm'):
                self.assertEqual(arguments, ('image', 'rm', facts['tag']))
                if cleanup_error:
                    raise subprocess.TimeoutExpired(arguments, 60)
            else:
                self.fail('unexpected Docker operation: ' + repr(arguments))
            return record

        with tempfile.TemporaryDirectory() as directory, patch.object(probe, 'command', side_effect=command), \
                patch.object(probe.platform, 'machine', return_value='x86_64'), \
                patch.object(probe.uuid, 'uuid4') as identity:
            identity.return_value.hex = NONCE
            output = Path(directory) / 'evidence.json'
            status = probe.run_probe(output)
            return status, json.loads(output.read_bytes()), calls

    def test_classic_config_lookup_passes_and_cleans_only_the_fixture(self):
        status, report, calls = self.run_probe()
        self.assertEqual(status, 0)
        self.assertEqual(report['conclusion'], 'success')
        self.assertEqual(set(report['inspections']), {'tag', 'config', 'manifest'})
        self.assertEqual(sum(call[:2] == ('image', 'rm') for call in calls), 1)

    def test_missing_config_lookup_preserves_all_identity_evidence_and_fails_guard(self):
        status, report, _ = self.run_probe(config_lookup=False)
        self.assertEqual(status, 1)
        self.assertIn('image/config/architecture mismatch', report['error']['message'])
        self.assertEqual(report['inspections']['config']['exit_code'], 1)
        self.assertEqual(report['inspections']['tag']['exit_code'], 0)
        self.assertEqual(report['inspections']['manifest']['exit_code'], 0)
        self.assertEqual(report['cleanup']['exit_code'], 0)

    def test_current_guard_still_rejects_wrong_architecture_and_source(self):
        changes = [lambda image: image.update(Architecture='arm64'),
                   lambda image: image['Config']['Labels'].update({'org.opencontainers.image.revision': 'f' * 40})]
        for change in changes:
            with self.subTest(change=change):
                status, report, _ = self.run_probe(image_change=change)
                self.assertEqual(status, 1)
                self.assertEqual(report['cleanup']['exit_code'], 0)

    def test_manifest_identity_is_not_silently_accepted_as_a_config(self):
        _, _, facts = probe.fixture('x86_64', NONCE)
        status, report, _ = self.run_probe(image_change=lambda image: image.update(Id=facts['manifest_digest']))
        self.assertEqual(status, 1)
        self.assertIn('image/config/architecture mismatch', report['error']['message'])
        self.assertEqual(json.loads(report['inspections']['config']['stdout'])[0]['Id'], facts['manifest_digest'])

    def test_existing_name_is_not_adopted_or_removed(self):
        status, report, calls = self.run_probe(collision=True)
        self.assertEqual(status, 1)
        self.assertIn('not confirmed absent', report['error']['message'])
        self.assertFalse(any(call[:2] in [('image', 'load'), ('image', 'rm')] for call in calls))

    def test_changed_ownership_is_not_removed(self):
        status, report, calls = self.run_probe(
            image_change=lambda image: image['Config']['Labels'].update({probe.OWNER_LABEL: 'foreign'}))
        self.assertEqual(status, 1)
        self.assertIn('foreign', report['cleanup']['error'])
        self.assertFalse(any(call[:2] == ('image', 'rm') for call in calls))

    def test_cleanup_exception_keeps_evidence_and_prevents_success(self):
        status, report, _ = self.run_probe(cleanup_error=True)
        self.assertEqual(status, 1)
        self.assertEqual(report['conclusion'], 'failure')
        self.assertIn('timed out', report['cleanup']['error'])
        self.assertEqual(set(report['inspections']), {'tag', 'config', 'manifest'})


if __name__ == '__main__':
    unittest.main()
