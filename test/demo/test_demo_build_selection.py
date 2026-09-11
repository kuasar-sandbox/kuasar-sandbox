"""Check the Demo's real build selection/readback snippets without a VM."""
import ast
import contextlib
import io
import json
from pathlib import Path
import re
import unittest
from unittest.mock import mock_open, patch

SCRIPT = Path(__file__).with_name("demo_e2b.sh").read_text()


class DemoBuildSelection(unittest.TestCase):
    def test_image_prepares_sdk_user_before_copy(self):
        match = re.search(r"(?ms)^BUILD_RESULT=.*?<<'PY'\n(.*?)^PY$", SCRIPT)
        self.assertIsNotNone(match)
        calls = [node for node in ast.walk(ast.parse(match.group(1)))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        setup = [call for call in calls if call.func.attr == "run_cmd" and call.args
                 and isinstance(call.args[0], ast.Constant) and "useradd" in call.args[0].value]
        self.assertEqual(len(setup), 1)
        self.assertEqual({key.arg: ast.literal_eval(key.value) for key in setup[0].keywords},
                         {"user": "root"})
        self.assertIn("1000:1000", setup[0].args[0].value)
        self.assertTrue(all(call.lineno > setup[0].lineno for call in calls if call.func.attr == "copy"))

    def test_sdk_build_uses_command_driven_auto_target(self):
        match = re.search(r"(?ms)^BUILD_RESULT=.*?<<'PY'\n(.*?)^PY$", SCRIPT)
        self.assertIsNotNone(match)
        calls = [node for node in ast.walk(ast.parse(match.group(1)))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        builds = [call for call in calls if isinstance(call.func.value, ast.Name)
                  and call.func.value.id == "Template" and call.func.attr == "build"]
        self.assertEqual(len(builds), 1)
        self.assertNotIn("headers", [keyword.arg for keyword in builds[0].keywords])
        commands = [call for call in calls if call.func.attr == "set_start_cmd"]
        self.assertEqual(len(commands), 1)
        self.assertTrue(ast.literal_eval(commands[0].args[0]).strip())

    def readback(self, **changes):
        match = re.search(r'(?ms)^TEMPLATE=".*?<<\'PY\'\n(.*?)^PY$', SCRIPT)
        self.assertIsNotNone(match)
        response = {"buildID": "fixture-build", "status": "ready", "profile": "e2b",
                    "target": None, "kind": "snp", "templateID": "e2b-snp-fixture"}
        response.update(changes)
        output = io.StringIO()
        with patch("builtins.open", mock_open(read_data=json.dumps(response))), \
                patch("sys.argv", ["-", "fixture.json", "fixture-build"]), \
                contextlib.redirect_stdout(output):
            exec(compile(match.group(1), "demo-build-readback", "exec"), {})
        return output.getvalue().strip()

    def test_uses_published_snapshot_identity(self):
        self.assertEqual(self.readback(), "e2b-snp-fixture")

    def test_rejects_wrong_build_or_non_snapshot_result(self):
        for change in ({"buildID": "another-build"}, {"status": "building"},
                       {"profile": "bare"}, {"target": {"kind": "image"}},
                       {"kind": "img"}, {"templateID": "transient-fixture"},
                       {"templateID": ""}):
            with self.subTest(change=change), self.assertRaises(AssertionError):
                self.readback(**change)


if __name__ == "__main__":
    unittest.main()
