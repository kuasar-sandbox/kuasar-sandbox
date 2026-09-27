import importlib.machinery
import importlib.util
import json
import os
import platform
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).with_name("e2e")
loader = importlib.machinery.SourceFileLoader("e2e_runner", str(MODULE))
spec = importlib.util.spec_from_loader(loader.name, loader)
runner = importlib.util.module_from_spec(spec)
loader.exec_module(runner)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ci/integration"))
prepare_spec = importlib.util.spec_from_file_location(
    "prepare_artifacts", ROOT / "ci/integration/prepare-artifacts.py")
prepare = importlib.util.module_from_spec(prepare_spec)
prepare_spec.loader.exec_module(prepare)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.old = runner.CASES
        self.tmp = tempfile.TemporaryDirectory()
        runner.CASES = Path(self.tmp.name)
        for name in ("basic.one.sh", "storage.one.sh", "storage.two.sh"):
            (runner.CASES / name).write_text("#!/bin/sh\n")

    def tearDown(self):
        runner.CASES = self.old
        self.tmp.cleanup()

    def args(self, **kw):
        defaults = dict(suite=[], include=[], exclude=[], all=False)
        defaults.update(kw)
        return type("Args", (), defaults)()

    def test_suite(self):
        self.assertEqual([p.name for p in runner.selected(self.args(suite=["storage"]))],
                         ["storage.one.sh", "storage.two.sh"])

    def test_include_exclude(self):
        args = self.args(include=["*.one.sh"], exclude=["storage.*"])
        self.assertEqual([p.name for p in runner.selected(args)], ["basic.one.sh"])

    def test_unknown_suite_fails(self):
        with self.assertRaises(SystemExit):
            runner.selected(self.args(suite=["missing"]))

    def test_known_but_empty_suite_fails_as_empty_selection(self):
        with self.assertRaisesRegex(SystemExit, "selection contains no cases"):
            runner.selected(self.args(suite=["image"]))

    def test_case_with_unapproved_suite_fails_discovery(self):
        (runner.CASES / "working-set.memory.sh").write_text("#!/bin/sh\n")
        with self.assertRaisesRegex(SystemExit, "unsupported suite in case file"):
            runner.discover()

    def test_all_does_not_hide_unknown_include(self):
        with self.assertRaisesRegex(SystemExit, 'matched no case'):
            runner.selected(self.args(all=True, include=['missing.sh']))

    def test_malformed_case_and_symlink_fail_discovery(self):
        for name, kind in (('case.sh', 'file'), ('basic..sh', 'file'),
                           ('basic.link.sh', 'symlink'), ('basic.dir.sh', 'directory')):
            path = runner.CASES / name
            with self.subTest(name=name):
                if kind == 'file': path.write_text('exit 0\n')
                elif kind == 'symlink': path.symlink_to(runner.CASES / 'basic.one.sh')
                else: path.mkdir()
                with self.assertRaisesRegex(SystemExit, 'invalid case file'):
                    runner.discover()
                if kind == 'directory': path.rmdir()
                else: path.unlink()

    def test_empty_selection_fails(self):
        with self.assertRaises(SystemExit):
            runner.selected(self.args(suite=["basic"], exclude=["basic.*"]))

    def test_run_can_keep_state_outside_prepared_workspace(self):
        with tempfile.TemporaryDirectory(prefix="e2e-prepared-") as directory:
            work = Path(directory) / "prepared"
            run_root = Path(directory) / "state"
            out_root = Path(directory) / "output"
            (work / "bin").mkdir(parents=True)
            (work / "test/e2e/lib").mkdir(parents=True)
            (work / "test/e2e/cases").mkdir()
            (work / "ARCH").write_text("x86_64\n")
            (runner.CASES / "basic.one.sh").write_text(
                '#!/bin/sh\nset -eu\n'
                f'[ "$E2E_WORKSPACE" = "{work}" ]\n'
                f'[ "$E2E_LIB" = "{work}/test/e2e/lib" ]\n'
                '[ "$E2E_ARCH" = x86_64 ]\n'
                f'[ "$WORK" = "{run_root}/basic.one.sh" ]\n'
                f'[ "$OUT" = "{out_root}/basic.one.sh" ]\n'
                'printf ok > "$OUT/result"\n')
            shutil.copyfile(runner.CASES / 'basic.one.sh', work / 'test/e2e/cases/basic.one.sh')
            runner.workspace.seal(work, {'arch': platform.machine(), 'prepared_cases': ['basic.one.sh']})
            args = self.args(include=["basic.one.sh"], workdir=str(work), arch=platform.machine(),
                             run_root=str(run_root), out_root=str(out_root), result=None)
            self.assertEqual(runner.cmd_run(args), 0)
            self.assertEqual((out_root / "basic.one.sh/result").read_text(), "ok")
            self.assertFalse((work / "run").exists())
            self.assertFalse((work / "out").exists())


class PreparedRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='e2e-contract-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.release = self.root / 'release'
        self.work = self.root / 'prepared'
        for directory in ('bin', 'test/e2e/cases', 'test/e2e/lib'):
            (self.release / directory).mkdir(parents=True)
        shutil.copyfile(MODULE, self.release / 'test/e2e/e2e')
        shutil.copyfile(MODULE.parent / 'lib/common.sh', self.release / 'test/e2e/lib/common.sh')
        shutil.copyfile(MODULE.parent / 'lib/workspace.py', self.release / 'test/e2e/lib/workspace.py')
        helpers = self.release / 'test/e2e/helpers' / platform.machine()
        helpers.mkdir(parents=True)
        (helpers / 'helpers.json').write_text(json.dumps({'arch': platform.machine(), 'helpers': {}}))
        (self.release / 'test/e2e/cases/basic.fixture.sh').write_text('exit 0\n')
        self.args = dict(suite=[], include=['basic.fixture.sh'], exclude=[], all=False,
                         arch=platform.machine(), release_dir=str(self.release), workdir=str(self.work),
                         run_root=str(self.root / 'run'), out_root=str(self.root / 'out'), result=None)

    def args_with(self, **values):
        return type('Args', (), self.args | values)()

    def test_prepare_and_run_use_only_release_inputs(self):
        case = self.release / 'test/e2e/cases/basic.fixture.sh'
        case.write_text('set -eu\n[ -z "${E2E_IMAGE:-}" ]\n[ -z "${ZOT_BIN:-}" ]\n'
                        '[ "$E2E_LIB" = "$E2E_WORKSPACE/test/e2e/lib" ]\n'
                        'printf prepared > "$OUT/result"\n')
        before = runner.workspace.files(self.release)
        self.assertEqual(runner.cmd_prepare(self.args_with()), 0)
        # An ambient checkout/fixture must never replace the prepared case.
        case.write_text('exit 81\n')
        with patch.dict(os.environ, {'E2E_CASES_DIR': str(case.parent), 'E2E_LIB': '/host/lib',
                                     'E2E_IMAGE': 'host-image', 'ZOT_BIN': '/host/zot'}):
            self.assertEqual(runner.cmd_run(self.args_with()), 0)
        self.assertEqual((self.root / 'out/basic.fixture.sh/result').read_text(), 'prepared')
        report = json.loads((self.root / 'out/result.json').read_text())
        self.assertEqual(report['cases'], ['basic.fixture.sh'])
        self.assertEqual(report['conclusion'], 'success')
        self.assertEqual(report['timings'][0]['exit_code'], 0)
        self.assertEqual(runner.workspace.verify(self.work)['files']['test/e2e/cases/basic.fixture.sh'],
                         before['test/e2e/cases/basic.fixture.sh'])

    def test_failure_remains_failure_in_return_code_and_result(self):
        (self.release / 'test/e2e/cases/basic.fixture.sh').write_text('exit 23\n')
        runner.cmd_prepare(self.args_with())
        self.assertEqual(runner.cmd_run(self.args_with()), 1)
        report = json.loads((self.root / 'out/result.json').read_text())
        self.assertEqual(report['conclusion'], 'failure')
        self.assertEqual(report['timings'][0]['exit_code'], 23)

    def test_input_tampering_fails_before_case_execution(self):
        runner.cmd_prepare(self.args_with())
        (self.work / 'test/e2e/cases/basic.fixture.sh').write_text('exit 0 # substituted\n')
        with self.assertRaisesRegex(ValueError, 'contents changed'):
            runner.cmd_run(self.args_with())
        self.assertFalse((self.root / 'run').exists())

    def test_case_cannot_modify_prepared_inputs_and_report_pass(self):
        (self.release / 'test/e2e/cases/basic.fixture.sh').write_text('touch "$E2E_WORKSPACE/undeclared"\n')
        runner.cmd_prepare(self.args_with())
        with self.assertRaisesRegex(ValueError, 'contents changed'):
            runner.cmd_run(self.args_with())
        report = json.loads((self.root / 'out/result.json').read_text())
        self.assertEqual(report['conclusion'], 'failure')

    def test_unprepared_case_and_state_under_inputs_fail(self):
        (self.release / 'test/e2e/cases/basic.unprepared.sh').write_text('exit 0\n')
        runner.cmd_prepare(self.args_with())
        with self.assertRaisesRegex(ValueError, 'were not prepared'):
            runner.cmd_run(self.args_with(include=['basic.unprepared.sh']))
        with self.assertRaisesRegex(ValueError, 'outside immutable'):
            runner.cmd_run(self.args_with(run_root=str(self.work / 'run')))

    def test_preparation_rejects_wrong_helper_architecture_before_fixtures(self):
        import struct
        helpers = self.release / 'test/e2e/helpers' / platform.machine()
        header = bytearray(64)
        header[:7] = b'\x7fELF\x02\x01\x01'
        struct.pack_into('<H', header, 18, 183 if platform.machine() == 'x86_64' else 62)
        (helpers / 'zot').write_bytes(header)
        (helpers / 'zot').chmod(0o755)
        (helpers / 'helpers.json').write_text(json.dumps({'arch': platform.machine(),
            'helpers': {'zot': {'sha256': runner.workspace.digest(helpers / 'zot')}}}))
        with patch.object(runner.workspace, 'prepare_fixtures') as prepare:
            with self.assertRaisesRegex(ValueError, 'wrong architecture'):
                runner.cmd_prepare(self.args_with())
        prepare.assert_not_called()

    def test_prepare_rejects_host_symlinks(self):
        (self.release / 'bin/host-product').symlink_to('/bin/true')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            runner.cmd_prepare(self.args_with())


