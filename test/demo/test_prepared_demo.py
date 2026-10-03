"""Exercise the prepared Demo adapter without Docker, root or a product build."""
import argparse
import importlib.util
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('prepared_demo', Path(__file__).with_name('prepared.py'))
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


class PreparedDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / 'prepared'
        self.root.mkdir()
        self.arch = platform.machine()
        self.sdk = self.root / 'fixtures/demo-sdk'
        (self.sdk / 'e2b').mkdir(parents=True)
        (self.sdk / 'e2b/__init__.py').write_text('')
        metadata = self.sdk / 'e2b-2.25.1.dist-info'
        metadata.mkdir()
        (metadata / 'METADATA').write_text('Metadata-Version: 2.1\nName: e2b\nVersion: 2.25.1\n')
        (self.root / 'test/demo').mkdir(parents=True)
        (self.root / 'test/demo/requirements.txt').write_text('e2b==2.25.1\n')
        for script in ('demo_prep.sh', 'demo_e2b.sh'):
            (self.root / 'test/demo' / script).write_text('#!/bin/bash\nexit 0\n')
        helpers = self.root / 'fixtures/bin'
        helpers.mkdir()
        records = {}
        for name in ('zot', 'versitygw'):
            header = bytearray(64)
            header[:7] = b'\x7fELF\x02\x01\x01'
            struct.pack_into('<H', header, 18, {'x86_64': 62, 'aarch64': 183}[self.arch])
            path = helpers / name
            path.write_bytes(header)
            path.chmod(0o755)
            records[name] = {'sha256': demo.workspace.digest(path), 'source_sha': 'a' * 40}
        self.provenance = {
            'schema': 1, 'arch': self.arch, 'prepared_cases': [demo.CASE], 'helpers': records,
            'python': {'demo': {'directory': 'fixtures/demo-sdk',
                                 'python': f'{sys.version_info.major}.{sys.version_info.minor}'}},
            'images': {'python': {'image_id': 'sha256:' + 'a' * 64,
                                  'platform': 'linux/' + {'x86_64': 'amd64', 'aarch64': 'arm64'}[self.arch],
                                  'archive': 'images/python.tar', 'sha256': 'b' * 64}},
        }
        self.seal()
        self.data = self.directory / 'kuasar-demo-test'

    def seal(self):
        demo.workspace.seal(self.root, self.provenance)

    def args(self, command='run', **values):
        return argparse.Namespace(command=command, workdir=self.root, data_dir=self.data,
                                  quick=values.get('quick', False), pause=values.get('pause', False))

    def test_verified_inputs_and_isolated_sdk_launcher(self):
        root, provenance = demo.demo_inputs(self.root)
        self.assertEqual(provenance['arch'], self.arch)
        launcher = demo.python_launcher(root, self.directory / 'demo-python')
        value = subprocess.check_output([str(launcher), '-c', 'import e2b; print(e2b.__file__)'], text=True)
        self.assertTrue(value.strip().startswith(str(self.sdk)))
        self.assertEqual(launcher.stat().st_mode & 0o777, 0o700)
        demo.workspace.verify(root)
        with self.assertRaises(FileExistsError):
            demo.python_launcher(root, launcher)

    def test_launcher_does_not_import_mutable_cwd(self):
        launcher = demo.python_launcher(self.root, self.directory / 'demo-python')
        (self.directory / 'e2b.py').write_text('raise RuntimeError("foreign SDK")\n')
        subprocess.run([str(launcher), '-c', 'import e2b'], cwd=self.directory, check=True)

    def test_tampered_input_is_rejected(self):
        (self.sdk / 'e2b/__init__.py').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'contents changed'):
            demo.demo_inputs(self.root)

    def test_changed_permission_is_rejected(self):
        (self.sdk / 'e2b/__init__.py').chmod(0o700)
        with self.assertRaisesRegex(ValueError, 'permissions changed'):
            demo.demo_inputs(self.root)

    def test_missing_seal_is_rejected(self):
        (self.root / 'provenance.json').unlink()
        with self.assertRaisesRegex(ValueError, 'provenance'):
            demo.demo_inputs(self.root)

    def test_wrong_native_architecture_is_rejected(self):
        with mock.patch.object(demo.platform, 'machine', return_value='unsupported'):
            with self.assertRaisesRegex(ValueError, 'native architecture'):
                demo.demo_inputs(self.root)

    def test_case_must_have_been_prepared(self):
        self.provenance['prepared_cases'] = ['image.flatten.sh']
        self.seal()
        with self.assertRaisesRegex(ValueError, 'prepare basic.demo.sh'):
            demo.demo_inputs(self.root)

    def test_wrong_sdk_python_is_rejected(self):
        self.provenance['python']['demo']['python'] = '3.0'
        self.seal()
        with self.assertRaisesRegex(ValueError, 'Python version'):
            demo.demo_inputs(self.root)

    def test_wrong_sdk_location_is_rejected(self):
        self.provenance['python']['demo']['directory'] = '/usr/lib/python3'
        self.seal()
        with self.assertRaisesRegex(ValueError, 'missing prepared Demo SDK'):
            demo.demo_inputs(self.root)

    def test_helper_must_be_declared(self):
        del self.provenance['helpers']['zot']
        self.seal()
        with self.assertRaisesRegex(ValueError, 'prepared Demo helper: zot'):
            demo.demo_inputs(self.root)

    def test_helper_elf_architecture_is_checked(self):
        path = self.root / 'fixtures/bin/zot'
        data = bytearray(path.read_bytes())
        struct.pack_into('<H', data, 18, 0)
        path.write_bytes(data)
        self.provenance['helpers']['zot']['sha256'] = demo.workspace.digest(path)
        self.seal()
        with self.assertRaisesRegex(ValueError, 'wrong architecture'):
            demo.demo_inputs(self.root)

    def test_wrong_image_platform_is_rejected(self):
        self.provenance['images']['python']['platform'] = 'linux/wrong'
        self.seal()
        with self.assertRaisesRegex(ValueError, 'prepared Demo image'):
            demo.demo_inputs(self.root)

    def test_mutable_paths_cannot_overlap_inputs(self):
        for path in (self.root, self.root / 'output', self.directory):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'separate'):
                demo.separate_path(path, self.root)
        alias = self.directory / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'canonical'):
            demo.separate_path(alias / 'output', self.root)

    def test_environment_does_not_inherit_short_diagnostic_or_foreign_inputs(self):
        with mock.patch.dict(os.environ, {'DEMO_QUICKSTART': '1', 'DEMO_NETDIAG': '1',
                 'DEMO_PAUSE': '1', 'DOCKER_HOST': 'tcp://foreign', 'PYTHONPATH': '/foreign',
                 'E2E_IMAGE': 'foreign', 'VGW_BIN': '/foreign', 'REGISTRY_PASS': 'not-for-child'}):
            env = demo.demo_environment(self.root, self.provenance, self.data)
        for key in ('DEMO_QUICKSTART', 'DEMO_NETDIAG', 'DEMO_PAUSE', 'DOCKER_HOST', 'PYTHONPATH', 'REGISTRY_PASS'):
            self.assertNotIn(key, env)
        self.assertEqual(env['E2E_IMAGE'], self.provenance['images']['python']['image_id'])
        self.assertEqual(env['VGW_BIN'], str(self.root / 'fixtures/bin/versitygw'))
        self.assertEqual(env['DEMO_KEEP'], '1')

    def execute_mocked(self, args, codes):
        with mock.patch.object(demo.os, 'geteuid', return_value=0), \
             mock.patch.object(demo.workspace, 'load_images') as load, \
             mock.patch.object(demo, 'python_launcher', return_value=self.directory / 'python'), \
             mock.patch.object(demo.subprocess, 'call', side_effect=codes) as call:
            status = demo.execute(args)
        return status, load, call

    def test_quick_and_full_are_explicit_and_use_existing_scripts(self):
        for quick in (False, True):
            status, load, call = self.execute_mocked(self.args(quick=quick), [0, 0])
            self.assertEqual(status, 0)
            self.assertEqual(load.call_args.args[1]['images'], self.provenance['images'])
            self.assertEqual(call.call_count, 2)
            self.assertEqual(call.call_args.args[0][-1], str(self.root / 'test/demo/demo_e2b.sh'))
            self.assertEqual('DEMO_QUICKSTART' in call.call_args.kwargs['env'], quick)
            self.assertNotIn('DEMO_NETDIAG', call.call_args.kwargs['env'])

    def test_failed_prepare_does_not_run_demo(self):
        status, _, call = self.execute_mocked(self.args(), [7])
        self.assertEqual(status, 7)
        self.assertEqual(call.call_count, 1)

    def test_demo_failure_exit_is_preserved(self):
        status, _, _ = self.execute_mocked(self.args(), [0, 9])
        self.assertEqual(status, 9)

    def test_stop_and_reset_delegate_without_loading_images(self):
        for action in ('stop', 'reset'):
            status, load, call = self.execute_mocked(self.args(action), [3])
            self.assertEqual(status, 3)
            load.assert_not_called()
            self.assertEqual(call.call_args.args[0], ['bash', str(self.root / 'test/demo/demo_prep.sh'), action])

    def test_pause_requires_a_terminal(self):
        with mock.patch.object(demo.os, 'geteuid', return_value=0), \
             mock.patch.object(demo.sys.stdin, 'isatty', return_value=False), \
             mock.patch.object(demo.workspace, 'load_images') as load:
            with self.assertRaisesRegex(ValueError, 'interactive terminal'):
                demo.execute(self.args(pause=True))
            load.assert_not_called()

    def test_run_verifies_inputs_after_failure(self):
        with mock.patch.object(demo.os, 'geteuid', return_value=0), \
             mock.patch.object(demo.workspace, 'load_images', side_effect=RuntimeError('load failed')), \
             mock.patch.object(demo.workspace, 'verify', wraps=demo.workspace.verify) as verify:
            with self.assertRaisesRegex(RuntimeError, 'load failed'):
                demo.execute(self.args())
            self.assertEqual(verify.call_count, 2)

    def test_public_case_clears_modes_and_shares_launcher(self):
        case = Path(__file__).resolve().parents[1] / 'e2e/platform/cases/basic.demo.sh'
        text = case.read_text()
        self.assertIn('unset DEMO_QUICKSTART DEMO_NETDIAG DEMO_PAUSE', text)
        self.assertIn('prepared.py" python --workdir', text)
        self.assertIn('Demo preparation adopted a foreign listener', text)
        self.assertIn('bash "$DEMO_DIR/demo_e2b.sh"', text)


if __name__ == '__main__':
    unittest.main()
