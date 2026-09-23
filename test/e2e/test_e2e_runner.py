import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).with_name("e2e")
loader = importlib.machinery.SourceFileLoader("e2e_runner", str(MODULE))
spec = importlib.util.spec_from_loader(loader.name, loader)
runner = importlib.util.module_from_spec(spec)
loader.exec_module(runner)


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


if __name__ == "__main__":
    unittest.main()
