#!/usr/bin/env python3
"""Credential-free contracts for the hosted formal release entry."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "release"))
import formal_coordinator as formal


class FormalReleaseTest(unittest.TestCase):
    def test_separate_source_checkout_is_selected_not_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "release").mkdir()
            (source / "release/formal_coordinator.py").write_text("raise RuntimeError('untrusted source controller')")
            environment = dict(os.environ, PLATFORM_ROOT=str(source), PYTHONDONTWRITEBYTECODE="1")
            program = (
                "import sys; from pathlib import Path; "
                "import formal_coordinator as f; "
                "assert f.ROOT == Path(sys.argv[1]).resolve(); "
                "assert f.coordinator.PLATFORM_ROOT == f.ROOT; "
                "assert Path(f.coordinator.__file__).resolve().parent == Path(sys.argv[2]).resolve()"
            )
            subprocess.run([sys.executable, "-c", program, str(source), str(ROOT / "release")],
                           cwd=ROOT / "release", env=environment, check=True, timeout=10)

    def test_selected_manifest_and_checkout_use_the_source_root(self):
        sha = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "releases").mkdir()
            (source / "releases/release.yaml").write_text("selected-manifest")
            with (mock.patch.object(formal, "ROOT", source),
                  mock.patch.dict(os.environ, RELEASE_VERSION="release-v1.2.3",
                                  PLATFORM_REF="main", PLATFORM_SHA=sha),
                  mock.patch.object(formal.coordinator, "run", return_value=mock.Mock(stdout=sha)) as run,
                  mock.patch.object(formal.coordinator, "branch_sha", return_value=sha),
                  mock.patch.object(formal.coordinator.selection, "validate_current_manifests") as validate,
                  mock.patch.object(formal.coordinator.selection, "parse_manifest",
                                    return_value=("release-v1.2.3", None, {})) as parse,
                  mock.patch.object(formal.coordinator, "platform_release",
                                    return_value=mock.Mock(complete=True)),
                  mock.patch.object(formal.coordinator, "UNITS", ()),
                  mock.patch.object(formal, "wait_for_convergence") as converge):
                formal.main()
            self.assertEqual(run.call_args.kwargs["cwd"], source)
            validate.assert_called_once_with(source)
            parse.assert_called_once_with("selected-manifest", str(source / "releases/release.yaml"), False)
            converge.assert_called_once_with({}, "release-v1.2.3", sha)

    def test_request_guard_rejects_untrusted_ref_preview_and_injection(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/formal-release.yml").read_text())
        jobs = workflow["jobs"]
        job = jobs["converge"]
        guard = job["steps"][0]["run"]
        base = dict(os.environ, GITHUB_REF="refs/heads/main", RELEASE_VERSION="release-v1.2.3",
                    PLATFORM_REF="main", PLATFORM_SHA="a" * 40)
        cases = [
            ({}, True),
            ({"PLATFORM_REF": "release/v1.2.x"}, True),
            ({"GITHUB_REF": "refs/heads/unreviewed"}, False),
            ({"RELEASE_VERSION": "release-v1.2.3-preview.20261010"}, False),
            ({"RELEASE_VERSION": "release-v01.2.3"}, False),
            ({"PLATFORM_REF": "feature/unreviewed"}, False),
            ({"PLATFORM_REF": "main; false"}, False),
            ({"PLATFORM_SHA": "a" * 39}, False),
        ]
        for overrides, valid in cases:
            with self.subTest(overrides=overrides):
                result = subprocess.run(["bash", "-c", guard], env={**base, **overrides},
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode == 0, valid)
        self.assertEqual(workflow.get("on", workflow.get(True)).keys(), {"workflow_dispatch"})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(workflow["concurrency"], {
            "group": "preview-manifest-selection-and-gc", "cancel-in-progress": False})
        tokens = [step for step in job["steps"] if "create-github-app-token@" in step.get("uses", "")]
        self.assertEqual(len(tokens), 1)
        permissions = {key: value for key, value in tokens[0]["with"].items() if key.startswith("permission-")}
        self.assertEqual(permissions, {"permission-actions": "write", "permission-contents": "read"})
        for step in job["steps"]:
            self.assertNotIn("continue-on-error", step)
            self.assertNotIn("ssh ", step.get("run", ""))
            if "actions/checkout@" in step.get("uses", ""):
                self.assertFalse(step["with"]["persist-credentials"])
        runner = job["steps"][-1]
        self.assertEqual(runner["run"], "python3 control/release/formal_coordinator.py")
        self.assertEqual(runner["env"]["PLATFORM_ROOT"], "${{ github.workspace }}/platform")
        wait = int(runner["env"]["RELEASE_WAIT_SECONDS"])
        self.assertLess(wait, job["timeout-minutes"] * 60)
        self.assertLessEqual(job["timeout-minutes"], 55)


if __name__ == "__main__":
    unittest.main()
