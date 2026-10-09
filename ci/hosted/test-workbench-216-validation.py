#!/usr/bin/env python3
"""Offline fixtures for the temporary #216 full-validation entry."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("task216", ROOT / "ci/hosted/verify-workbench-216.py")
task = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(task)


class VerificationContracts(unittest.TestCase):
    def test_cold_and_warm_scope_are_frozen_before_task_metadata_or_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan"
            plan.mkdir()
            for name in ("frozen.json", "workbench.json", "integration-plan.json"):
                (plan / name).write_text("frozen task fixture")
            record = {"sources": {owner: {"repository": repository, "sha": "a" * 40}
                                   for owner, repository in task.REPOSITORIES.items()}}
            snapshots, receipts = [], []

            def checkout(repository, revision, destination):
                (destination / ".git").mkdir(parents=True)
                (destination / ".git/fixture").write_text(repository + "@" + revision)

            def cache_scope(command, *, check):
                self.assertTrue(check)
                self.assertEqual(command[2:4], [task.ROOT / "ci/hosted/workbench.py", "cache-scope"])
                sources, receipt = command[5], command[7]
                self.assertEqual(set(path.name for path in sources.iterdir()), set(task.REPOSITORIES))
                snapshots.append({path.name: (path / ".git/fixture").read_text() for path in sources.iterdir()})
                expected_hash = hashlib.sha256(str(sources.resolve()).encode()).hexdigest()
                self.assertEqual(receipt, root / ("workbench-cache-scope-123-" + expected_hash + ".json"))
                receipts.append(receipt)

            builder = SimpleNamespace(checkout=checkout)
            spec = SimpleNamespace(loader=SimpleNamespace(exec_module=Mock()))
            with patch.object(task, "frozen", return_value=record), \
                    patch.object(task.importlib.util, "spec_from_file_location", return_value=spec), \
                    patch.object(task.importlib.util, "module_from_spec", return_value=builder), \
                    patch.object(task.subprocess, "run", side_effect=cache_scope), \
                    patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "123", "RUNNER_TEMP": str(root)}):
                for phase in ("cold", "warm"):
                    sources = root / phase
                    task.fetch(SimpleNamespace(frozen=plan / "frozen.json", sources=sources))
                    self.assertEqual((sources / "frozen.json").read_bytes(), (plan / "frozen.json").read_bytes())
                    self.assertEqual((sources / "workbench.json").read_bytes(), (plan / "workbench.json").read_bytes())
                    if phase == "warm":
                        (sources / "carried-products.tar").write_text("added only after fresh source admission")
            self.assertEqual(snapshots[0], snapshots[1])
            self.assertNotEqual(receipts[0], receipts[1])

    def test_failure_and_signal_keep_diagnostics_out_of_candidate_upload_paths(self):
        for exit_code in (17, -15):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = task.Evidence(root / "sources/verification", {}, root / "output/verification")
                secret = root / "host-file"
                secret.write_text("must never be dereferenced by an uploader")
                (evidence.directory / "result.json").symlink_to(secret)
                os.mkfifo(evidence.directory / "partial-package")
                command = [sys.executable, "-c", "import os, signal, sys; print('original failure', flush=True); "
                           + ("sys.exit(17)" if exit_code == 17 else "os.kill(os.getpid(), signal.SIGTERM)")]
                with self.assertRaises(ValueError):
                    evidence.run("failed-stage", command)
                evidence.record.update(conclusion="failure")
                evidence.save()
                with self.assertRaisesRegex(ValueError, "failed task outputs"):
                    evidence.export_success()
                result = json.loads((evidence.diagnostics / "result.json").read_text())
                self.assertEqual(result["stages"][0]["exit_code"], exit_code)
                self.assertEqual((evidence.diagnostics / "logs/failed-stage.log").read_text(), "original failure\n")
                self.assertFalse((evidence.directory / "logs").exists())
                self.assertEqual(secret.read_text(), "must never be dereferenced by an uploader")

    def test_successful_diagnostics_are_still_inside_the_declared_checked_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = task.Evidence(root / "sources/verification", {}, root / "output/verification")
            evidence.run("package", [sys.executable, "-c", "print('validated')"])
            evidence.record.update(conclusion="success")
            evidence.save()
            evidence.export_success()
            self.assertEqual((evidence.directory / "result.json").read_bytes(),
                             (evidence.diagnostics / "result.json").read_bytes())
            self.assertEqual((evidence.directory / "logs/package.log").read_text(), "validated\n")

    def test_in_container_copy_does_not_dereference_links_or_read_special_files(self):
        for kind in ("link", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = task.Evidence(root / "sources/verification", {}, root / "output/verification")
                path = evidence.diagnostics / "unsafe"
                if kind == "link":
                    path.symlink_to(root / "unavailable-host-file")
                else:
                    os.mkfifo(path)
                evidence.record.update(conclusion="success")
                evidence.save()
                if kind == "fifo":
                    with self.assertRaises(OSError):
                        evidence.export_success()
                    self.assertEqual(evidence.record["conclusion"], "failure")
                else:
                    evidence.export_success()
                    # The shared action rejects this escaping link after stopping
                    # the instance. Copying diagnostics itself never follows it.
                    self.assertTrue((evidence.directory / "unsafe").is_symlink())
                    self.assertFalse((root / "unavailable-host-file").exists())

    def test_all_six_units_use_the_existing_package_and_validate_interfaces(self):
        version = "v0.0.0-preview.20261009.123"
        expected = {
            "accelerator": ("accelerator", version), "connector": ("connector", version),
            "sandboxer": ("sandboxer", version), "orchestrator": ("orchestrator", version),
            "runtime": ("guest-runtime", "runtime-" + version),
            "vmlinux": ("guest-runtime", "vmlinux-" + version),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = root / "sources"
            evidence = task.Evidence(sources / "verification", {}, root / "output/verification")
            evidence.record["products"] = {"exact-product": "frozen-hash"}
            record = {"version": version, "sources": {name: {"sha": "a" * 40} for name in task.REPOSITORIES}}
            commands = []

            def run(label, command, *, cwd, env):
                argv = list(map(str, command))
                commands.append((label, argv[argv.index("bash"):], cwd, env))
                trace = Path(argv[argv.index("-o") + 1])
                trace.parent.mkdir(parents=True, exist_ok=True)
                trace.write_text('1 execve("/usr/bin/bash", ["bash", "release.sh"], 0x123) = 0\n')
                assets = Path(argv[-1]) / "assets"
                assets.mkdir(parents=True, exist_ok=True)
                (assets / "validated.tar.gz").write_bytes(b"fixture archive")

            with patch.object(task, "output", return_value="1700000000"), \
                    patch.object(task, "manifest", return_value=[]), \
                    patch.object(task, "products", return_value=evidence.record["products"]) as products, \
                    patch.object(evidence, "run", side_effect=run):
                task.package_all(sources, "aarch64", record, evidence, {"KUASAR_BUILD_JOBS": "2"})
            self.assertEqual(len(commands), 12)
            self.assertEqual(products.call_count, 12)
            for label, argv, cwd, env in commands:
                operation, unit = label.split("/")
                owner, selected_version = expected[unit]
                wanted = ["bash", str(sources / owner / "scripts/release.sh"), operation]
                if unit in ("runtime", "vmlinux"):
                    wanted.append(unit)
                wanted += [selected_version, "aarch64", str(evidence.directory / "packages" / unit)]
                self.assertEqual(argv, wanted)
                self.assertEqual(cwd, sources / owner)
                self.assertEqual(env["SOURCE_SHA"], "a" * 40)
                self.assertEqual(env["SOURCE_DATE_EPOCH"], "1700000000")
                for dependency in ("ACCELERATOR", "CONNECTOR", "SANDBOXER"):
                    self.assertEqual(env["RELEASE_" + dependency + "_VERSION"], version)
                    self.assertEqual(env["RELEASE_" + dependency + "_SOURCE_SHA"], "a" * 40)
                if owner in task.VALIDATORS:
                    self.assertEqual(env["RELEASE_ARCHIVE_VALIDATOR"],
                                     str(sources / "task-tools" / owner / "release-archive-validator"))
            self.assertEqual(set(evidence.record["packages"]), set(expected))

    def test_packaging_trace_allows_inspection_but_rejects_compilation_or_empty_trace(self):
        accepted = [["go", "-C", "/src/sandboxer", "env", "GOROOT"],
                    ["go", "mod", "download", "-json", "example@v1"], ["go", "version", "-m", "bin"],
                    ["cargo", "metadata", "--locked"], ["rustc", "-vV"], ["rustc", "--print", "sysroot"],
                    ["gcc", "-print-file-name=libgcc.a"]]
        rejected = [["go", "run", "validator.go"], ["go", "build", "./..."], ["cargo", "build", "--release"],
                    ["make", "build"], ["gcc", "-c", "input.c"], ["rustc", "input.rs"],
                    ["compile", "-o", "obj", "file.go"]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace"
            for argv in accepted + rejected:
                path.write_text('123 execve(' + json.dumps('/usr/bin/' + argv[0]) + ', ' + json.dumps(argv)
                                + ', 0x123 /* 3 vars */) = 0\n')
                if argv in accepted:
                    task.audit_packaging(path)
                else:
                    with self.assertRaises(ValueError, msg=repr(argv)):
                        task.audit_packaging(path)
            for text in ("", "not an execution trace\n"):
                path.write_text(text)
                with self.assertRaisesRegex(ValueError, "no readable exec"):
                    task.audit_packaging(path)

    def test_carried_products_exclude_all_native_materials(self):
        paths = task.carry_paths(Path("/src"), "aarch64", [
            ("sandboxer", "cloud-hypervisor"), ("guest-runtime/native-deps", "vmlinux"),
            ("guest-runtime/native-deps", "mkfs.erofs"), ("sandboxer", "sandbox-ctl"),
            ("guest-runtime", "sandbox-runtime.bundle")])
        self.assertIn("sandboxer/bin/aarch64/sandbox-ctl", paths)
        self.assertIn("accelerator/build/aarch64/cache-ctl.map", paths)
        self.assertFalse(any("/native-deps/" in path or path.endswith("/cloud-hypervisor") for path in paths))

    def test_warm_miss_never_calls_native_build(self):
        for miss in ("0", "1"):
            with self.subTest(miss=miss), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                script = root / "ci/native-cache/native-cache.sh"
                script.parent.mkdir(parents=True)
                marker = root / "unexpected-build"
                key = "a" * 64
                script.write_text('''CACHE_ROOT="$FIXTURE_CACHE_ROOT"
CACHE_SCHEMA=v2
TARGET_ARCH=x86_64
compute_key() { printf %s KEY; }
printf '%s\\t%s\\n' "$2" KEY
die() { echo "$*" >&2; exit 1; }
build_component() { touch "$BUILD_MARKER"; }
restore_or_build() {
  if [ "$WARM_MISS" = 1 ]; then build_component "$1"; else
    [ -f "$KUASAR_NATIVE_CACHE_METRICS" ] || printf 'component\\tstatus\\tinput_hash\\telapsed_seconds\\n' > "$KUASAR_NATIVE_CACHE_METRICS"
    printf '%s\\thit\\tKEY\\t0\\n' "$1" >> "$KUASAR_NATIVE_CACHE_METRICS"
  fi
}
'''.replace("KEY", key))
                for component in task.NATIVE:
                    entry = root / "cache/v2/x86_64" / component / key
                    entry.mkdir(parents=True)
                    for name in ("inputs.tsv", "provenance.txt", "SHA256SUMS", "payload.tar"):
                        (entry / name).write_text("fixture")
                evidence = task.Evidence(root / "sources/verification", {}, root / "output/verification")
                environment = {**os.environ, "WARM_MISS": miss, "BUILD_MARKER": str(marker),
                               "KUASAR_NATIVE_CACHE_METRICS": str(root / "native.tsv"),
                               "FIXTURE_CACHE_ROOT": str(root / "cache")}
                with patch.object(task, "ROOT", root):
                    if miss == "1":
                        with self.assertRaises(ValueError):
                            task.native_cache(evidence, "restore", environment)
                    else:
                        task.native_cache(evidence, "restore", environment)
                        self.assertEqual(len(evidence.record["native_cache_hits"]), 5)
                self.assertFalse(marker.exists())
                self.assertEqual(evidence.record["stages"][-1]["exit_code"], int(miss))

    def test_no_failed_or_cancelled_candidate_path_is_uploaded_by_the_task(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/workbench-216-validation.yml").read_text())
        self.assertEqual(workflow["permissions"], {"contents": "read", "pull-requests": "read"})
        for lane, arch, runner in (("x86", "x86_64", "ubuntu-24.04"), ("arm", "aarch64", "ubuntu-24.04-arm")):
            for phase in ("cold", "warm"):
                job = workflow["jobs"][phase + "-" + lane]
                self.assertEqual(job["runs-on"], runner)
                self.assertEqual(job["needs"], "prepare" if phase == "cold" else ["prepare", "cold-" + lane])
                self.assertIn("github.ref == 'refs/heads/main'", job["if"])
                seen_workbench = False
                for step in job["steps"]:
                    if "actions/checkout@" in step.get("uses", ""):
                        self.assertEqual(step["with"]["ref"], "${{ job.workflow_sha }}")
                    if "run" in step:
                        subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)
                    if step.get("uses", "").endswith("/.github/actions/workbench"):
                        self.assertEqual(step["id"], "verification")
                        self.assertEqual(step["with"]["arch"], arch)
                        self.assertEqual(step["with"]["outputs"], "verification")
                        self.assertEqual(step["with"]["cache-coverage"], "full-manifest")
                        self.assertEqual(step["with"]["cpus"], "2")
                        self.assertEqual(step["with"]["memory-gib"], "8")
                        self.assertNotIn("env", step)
                        seen_workbench = True
                    if "actions/upload-artifact@" in step.get("uses", ""):
                        self.assertTrue(seen_workbench)
                        self.assertEqual(step["if"], "success() && steps.verification.outcome == 'success'")
                        for path in step["with"]["path"].splitlines():
                            self.assertTrue(path.startswith("sources/verification"))
                self.assertTrue(seen_workbench)

    def test_packaged_integration_keeps_architectures_independent_and_existing_runners(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/workbench-216-validation.yml").read_text())
        jobs = workflow["jobs"]
        for lane, arch in (("x86", "x86_64"), ("arm", "aarch64")):
            self.assertEqual(jobs["helpers-" + lane]["needs"], "prepare")
            self.assertEqual(jobs["prepare-integration-" + lane]["needs"], ["prepare", "cold-" + lane, "helpers-" + lane])
            self.assertEqual(jobs["e2e-" + lane]["needs"], ["prepare", "prepare-integration-" + lane])
            self.assertEqual(jobs["e2e-" + lane]["strategy"]["matrix"]["shard"],
                             "${{ fromJSON(needs.prepare.outputs." + arch + "_shards) }}")
            self.assertEqual(jobs["e2e-" + lane]["env"]["TARGET_ARCH"], arch)
            self.assertNotIn("warm-" + lane, jobs["prepare-integration-" + lane]["needs"])
            for step in jobs["helpers-" + lane]["steps"]:
                if step.get("uses", "").endswith("/.github/actions/workbench"):
                    self.assertEqual(step["with"]["cache"], "false")
            prepare = json.dumps(jobs["prepare-integration-" + lane])
            self.assertIn("verify-workbench-216.py packaged-delta", prepare)
            self.assertIn("prepare-artifacts.py", prepare)
            self.assertIn("--clean-image", prepare)
            self.assertNotIn("build-artifacts.py", prepare)
            self.assertIn("run-artifact-tests.py", json.dumps(jobs["e2e-" + lane]))
        self.assertIn("run-artifact-performance.py", json.dumps(jobs["performance-x86"]))
        self.assertIn("artifacts.py shard-results", json.dumps(jobs["validation-results"]))
        for job in jobs.values():
            for step in job.get("steps", []):
                if "run" in step:
                    subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)
                if "actions/upload-artifact@" in step.get("uses", ""):
                    self.assertNotIn("always", step.get("if", "success()"))


class PackagedInputContracts(unittest.TestCase):
    """Real tar/ELF/EROFS composition fixtures; no product compilation or execution."""

    ARCH = "x86_64"

    @classmethod
    def setUpClass(cls):
        cls.fixtures = task.module("task216_existing_archive_fixtures", ROOT / "ci/integration/test-artifacts.py")
        cls.fixtures.ArtifactContracts.setUpClass()

    def setUp(self):
        base = self.fixtures.ArtifactContracts()
        base.setUp()
        self.addCleanup(base.doCleanups)
        base.workbench_stage()
        base.plan["baseline"].update(repository="kuasar-sandbox/kuasar-sandbox", staged=False)
        self.base, self.root = base, base.root
        self.arch, self.image = self.ARCH, "sha256:" + "1" * 64
        self.record = {"task": "kuasar-sandbox/kuasar-sandbox#216", "framework_sha": "a" * 40,
                       "run_id": "123", "version": "v0.0.0-preview.20261009.123", "admission": None,
                       "sources": {owner: {"repository": repository, "sha": "d" * 40}
                                   for owner, repository in task.REPOSITORIES.items()}}
        self.record["test_revisions"] = self.record["sources"]
        resolver = task.module("task216_fixture_resolver", ROOT / "ci/integration/resolve-artifacts.py")
        cases = self.fixtures.WORKBENCH_CASES | {"guest-runtime": ["image.flatten.sh"]}
        with patch.object(resolver, "baseline", return_value=base.plan["baseline"]) as baseline, \
                patch.object(resolver, "case_files", return_value=cases), \
                patch.object(task, "module", return_value=resolver):
            self.plan = task.integration_plan(self.record)
        baseline.assert_called_once_with("a" * 40, "main")
        self.plan_directory = self.root / "plan"
        self.plan_directory.mkdir()
        task.write(self.plan_directory / "integration-plan.json", self.plan)
        task.write(self.plan_directory / "workbench.json", {"architectures": {self.arch: {"image_id": self.image}}})
        self.record["workbench_sha256"] = task.artifacts.digest(self.plan_directory / "workbench.json")
        self.record["integration_plan_sha256"] = task.artifacts.digest(self.plan_directory / "integration-plan.json")
        task.write(self.plan_directory / "frozen.json", self.record)
        self.frozen_digest = task.artifacts.digest(self.plan_directory / "frozen.json")
        # The helper/test data uses the existing fixture's owned overlay layout.
        base.plan = self.plan
        helper = base.delta(self.arch)
        shutil.rmtree(helper / "bin")
        (helper / "outputs.json").unlink()
        self.helpers = self.root / "helpers"
        self.helpers.mkdir()
        helper.rename(self.helpers / "integration-helpers")
        helper = self.helpers / "integration-helpers"
        metadata = base.metadata
        for name, owner in task.artifacts.planned_helpers(self.plan["lanes"][self.arch]["selection"]).items():
            binary = helper / "helpers" / name
            binary.parent.mkdir(exist_ok=True)
            binary.write_bytes(self.fixtures.elf(self.arch, "pinned helper " + name))
            binary.chmod(0o755)
            metadata["helpers"][name] = {"sha256": task.artifacts.digest(binary),
                                         "source_sha": self.plan["framework_sha"] if owner == "framework"
                                         else self.plan["test_revisions"][owner]["sha"]}
        metadata.update(frozen_sha256=self.frozen_digest, image_id=self.image)
        task.write(helper / "helpers.json", metadata)
        self.packages = self.root / "cold"
        files = {name: self.fixtures.elf(self.arch, "task package " + name) for name in task.artifacts.PRODUCTS}
        files["vmlinux"] = self.fixtures.kernel(self.arch)
        runtime = self.root / "candidate-runtime"
        runtime.mkdir()
        files["sandbox-runtime.bundle"] = self.fixtures.runtime(runtime, self.arch, files)
        self.cold = {"conclusion": "success", "phase": "cold", "arch": self.arch, "image_id": self.image,
                     "frozen_sha256": self.frozen_digest,
                     "products": {name: {"sha256": hashlib.sha256(data).hexdigest()} for name, data in files.items()},
                     "packages": {}}
        self.cold["products"]["embedded/envd"] = {"sha256": hashlib.sha256(self.fixtures.elf(self.arch, "envd")).hexdigest()}
        for unit in task.UNITS:
            assets = self.packages / "packages" / unit / "assets"
            assets.mkdir(parents=True)
            version = (unit + "-" if unit in ("runtime", "vmlinux") else "") + self.record["version"]
            name = task.artifacts.archive_name(unit, version, self.arch)
            self.fixtures.archive(assets / name, {"bin/" + product: data for product, data in files.items()
                                                  if task.artifacts.PRODUCTS[product] == unit})
            self.cold["packages"][unit] = task.artifacts.tree_files(assets)
        task.write(self.packages / "result.json", self.cold)

    def delta(self, name="delta"):
        args = SimpleNamespace(frozen=self.plan_directory / "frozen.json", arch=self.arch,
                               packages=self.packages, helpers=self.helpers, output=self.root / name)
        real_run = subprocess.run

        def only_runtime_reader(command, **kwargs):
            self.assertIn(command[0], ("python3", sys.executable))
            self.assertEqual(command[1], os.environ["KUASAR_RUNTIME_READER"])
            return real_run(command, **kwargs)

        with patch.object(task, "check_framework"), patch.object(task.subprocess, "run", side_effect=only_runtime_reader):
            task.packaged_delta(args)
        return args.output

    def test_exact_six_packages_feed_existing_compose_without_product_builds(self):
        self.assertEqual(self.plan["baseline"]["delivery"], "workbench-v1")
        self.assertEqual(self.plan["baseline"]["version"], "release-v1.2.3")
        self.assertNotEqual(self.plan["baseline"]["version"], "release-" + self.record["version"])
        delta = self.delta()
        metadata = json.loads((delta / "outputs.json").read_text())
        self.assertEqual(set(metadata["build_context"]["task"]["package_origins"]), set(task.UNITS))
        provenance = task.artifacts.compose(self.plan, self.arch, self.base.assets, delta, self.root / "prepared" / self.arch)
        self.assertEqual(set(provenance["products"]), set(task.artifacts.PRODUCTS))
        for name, product in provenance["products"].items():
            self.assertEqual(product["origin"], "candidate")
            self.assertEqual(product["sha256"], self.cold["products"][name]["sha256"])
        self.assertEqual(provenance["embedded"]["envd"], self.cold["products"]["embedded/envd"]["sha256"])
        self.assertEqual(self.plan["lanes"]["x86_64"]["performance"], ["working-set-smoke"])
        self.assertEqual(self.plan["lanes"]["aarch64"]["performance"], [])

    def test_changed_package_or_declared_product_or_envd_is_refused(self):
        first = next((self.packages / "packages/accelerator/assets").iterdir())
        original = first.read_bytes()
        first.write_bytes(original + b"corrupted")
        with self.assertRaisesRegex(ValueError, "validated package bytes changed"):
            self.delta("corrupt-archive")
        first.write_bytes(original)
        for name, message in (("manifest-ctl", "packaged product differs"), ("embedded/envd", "embedded envd differs")):
            with self.subTest(name=name):
                original_digest = self.cold["products"][name]["sha256"]
                self.cold["products"][name]["sha256"] = "0" * 64
                task.write(self.packages / "result.json", self.cold)
                with self.assertRaisesRegex(ValueError, message):
                    self.delta(name.replace("/", "-"))
                self.cold["products"][name]["sha256"] = original_digest
        task.write(self.packages / "result.json", self.cold)

    def test_helper_revision_or_frozen_plan_mismatch_is_refused(self):
        path = self.helpers / "integration-helpers/helpers.json"
        helper = json.loads(path.read_text())
        name = next(iter(helper["helpers"]))
        helper["helpers"][name]["source_sha"] = "e" * 40
        task.write(path, helper)
        with self.assertRaisesRegex(ValueError, "compiled helper identity changed"):
            self.delta("helper-mismatch")
        (self.plan_directory / "integration-plan.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "frozen full integration plan changed"):
            self.delta("plan-mismatch")


class ArmPackagedInputContracts(PackagedInputContracts):
    ARCH = "aarch64"


if __name__ == "__main__":
    unittest.main(verbosity=2)
