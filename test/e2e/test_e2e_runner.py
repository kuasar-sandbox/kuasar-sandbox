import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

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
            (work / "ARCH").write_text("x86_64\n")
            (runner.CASES / "basic.one.sh").write_text(
                '#!/bin/sh\nset -eu\n'
                f'[ "$E2E_WORKSPACE" = "{work}" ]\n'
                f'[ "$E2E_LIB" = "{work}/test/e2e/lib" ]\n'
                '[ "$E2E_ARCH" = x86_64 ]\n'
                f'[ "$WORK" = "{run_root}/basic.one.sh" ]\n'
                f'[ "$OUT" = "{out_root}/basic.one.sh" ]\n'
                'printf ok > "$OUT/result"\n')
            args = self.args(include=["basic.one.sh"], workdir=str(work), arch="x86_64",
                             run_root=str(run_root), out_root=str(out_root))
            self.assertEqual(runner.cmd_run(args), 0)
            self.assertEqual((out_root / "basic.one.sh/result").read_text(), "ok")
            self.assertFalse((work / "run").exists())
            self.assertFalse((work / "out").exists())


class SelectedEntryModeTests(unittest.TestCase):
    def test_rewritten_case_may_be_0644_but_legacy_runner_must_be_executable(self):
        with tempfile.TemporaryDirectory(prefix="kuasar-entry-modes-") as directory:
            workspace = Path(directory)
            rewritten = workspace / "test/e2e/connector/cases/network.tap.sh"
            rewritten.parent.mkdir(parents=True)
            rewritten.write_text("#!/bin/sh\nexit 0\n")
            rewritten.chmod(0o644)
            prepare.validate_selected_entry_modes(
                workspace, {"cases": ["test/e2e/connector/cases/network.tap.sh"]})

            legacy = workspace / "test/e2e/sandboxer/run_all.sh"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("#!/bin/sh\nexit 0\n")
            legacy.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "legacy test entry is not executable"):
                prepare.validate_selected_entry_modes(
                    workspace, {"cases": ["test/e2e/sandboxer/run_all.sh"]})

            legacy.chmod(0o755)
            prepare.validate_selected_entry_modes(
                workspace, {"cases": ["test/e2e/sandboxer/run_all.sh"]})


class GuestFixturePathTests(unittest.TestCase):
    def test_migrated_guest_uses_normalized_component_library(self):
        with tempfile.TemporaryDirectory(prefix="kuasar-guest-fixture-") as directory:
            workspace = Path(directory)
            migrated = workspace / "test/e2e/lib/guest-runtime/fixture.py"
            migrated.parent.mkdir(parents=True)
            migrated.write_text("# prepared fixture\n")
            plan = {"candidate_cases": {"guest-runtime": ["image.flatten.sh"]}}
            self.assertEqual(prepare.guest_fixture_script(plan, workspace), migrated)

    def test_unmigrated_guest_keeps_legacy_overlay_path(self):
        with tempfile.TemporaryDirectory(prefix="kuasar-guest-fixture-") as directory:
            workspace = Path(directory)
            legacy = workspace / "test/e2e/guest-runtime/fixture.py"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("# legacy fixture\n")
            self.assertEqual(prepare.guest_fixture_script({}, workspace), legacy)

    def test_selected_guest_fixture_must_be_a_real_file(self):
        with tempfile.TemporaryDirectory(prefix="kuasar-guest-fixture-") as directory:
            workspace = Path(directory)
            target = workspace / "fixture.py"
            target.write_text("# target\n")
            migrated = workspace / "test/e2e/lib/guest-runtime/fixture.py"
            migrated.parent.mkdir(parents=True)
            migrated.symlink_to(target)
            plan = {"candidate_cases": {"guest-runtime": ["image.flatten.sh"]}}
            with self.assertRaisesRegex(ValueError, "missing prepared guest-runtime fixture helper"):
                prepare.guest_fixture_script(plan, workspace)


if __name__ == "__main__":
    unittest.main()