class GuestFixturePathTests(unittest.TestCase):
    def test_guest_uses_only_normalized_prepared_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized = root / "test/e2e/lib/guest-runtime/fixture.py"
            normalized.parent.mkdir(parents=True)
            normalized.write_text("# pinned helper\n")
            self.assertEqual(runner.workspace.fixture_helper(root, "guest-runtime", "fixture.py"), normalized)

    def test_missing_normalized_helper_cannot_use_legacy_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "test/e2e/guest-runtime/fixture.py"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("# old helper\n")
            with self.assertRaisesRegex(ValueError, "missing prepared guest-runtime"):
                runner.workspace.fixture_helper(root, "guest-runtime", "fixture.py")

    def test_normalized_helper_rejects_symlinks_and_directories(self):
        for kind in ("symlink", "dangling", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "old.py"
                target.write_text("# old helper\n")
                helper = root / "test/e2e/lib/guest-runtime/fixture.py"
                helper.parent.mkdir(parents=True)
                if kind == "directory":
                    helper.mkdir()
                else:
                    helper.symlink_to(target if kind == "symlink" else root / "missing")
                with self.assertRaisesRegex(ValueError, "missing prepared guest-runtime"):
                    runner.workspace.fixture_helper(root, "guest-runtime", "fixture.py")


if __name__ == "__main__":
    unittest.main()
