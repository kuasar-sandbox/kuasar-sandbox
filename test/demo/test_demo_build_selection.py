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
    def test_readme_examples_use_published_template_ids(self):
        root = Path(__file__).resolve().parents[2]
        for name in ("README.md", "README_zh.md"):
            with self.subTest(name=name):
                text = (root / name).read_text()
                self.assertIn('os.environ["TEMPLATE_ID"]', text)
                self.assertNotIn("Sandbox.create(build.template_id", text)

    def test_image_prepares_sdk_user_before_copy(self):
        match = re.search(r"(?ms)^BUILD_RESULT=.*?<<'PY'\n(.*?)^PY$", SCRIPT)
        self.assertIsNotNone(match)
        calls = [node for node in ast.walk(ast.parse(match.group(1)))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        setup = [call for call in calls if call.func.attr == "run_cmd" and call.args
                 and isinstance(call.args[0], ast.Constant) and "useradd" in call.args[0].value]
        self.assertEqual(len(setup), 1)
        self.assertEqual(setup[0].keywords, [])
        self.assertEqual(setup[0].func.value.func.attr, "set_user")
        self.assertEqual(ast.literal_eval(setup[0].func.value.args[0]), "root")
        users = [call for call in calls if call.func.attr == "set_user"]
        self.assertEqual(len(users), 2)
        self.assertTrue(any(ast.literal_eval(call.args[0]) == "user"
                            and call.func.value is setup[0] for call in users))
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
