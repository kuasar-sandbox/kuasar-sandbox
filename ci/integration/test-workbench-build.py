#!/usr/bin/env python3
"""Check exact source handoff, native helpers and reproducible Go helper builds."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import struct
import tempfile
import unittest
from unittest.mock import patch

import artifacts
from test_fixtures import restore_using, helper_contract
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "release"))
from test_tag_sources import fixture

ROOT = Path(__file__).resolve().parents[2]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("workbench_delta", "ci/integration/build-artifacts.py")
release_helpers = load("workbench_release_helpers", "release/build-e2e-helpers.py")


def declare(root, owner):
    recipe = 'release/helper.sh' if owner == 'orchestrator' else None
    declaration = {'schema': 1, 'recipe': recipe, 'dependencies': [], 'helpers':
                   [{'name': 'custom-proxy', 'env': 'CUSTOM_PROXY_BIN', 'cases': ['orchestrator.*.sh']}] if recipe else []}
    (root / 'release/helper-inputs.json').write_text(json.dumps(declaration))
    if recipe: (root / recipe).write_text('go build -o "$2/custom-proxy" ./examples/custom-proxy\n')


def platform_fixture(root):
    fixture(root, 'connector', 'v0.0.1')
    git = release_helpers.tag_sources.git
    git(root, 'remote', 'set-url', 'origin', 'https://github.com/kuasar-sandbox/kuasar-sandbox')
    declare(root, 'platform')
    git(root, 'add', '.'); git(root, 'commit', '-qm', 'platform helper declaration')



class WorkbenchBuild(unittest.TestCase):
    def test_release_helper_private_copy_paths_do_not_change_go_binary_bytes(self):
        selected = {unit: ('runtime-v1.2.3' if unit == 'runtime' else 'vmlinux-v1.2.3' if unit == 'vmlinux' else 'v1.2.3')
                    for unit in release_helpers.selection.UNITS}
        arch = os.uname().machine
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = root / "sources"
            for unit, tag in selected.items():
                module = release_helpers.tag_sources.source_path(sources, unit, owner_layout=True)
                fixture(module, unit, tag)
                (module / "go.mod").write_text("module example.test/" + unit + "\ngo 1.22\n")
            proxy = sources / "orchestrator/examples/custom-proxy"
            proxy.mkdir(parents=True)
            (proxy / "main.go").write_text('package main\nimport ("fmt"; "runtime")\n'
                                         'func main() { _, file, _, _ := runtime.Caller(0); fmt.Println(file) }\n')
            for unit, tag in selected.items():
                module = release_helpers.tag_sources.source_path(sources, unit, owner_layout=True)
                declare(module, unit)
                release_helpers.tag_sources.git(module, 'add', '.')
                release_helpers.tag_sources.git(module, 'commit', '-qm', 'helper input')
                release_helpers.tag_sources.git(module, 'tag', '-f', tag)
            platform_fixture(root / "platform")
            copied = []
            def helpers(source, target, destination, selected, environment, contract):
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
                    release_helpers.build(sources, selected, root / phase, root / (phase + "-wheels"), root / "platform", arch)
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
        contract = helper_contract(sha='a' * 40, framework='b' * 40)
        contract['owners']['orchestrator']['source']['sha'] = 'c' * 40
        return {"mode": "source", "helper_contract": contract, "sources": revisions, "test_revisions": tests,
                "test_overlays": list(artifacts.OWNERS),
                "product_sources": {"node-ctl": {"kuasar-sandbox/orchestrator": "a" * 40}},
                "lanes": {"x86_64": {"products": [], "selection": {"cases": []}}}}

    def test_retired_mkfs_build_fails_before_sources_or_compilers_run(self):
        plan = self.plan()
        plan["framework_sha"] = "a" * 40
        plan["lanes"]["x86_64"]["products"] = ["mkfs.erofs"]
        with tempfile.TemporaryDirectory() as temporary, patch.object(builder.artifacts, "check_plan"), \
             patch.object(builder.platform, "machine", return_value="x86_64"), \
             patch.object(builder.subprocess, "check_output", return_value="a" * 40 + "\n"), \
             patch.object(builder, "materialize") as materialize, \
             patch.object(builder, "run") as run, \
             patch.dict(os.environ, {name: "" for name in ("GH_TOKEN", "GITHUB_TOKEN", "CALLER_TOKEN", "KUASAR_CI_APP_PRIVATE_KEY")}):
            with self.assertRaisesRegex(ValueError, "native mkfs builds are retired"):
                builder.build(plan, "x86_64", Path("assets"),
                              Path("sources"), Path(temporary) / "output")
            materialize.assert_not_called()
            run.assert_not_called()

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
                 patch.object(builder.source_inputs, "copy_run_inputs", side_effect=restore_using(checkout)), patch.object(builder, "run") as run:
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

    def test_release_helper_job_builds_only_native_helpers_from_selected_tags(self):
        from test_tag_sources import fixture
        selected = {unit: ('runtime-' if unit == 'runtime' else 'vmlinux-' if unit == 'vmlinux' else '') + 'v1.2.3'
                    for unit in release_helpers.selection.UNITS}
        framework = subprocess.check_output(['git', '-C', ROOT, 'rev-parse', 'HEAD'], text=True).strip()
        for arch in artifacts.ARCHES:
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                sources = work / "sources"
                for unit, tag in selected.items():
                    source = release_helpers.tag_sources.source_path(sources, unit, owner_layout=True)
                    fixture(source, unit, tag)
                    declare(source, unit)
                    git = release_helpers.tag_sources.git
                    git(source, 'add', '.'); git(source, 'commit', '-qm', 'declared helpers'); git(source, 'tag', '-f', tag)
                platform_fixture(work / 'platform')
                facts = release_helpers.tag_sources.inspect_sources(sources, selected, owner_layout=True)
                pins = release_helpers.tag_sources.owner_revisions(facts)
                calls = []
                def helpers(source, target, destination, chosen, environment, contract):
                    calls.append(target)
                    self.assertEqual(set(p.name for p in source.iterdir()), set(pins) | {'platform'})
                    for owner, unit in release_helpers.tag_sources.OWNER_UNITS.items():
                        expected = release_helpers.tag_sources.source_path(sources, unit, owner_layout=True)
                        self.assertEqual((source / owner / 'README.md').read_bytes(), (expected / 'README.md').read_bytes())
                    destination.mkdir(parents=True)
                    for name in chosen:
                        data = bytearray(64); data[:7] = b'\x7fELF\x02\x01\x01'
                        struct.pack_into('<H', data, 18, 62 if arch == 'x86_64' else 183)
                        (destination / name).write_bytes(data); (destination / name).chmod(0o755)
                real_run = subprocess.run
                def run(command, **kwargs):
                    if command[:3] == ['go', 'work', 'init']:
                        return subprocess.CompletedProcess(command, 0)
                    return real_run(command, **kwargs)
                with patch.object(release_helpers.platform, "machine", return_value=arch), \
                     patch.object(release_helpers.subprocess, "run", side_effect=run), \
                     patch.object(release_helpers.build_demo_wheels, "build") as wheels, \
                     patch.object(release_helpers.build_helpers, "build", side_effect=helpers):
                    release_helpers.build(sources, selected, work / "helpers", work / "wheels", work / "platform", arch)
                self.assertEqual(calls, [arch])
                self.assertEqual(wheels.call_args.args[1], arch)
                self.assertEqual({path.name for path in (work / "helpers").iterdir()}, {arch, "contract.json"})
                record = json.loads((work / "helpers" / arch / "helpers.json").read_text())
                self.assertEqual(record["test_revisions"], pins)
                self.assertEqual(record["source_records"], facts)
                self.assertEqual(record["framework_sha"], framework)
                self.assertEqual(record["helpers"]["custom-proxy"]["source_sha"], facts['orchestrator']['sha'])


if __name__ == "__main__":
    unittest.main()
