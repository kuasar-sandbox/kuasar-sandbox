#!/usr/bin/env python3
"""Check exact source handoff, native helpers and reproducible Go helper builds."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import artifacts

ROOT = Path(__file__).resolve().parents[2]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("workbench_delta", "ci/integration/build-artifacts.py")
release_helpers = load("workbench_release_helpers", "release/build-e2e-helpers.py")


class WorkbenchBuild(unittest.TestCase):
    def test_release_helper_private_copy_paths_do_not_change_go_binary_bytes(self):
        pins = {owner: "c" * 40 for owner in artifacts.OWNERS if owner != "platform"}
        arch = os.uname().machine
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = root / "sources"
            for owner in pins:
                module = sources / owner
                module.mkdir(parents=True)
                (module / "go.mod").write_text("module example.test/" + owner + "\ngo 1.22\n")
            proxy = sources / "orchestrator/examples/custom-proxy"
            proxy.mkdir(parents=True)
            (proxy / "main.go").write_text('package main\nimport ("fmt"; "runtime")\n'
                                         'func main() { _, file, _, _ := runtime.Caller(0); fmt.Println(file) }\n')
            copied = []
            def helpers(source, target, destination, selected, environment):
                copied.append(source)
                self.assertIn("-p=2", environment["GOFLAGS"].split())
                destination.mkdir(parents=True)
                # Same owner command shape which omitted -trimpath in the
                # pinned custom-proxy recipe; the producer supplies the flag.
                subprocess.run(["go", "-C", str(source / "orchestrator"), "build",
                                "-o", str(destination / "custom-proxy"), "./examples/custom-proxy"],
                               env={**environment, "GOWORK": "off", "CGO_ENABLED": "0"}, check=True)
                for name in set(selected) - {"custom-proxy"}:
                    (destination / name).write_bytes(b"unrelated helper fixture")
            with patch.dict(os.environ, {"GOFLAGS": "-p=2", "GOPROXY": "off", "GOSUMDB": "off"}), \
                    patch.object(release_helpers.build_demo_wheels, "build"), \
                    patch.object(release_helpers.build_helpers, "build", side_effect=helpers):
                for phase in ("cold", "warm"):
                    release_helpers.build(sources, pins, root / phase, root / (phase + "-wheels"), root, arch)
            self.assertNotEqual(copied[0], copied[1])
            self.assertTrue(all(not source.exists() for source in copied))
            self.assertEqual(artifacts.digest(root / "cold" / arch / "custom-proxy"),
                             artifacts.digest(root / "warm" / arch / "custom-proxy"))
            self.assertEqual((root / "cold" / arch / "helpers.json").read_bytes(),
                             (root / "warm" / arch / "helpers.json").read_bytes())

    def plan(self):
        revisions = artifacts.release_test_revisions(
            {owner: "a" * 40 for owner in artifacts.OWNERS if owner != "platform"}, "b" * 40)
        tests = copy.deepcopy(revisions)
        tests["orchestrator"]["sha"] = "c" * 40
        return {"mode": "source", "sources": revisions, "test_revisions": tests,
                "test_overlays": list(artifacts.OWNERS),
                "product_sources": {"node-ctl": {"kuasar-sandbox/orchestrator": "a" * 40}},
                "lanes": {"x86_64": {"products": [], "selection": {"cases": []}}}}

    def test_host_fetches_exact_product_test_and_helper_sources_without_go(self):
        plan = self.plan()
        with tempfile.TemporaryDirectory() as temporary:
            sources = Path(temporary) / "sources"
            fetched = {}
            def checkout(repository, sha, destination):
                destination.mkdir()
                (destination / "go.mod").write_text("module fixture\n\ngo 1.26.1\n")
                fetched[str(destination.relative_to(sources))] = sha
            with patch.object(builder.artifacts, "planned_helpers", return_value={"custom-proxy": "orchestrator"}), \
                 patch.object(builder, "checkout", side_effect=checkout), patch.object(builder, "run") as run:
                builder.materialize(plan, "x86_64", sources)
                run.assert_not_called()
                self.assertEqual(fetched["orchestrator"], "a" * 40)
                self.assertEqual(fetched["test-overlays/orchestrator"], "c" * 40)
                self.assertEqual(fetched["test-helpers/orchestrator"], "c" * 40)
                self.assertEqual(builder.helper_sources(plan, "x86_64", sources), sources / "test-helpers")
                self.assertEqual(builder.test_source(plan, "x86_64", sources, "orchestrator"), sources / "test-overlays/orchestrator")
                builder.configure_workspace(sources)
                self.assertEqual(run.call_args.args[0][:3], ["go", "work", "init"])
                self.assertEqual(run.call_args.kwargs["environment"]["GOWORK"], "off")

    def test_changed_materialized_revision_is_rejected_before_compilation(self):
        plan = self.plan()
        with patch.object(builder.artifacts, "planned_helpers", return_value={}), \
             patch.object(builder.subprocess, "check_output", return_value="f" * 40 + "\n"), \
             patch.object(builder, "run") as run:
            with self.assertRaisesRegex(ValueError, "materialized source identity changed"):
                builder.verify_materialized(plan, "x86_64", Path("/private/sources"))
            run.assert_not_called()

    def test_materialized_checkout_rejects_dirty_and_untracked_compiler_inputs(self):
        for mutation in ("clean", "modified", "staged", "untracked"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "connector"
                subprocess.run(["git", "init", "--quiet", "--template=", str(source)], check=True)
                for key, value in (("user.name", "Chen Xiaohui"), ("user.email", "graych@gmail.com")):
                    subprocess.run(["git", "-C", source, "config", "--local", key, value], check=True)
                (source / "input.go").write_text("package fixture\n")
                subprocess.run(["git", "-C", source, "add", "input.go"], check=True)
                subprocess.run(["git", "-C", source, "commit", "--quiet", "-m", "fixture"], check=True)
                revision = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True).strip()
                if mutation in ("modified", "staged"):
                    (source / "input.go").write_text("package changed\n")
                    if mutation == "staged":
                        subprocess.run(["git", "-C", source, "add", "input.go"], check=True)
                elif mutation == "untracked":
                    (source / "extra.go").write_text("package fixture\n")
                with patch.object(builder, "source_layout", return_value={"connector": {"sha": revision}}):
                    if mutation == "clean":
                        builder.verify_materialized({}, "x86_64", root)
                    else:
                        with self.assertRaisesRegex(ValueError, "materialized source has"):
                            builder.verify_materialized({}, "x86_64", root)

    def test_cross_architecture_is_rejected_before_checkout_or_outputs(self):
        with patch.object(builder.artifacts, "check_plan"), patch.object(builder.platform, "machine", return_value="x86_64"), \
             patch.object(builder, "materialize") as materialize:
            with self.assertRaisesRegex(ValueError, "selected native architecture"):
                builder.build({}, "aarch64", Path("assets"), Path("sources"), Path("output"))
            materialize.assert_not_called()

    def test_framework_mismatch_is_rejected_before_checkout_or_toolchain_selection(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(builder.artifacts, "check_plan"), \
             patch.object(builder.platform, "machine", return_value="x86_64"), \
             patch.object(builder.subprocess, "check_output", return_value="f" * 40 + "\n"), \
             patch.object(builder, "materialize") as materialize, patch.object(builder, "configure_workspace") as configure, \
             patch.dict(os.environ, {name: "" for name in ("GH_TOKEN", "GITHUB_TOKEN", "CALLER_TOKEN", "KUASAR_CI_APP_PRIVATE_KEY")}):
            with self.assertRaisesRegex(ValueError, "framework checkout differs"):
                builder.build({"framework_sha": "a" * 40}, "x86_64", Path("assets"), Path("sources"), Path(temporary) / "output")
            materialize.assert_not_called()
            configure.assert_not_called()

    def test_release_helper_job_builds_only_its_native_architecture_and_exact_pins(self):
        pins = {owner: "c" * 40 for owner in artifacts.OWNERS if owner != "platform"}
        for arch in artifacts.ARCHES:
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                sources = work / "sources"
                for owner in pins:
                    path = sources / owner
                    path.mkdir(parents=True)
                    (path / "go.mod").write_text("module fixture\n")
                calls = []
                def helpers(source, target, destination, selected, environment):
                    calls.append(target)
                    destination.mkdir(parents=True)
                    for name in selected:
                        (destination / name).write_bytes(b"fixture")
                with patch.object(release_helpers.platform, "machine", return_value=arch), \
                     patch.object(release_helpers.subprocess, "check_output", return_value="b" * 40 + "\n"), \
                     patch.object(release_helpers.subprocess, "run"), \
                     patch.object(release_helpers.build_demo_wheels, "build") as wheels, \
                     patch.object(release_helpers.build_helpers, "build", side_effect=helpers):
                    release_helpers.build(sources, pins, work / "helpers", work / "wheels", work / "platform", arch)
                self.assertEqual(calls, [arch])
                self.assertEqual(wheels.call_args.args[1], arch)
                self.assertEqual([path.name for path in (work / "helpers").iterdir()], [arch])
                record = json.loads((work / "helpers" / arch / "helpers.json").read_text())
                self.assertEqual(record["test_revisions"], pins)
                self.assertEqual(record["framework_sha"], "b" * 40)
                self.assertEqual(record["helpers"]["custom-proxy"]["source_sha"], "c" * 40)


if __name__ == "__main__":
    unittest.main()
