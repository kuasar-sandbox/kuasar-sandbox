"""Byte-bound image receipt and committed delivery cutover regressions."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import selection
import workbench_assets as subject

VERSION = 'release-v1.2.3-preview.20260928.2'
REVISION = 'a' * 40


class WorkbenchAssetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / selection.workbench_archive(VERSION, 'x86_64')
        self.config = {'os': 'linux', 'architecture': 'amd64', 'rootfs': {'type': 'layers', 'diff_ids': []},
                       'config': {'Labels': {'org.opencontainers.image.version': VERSION,
                                  'org.opencontainers.image.revision': REVISION,
                                  'org.opencontainers.image.source': 'https://github.com/kuasar-sandbox/kuasar-sandbox'}}}
        self.write()

    def write(self):
        config = json.dumps(self.config).encode()
        manifest = [{'Config': 'config.json', 'Layers': [],
                     'RepoTags': ['ghcr.io/kuasar-sandbox/workbench:' + VERSION.removeprefix('release-')]}]
        with tarfile.open(self.path, 'w:gz') as archive:
            for name, data in {'config.json': config, 'manifest.json': json.dumps(manifest).encode()}.items():
                member = tarfile.TarInfo(name)
                member.size, member.mode = len(data), 0o644
                archive.addfile(member, io.BytesIO(data))
        self.record = {'aggregate_version': VERSION, 'arch': 'x86_64', 'source_revision': REVISION,
                       'archive': self.path.name, 'size': self.path.stat().st_size,
                       'sha256': subject.workspace.digest(self.path),
                       'image_id': 'sha256:' + hashlib.sha256(config).hexdigest(), 'compression_seconds': 0.1}
        self.receipt = self.root / 'workbench-x86_64.json'
        self.receipt.write_text(json.dumps(self.record))

    def validate(self):
        return subject.validate(self.root, VERSION, 'x86_64', REVISION)

    def test_actual_config_and_archive_match_receipt(self):
        self.assertEqual(self.validate(), self.record)

    def test_self_claimed_image_id_does_not_replace_verified_config(self):
        self.record['image_id'] = 'sha256:' + 'b' * 64
        self.receipt.write_text(json.dumps(self.record))
        with self.assertRaisesRegex(ValueError, 'verified archive config'):
            self.validate()

    def test_config_cannot_claim_another_release_even_with_matching_hashes(self):
        self.config['config']['Labels']['org.opencontainers.image.version'] = 'release-v9.9.9'
        self.write()
        with self.assertRaisesRegex(ValueError, 'config labels'):
            self.validate()

    def test_wrong_platform_archive_is_rejected(self):
        self.config['architecture'] = 'arm64'
        self.write()
        with self.assertRaisesRegex(ValueError, 'wrong image platform'):
            self.validate()

    def test_corrupt_compressed_bytes_are_rejected(self):
        with self.path.open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'archive bytes differ'):
            self.validate()

    def test_size_gate_checks_actual_compressed_size(self):
        # Sparse files test the hard boundary without allocating large storage.
        for size in (0, subject.ASSET_LIMIT, subject.ASSET_LIMIT + 1):
            with self.path.open('wb') as stream:
                stream.truncate(size)
            with self.assertRaisesRegex(ValueError, 'smaller than 2 GiB'):
                subject.check_size(self.path)
        with self.path.open('wb') as stream:
            stream.truncate(subject.ASSET_LIMIT - 1)
        self.assertEqual(subject.check_size(self.path), subject.ASSET_LIMIT - 1)

    def test_history_is_selected_by_committed_contract(self):
        self.assertEqual(selection.delivery({}, 'old exact source'), 'historical')
        self.assertEqual(selection.delivery({'delivery': 'workbench-v1'}, 'new exact source'), 'workbench-v1')
        for value in ('historical', '', 'workbench-v2'):
            with self.assertRaises(selection.ManifestError):
                selection.delivery({'delivery': value}, 'source')
        self.assertEqual(selection.workbench_archive(VERSION, 'aarch64'), 'workbench-aarch64-v1.2.3-preview.20260928.2.tar.gz')


if __name__ == '__main__':
    unittest.main()
