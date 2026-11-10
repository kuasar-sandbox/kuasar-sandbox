"""Public node-infra downloads never borrow publisher credentials or bad bytes."""
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'published_download', ROOT / 'ci/integration/check-published-download.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
PAYLOAD = b'public immutable asset bytes\n'
ASSET = dict(id=17, size=len(PAYLOAD), digest='sha256:' + hashlib.sha256(PAYLOAD).hexdigest())


class AnonymousDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / 'asset.tar.gz'
        self.environment = dict(PATH='/usr/bin:/bin', GH_TOKEN='must-not-pass',
                                GITHUB_TOKEN='must-not-pass-either',
                                GITHUB_API_URL='https://override.invalid')

    def transport(self, payload, *, fail=False):
        def run(command, *, env, check):
            self.assertEqual(command[:2], ['curl', '--disable'])
            self.assertEqual(command[-1],
                             'https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox/releases/assets/17')
            self.assertNotIn('GH_TOKEN', env)
            self.assertNotIn('GITHUB_TOKEN', env)
            self.assertNotIn('Authorization', ' '.join(command))
            self.assertIn('Accept: application/octet-stream', command)
            self.assertTrue(check)
            output = Path(command[command.index('--output') + 1])
            self.assertEqual(output.parent, self.destination.parent)
            self.assertNotEqual(output, self.destination)
            output.write_bytes(payload)
            if fail:
                raise subprocess.CalledProcessError(22, command)
            return subprocess.CompletedProcess(command, 0)
        return run

    def test_anonymous_fixed_id_download_verifies_and_installs_bytes(self):
        with patch.object(check.subprocess, 'run', side_effect=self.transport(PAYLOAD)) as run:
            check.download_anonymous_asset(ASSET, self.destination, env=self.environment)
        run.assert_called_once()
        self.assertEqual(self.destination.read_bytes(), PAYLOAD)
        self.assertEqual(list(self.root.iterdir()), [self.destination])
        self.assertEqual(self.environment['GH_TOKEN'], 'must-not-pass')

    def test_wrong_size_or_same_size_corruption_never_replace_destination(self):
        for payload, message in ((PAYLOAD[:-1], 'size mismatch'),
                                 (b'X' + PAYLOAD[1:], 'digest mismatch')):
            with self.subTest(message=message):
                self.destination.write_bytes(b'existing local file')
                with patch.object(check.subprocess, 'run', side_effect=self.transport(payload)):
                    with self.assertRaisesRegex(ValueError, message):
                        check.download_anonymous_asset(ASSET, self.destination, env=self.environment)
                self.assertEqual(self.destination.read_bytes(), b'existing local file')
                self.assertEqual(list(self.root.iterdir()), [self.destination])

    def test_failed_transport_cleans_partial_without_authenticated_fallback(self):
        with patch.object(check.subprocess, 'run',
                          side_effect=self.transport(PAYLOAD[:5], fail=True)) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                check.download_anonymous_asset(ASSET, self.destination, env=self.environment)
        run.assert_called_once()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_native_metadata_stops_before_network(self):
        for field, value in (('id', True), ('id', '../other'), ('size', 0), ('digest', 'sha256:bad')):
            with self.subTest(field=field, value=value):
                with patch.object(check.subprocess, 'run') as run:
                    with self.assertRaisesRegex(ValueError, 'native identity, size or digest'):
                        check.download_anonymous_asset(dict(ASSET, **{field: value}),
                                                       self.destination, env=self.environment)
                run.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
