"""Check the Demo's real build selection/readback snippets without a VM."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
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

    def test_quick_start_is_a_prefix_not_a_second_integration_flow(self):
        branch = SCRIPT.index('if [ -n "${DEMO_QUICKSTART:-}" ]; then')
        fanout = SCRIPT.index('banner "Paused state -> template')
        self.assertLess(branch, fanout)
        body = SCRIPT[branch:fanout]
        self.assertIn('Sandbox.connect("$SID").kill()', body)
        self.assertIn('quick start complete', body)
        self.assertIn('exit 0', body)

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


class DemoImagePreparation(unittest.TestCase):
    def prepare(self, reference, **changes):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker = root / "docker"
            docker.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
a = sys.argv[1:]
r = Path(os.environ["LOG_DIR"])
with (r / "calls").open("a") as f: f.write(json.dumps(a) + "\\n")
identity = "sha256:" + "a" * 64
destination = "registry.test/test/base:sha-" + "a" * 64
if a[:2] == ["image", "inspect"]:
    if a[3] == "{{.Id}} {{.Os}}/{{.Architecture}}":
        if os.environ.get("MISSING") and not (r / "pulled").exists(): sys.exit(1)
        print(identity, os.environ.get("PLATFORM", "linux/amd64"))
    elif a[3] == "{{.Id}}": print(os.environ.get("DEST_ID", identity))
    else: print("registry.test/test/base@sha256:" + "b" * 64)
elif a[0] == "pull":
    if a[-1] == destination:
        if not os.environ.get("EXISTS") and not (r / "pushed").exists():
            print("manifest unknown", file=sys.stderr); sys.exit(1)
    else: (r / "pulled").touch()
elif a[0] == "tag":
    assert a[1] == identity, "mutable input was resolved again instead of using its image ID"
elif a[0] == "push": (r / "pushed").touch()
else: raise AssertionError(a)
''')
            docker.chmod(0o755)
            curl = root / "curl"
            curl.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
a = sys.argv[1:]
r = Path(os.environ["LOG_DIR"])
exists = os.environ.get("EXISTS") or (r / "pushed").exists()
body = {"config":{"digest":os.environ.get("DEST_ID", "sha256:" + "a" * 64)}} if exists else {"errors":[{"code":"MANIFEST_UNKNOWN"}]}
Path(a[a.index("-o") + 1]).write_text(json.dumps(body))
Path(a[a.index("-D") + 1]).write_text("Docker-Content-Digest: sha256:" + "b" * 64 + "\\r\\n")
print("200" if exists else "404")
''')
            curl.chmod(0o755)
            prep = Path(__file__).with_name("demo_prep.sh").read_text()
            # Run the actual input resolution and publish/readback path, with
            # only Docker and the owned Registry transport replaced by fixtures.
            block = prep[prep.index('SOURCE_IMAGE_INFO=""'):prep.index('ENV_TMP=')]
            script = 'set -euo pipefail\n. "$1"\nsay() { :; }\nok() { :; }\n' + block
            script += '\nprintf "%s\\n" "$SOURCE_IMAGE_ID" "$BASE_TAG" "$BASE_REF"\n'
            env = {"PATH": str(root) + os.pathsep + os.environ["PATH"], "LOG_DIR": str(root),
                   "E2E_IMAGE": reference, "REGISTRY": "registry.test", "REGISTRY_NS": "test", "ZOT_PORT": "5000",
                   "OWNED_ZOT": "1", "KUASAR_ARTIFACT_E2E": "1", **changes}
            result = subprocess.run(["bash", "-c", script, "_", str(Path(__file__).with_name("demo_common.sh"))],
                                    env=env, text=True, capture_output=True, timeout=5)
            calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
            return result, calls

    def test_names_tags_ids_and_digests_resolve_once_without_source_pull(self):
        identity = "sha256:" + "a" * 64
        for reference in ("python", "python:3.12-slim", identity, "a" * 12, "python@sha256:" + "c" * 64):
            with self.subTest(reference=reference):
                result, calls = self.prepare(reference)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), [identity, "sha-" + "a" * 64,
                                 "registry.test/test/base@sha256:" + "b" * 64])
                self.assertEqual([c for c in calls if c[0] == "pull"], [])
                self.assertEqual([c for c in calls if c[:2] == ["image", "inspect"]],
                                 [["image", "inspect", "--format", "{{.Id}} {{.Os}}/{{.Architecture}}", reference]])
                self.assertEqual([c[1] for c in calls if c[0] == "tag"], [identity])
                self.assertEqual(len([c for c in calls if c[0] == "push"]), 1)

    def test_missing_prepared_image_and_wrong_platform_fail_without_pull_or_publish(self):
        for change in ({"MISSING": "1"}, {"PLATFORM": "linux/arm64"}, {"PLATFORM": "windows/amd64"}):
            with self.subTest(change=change):
                result, calls = self.prepare("python:local", **change)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c[0] in ("pull", "tag", "push") for c in calls))

    def test_standalone_preparation_can_pull_a_missing_reference(self):
        result, calls = self.prepare("python:local", MISSING="1", KUASAR_ARTIFACT_E2E="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c for c in calls if c[0] == "pull"], [["pull", "--platform", "linux/amd64", "python:local"]])

    def test_destination_content_mismatch_is_never_overwritten(self):
        for owned in ("0", "1"):
            with self.subTest(owned=owned):
                result, calls = self.prepare("python:local", EXISTS="1", DEST_ID="sha256:" + "d" * 64, OWNED_ZOT=owned)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("refusing to overwrite", result.stderr)
                self.assertFalse(any(c[0] in ("tag", "push") for c in calls))

    def test_new_destination_content_is_read_back_after_publish(self):
        result, calls = self.prepare("python:local", DEST_ID="sha256:" + "d" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to overwrite", result.stderr)
        self.assertEqual(len([c for c in calls if c[0] == "push"]), 1)


if __name__ == "__main__":
    unittest.main()
