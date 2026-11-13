#!/usr/bin/env python3
"""Private volume ownership and interrupted Quick Start initialization."""
import fcntl
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[2] / 'node-infra/image/rootfs/usr/libexec/node-infra/quickstart'
loader = importlib.machinery.SourceFileLoader('quickstart', str(SCRIPT))
qs = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
# Do not write test bytecode in the image build context.
exec(compile(SCRIPT.read_bytes(), str(SCRIPT), 'exec'), qs.__dict__)


class QuickStartInitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.data, self.runtime = base / 'volume', base / 'run'
        self.data.mkdir()
        self.runtime.mkdir()
        self.patch = mock.patch.multiple(qs, DATA=self.data, STATE=self.data / 'quickstart',
                                         PENDING=self.data / '.ks-quickstart-init', RUN=self.runtime)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        # The actual CLI also enters with this umask before touching a file.
        self.addCleanup(os.umask, os.umask(0o077))

    def test_conflicting_inputs_rejected_before_native_setup(self):
        mounts = ['1 2 0:1 / ' + str(self.data) + ' rw - tmpfs tmpfs rw']
        with mock.patch.object(Path, 'read_text', side_effect=lambda: '\n'.join(mounts)):
            with mock.patch.object(Path, 'is_char_device', return_value=False):
                with self.assertRaisesRegex(ValueError, '/dev/kvm'):
                    qs.check_inputs()
            mounts.append(f'2 3 0:1 / {qs.PENDING}/publications/site rw - tmpfs tmpfs rw')
            with self.assertRaisesRegex(ValueError, 'nested mount'):
                qs.check_inputs()
            mounts[-1] = '2 3 0:1 / /etc/node-conductor/conf.d rw - tmpfs tmpfs rw'
            with mock.patch.object(Path, 'is_char_device', return_value=True):
                with self.assertRaisesRegex(ValueError, 'explicit site configuration'):
                    qs.check_inputs()
        self.assertFalse(any(self.data.iterdir()))

    def test_preserves_an_unrecognized_volume_or_pending_directory(self):
        for location in [self.data, qs.PENDING]:
            with self.subTest(location=location):
                location.mkdir(exist_ok=True)
                unrelated = location / 'customer-data'
                unrelated.write_text('keep')
                with self.assertRaisesRegex(ValueError, 'preserving'):
                    qs.initialize()
                self.assertEqual(unrelated.read_text(), 'keep')
                unrelated.unlink()

    def test_competing_volume_user_cannot_initialize(self):
        lock = os.open(self.data, os.O_DIRECTORY)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        with mock.patch.object(qs, 'check_inputs'), self.assertRaisesRegex(ValueError, 'already in use'):
            qs.prepare()
        self.assertFalse(any(self.data.iterdir()))

    def test_interrupted_seed_build_retains_keys_and_retries_locally(self):
        def native(*argv, **options):
            result = ''
            if argv[0] == 'e2b-key-ctl':
                # None of these commands should pass secrets in their argument list.
                self.assertEqual(len(argv), 2)
                result = 'e2b_' + secrets.token_hex(36) if argv[1] == 'gen-apikey' else secrets.token_hex(32)
            return subprocess.CompletedProcess(argv, 0, result + '\n')

        root = 'b' * 64
        def seed(stage, key):
            directory = qs.location_directory(stage)
            directory.mkdir(parents=True)
            (directory / (root + '.bundle')).write_bytes(b'simulated native Bundle')
            qs.write(stage / 'template-id', qs.template_id(root) + '\n')

        with mock.patch.object(qs, 'run', side_effect=native):
            with mock.patch.object(qs, 'write_seed', side_effect=ValueError('simulated interruption')):
                with self.assertRaisesRegex(ValueError, 'simulated interruption'):
                    qs.initialize()
            self.assertFalse(qs.STATE.exists())
            secrets_on_disk = {name: (qs.PENDING / name).read_bytes() for name in
                               ('manifest-key', 'encryption-key', 'api-key')}
            with mock.patch.object(qs, 'write_seed', side_effect=seed) as build:
                qs.initialize()
                self.assertFalse(qs.PENDING.exists())
                qs.initialize()
                build.assert_called_once()
            for name, data in secrets_on_disk.items():
                self.assertEqual((qs.STATE / name).read_bytes(), data)
                self.assertEqual((qs.STATE / name).stat().st_mode & 0o777, 0o600)
            qs.configure()
            config = json.loads((qs.RUN / 'quickstart/conductor.yaml').read_text())
            self.assertEqual(config['checkpoint']['mode'], 'bundle')
            self.assertTrue(config['checkpoint']['remote']['manifest'])
            self.assertEqual(config['checkpoint']['remote']['ref_location_parent'],
                             (qs.STATE / 'publications').as_uri())
            (qs.STATE / 'template-id').write_text('bare-img-eA')
            qs.initialize()
            with self.assertRaisesRegex(ValueError, 'damaged Quick Start template reference'):
                qs.configure()
            self.assertEqual((qs.STATE / 'api-key').read_bytes(), secrets_on_disk['api-key'])


if __name__ == '__main__':
    unittest.main()
