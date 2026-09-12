"""Exercise the actual Demo Python assertions, not a MicroVM or SDK E2E."""
import contextlib
import io
from pathlib import Path
import re
import types
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("demo_e2b.sh").read_text()


class DemoStateAssertions(unittest.TestCase):
    def exercise(self, phase, *, listed=True, status=0, state=None, data=None, list_after_ready=False):
        label = "forked child exec failed" if phase == "fanout" else "connect-with-migration-token failed"
        match = re.search(r'(?m)^py <<PY \|\| die "' + label + r'"\n(.*?)^PY$', SCRIPT, re.S)
        self.assertIsNotNone(match)
        code = match.group(1).replace("$CHILD", "fixture-child").replace("$SID", "fixture-source")
        code = code.replace("$MIG_TOKEN", "fixture-migration")
        values = types.SimpleNamespace(killed=False, migration=False, ready=False)

        class Sandbox:
            @staticmethod
            def list():
                visible = listed and (values.ready or not list_after_ready)
                return types.SimpleNamespace(next_items=lambda: [types.SimpleNamespace(sandbox_id="fixture-child")] if visible else [])

            @staticmethod
            def connect(identifier, **options):
                expected = "fixture-child" if phase == "fanout" else "fixture-source"
                if identifier != expected:
                    raise AssertionError("wrong sandbox identity")
                if phase == "migration":
                    if options != {"headers": {"X-Kuasar-Migration-Token": "fixture-migration"}}:
                        raise AssertionError("migration token was not supplied")
                    values.migration = True
                def run(_):
                    values.ready = status == 0
                    return types.SimpleNamespace(exit_code=status, stderr="fixture",
                        stdout="hello from before the snapshot" if state is None else state)
                command = types.SimpleNamespace(run=run)
                files = types.SimpleNamespace(read=lambda _: "written through the E2B Files API" if data is None else data)
                return types.SimpleNamespace(commands=command, files=files, kill=lambda: setattr(values, "killed", True))

        with patch.dict("sys.modules", {"e2b": types.SimpleNamespace(Sandbox=Sandbox)}), contextlib.redirect_stdout(io.StringIO()):
            exec(compile(code, "demo-" + phase, "exec"), {})
        return values

    def test_complete_state_succeeds(self):
        self.assertTrue(self.exercise("fanout").killed)
        self.assertTrue(self.exercise("migration").migration)

    def test_missing_child_is_rejected(self):
        with self.assertRaises(AssertionError):
            self.exercise("fanout", listed=False)

    def test_starting_child_becomes_listed_only_after_guest_readiness(self):
        self.assertTrue(self.exercise("fanout", list_after_ready=True).killed)

    def test_both_phases_reject_failed_or_changed_data(self):
        for phase in ("fanout", "migration"):
            for change in ({"status": 1}, {"state": "lost state"}, {"data": "lost Files data"}):
                with self.subTest(phase=phase, change=change), self.assertRaises(AssertionError):
                    self.exercise(phase, **change)


if __name__ == "__main__":
    unittest.main()
