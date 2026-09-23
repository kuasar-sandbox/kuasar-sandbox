#!/usr/bin/env python3
"""Contract regressions using real archives and EROFS, without running payloads."""
import copy
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import artifacts as subject
import transport


def test_revisions(sha="d" * 40):
    return subject.release_test_revisions({owner: sha for owner in subject.OWNERS if owner != "platform"}, sha)


def elf(arch, marker):
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<H", header, 18, {"x86_64": 62, "aarch64": 183}[arch])
    return bytes(header) + marker.encode()


def kernel(arch):
    if arch == "x86_64":
        return elf(arch, "kernel")
    header = bytearray(64)
    struct.pack_into("<Q", header, 16, 4096)
    header[56:60] = b"ARM\x64"
    return bytes(header) + bytes(4096 - 64)


def archive(path, files):
    with tarfile.open(path, "w:gz") as output:
        for name, content in files.items():
            entry = tarfile.TarInfo("./" + name)
            entry.size, entry.mode = len(content), 0o755
            output.addfile(entry, io.BytesIO(content))
    return {"name": path.name, "size": path.stat().st_size, "digest": "sha256:" + subject.digest(path)}


def runtime(root, arch, files):
    tree = root / f"runtime-{arch}"
    for name, source in (("sbin/init", "sandbox-init"), ("opt/sandbox-runtime/bin/flatten-ctl", "flatten-ctl"),
                         ("opt/sandbox-runtime/bin/mkfs.erofs", "mkfs.erofs"), ("opt/sandbox-runtime/bin/envd", None)):
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files[source] if source else elf(arch, "envd"))
        path.chmod(0o755)
    image = root / f"{arch}.erofs"
    subprocess.run(["mkfs.erofs", "--all-root", "-T0", str(image), str(tree)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    def footer(digest):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as data:
            entry = zipfile.ZipInfo(".kuasar.digest." + digest, (1980, 1, 1, 0, 0, 0))
            entry.external_attr = 0o100444 << 16
            data.writestr(entry, b"")
        return output.getvalue()
    content = image.read_bytes()
    prefix = content + bytes((2 << 20) - len(footer("0" * 64)) - len(content))
    return prefix + footer(hashlib.sha256(prefix).hexdigest())


class ArtifactBuildContracts(unittest.TestCase):
    def test_helper_checkout_uses_test_pin_and_preserves_product_source(self):
        spec = importlib.util.spec_from_file_location("builder", Path(__file__).with_name("build-artifacts.py"))
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            def git(*args):
                return subprocess.check_output(["git", "-C", str(repository), *args])
            git("init", "-q")
            git("config", "user.name", "test-pin-fixture")
            git("config", "user.email", "test-pin@example.invalid")
            (repository / "cmd").mkdir()
            (repository / "cmd/product").write_bytes(b"unchanged product input")
            (repository / "go.mod").write_text("module cli-helper-fixture\n\ngo 1.26.1\n")
            test = repository / "internal/orch/capture_test.go"
            test.parent.mkdir(parents=True)
            test.write_text('package orch\nimport "testing"\n'
                            'func TestCapturePairCLIToPausedDatabase(t *testing.T) { t.Log("old CLI test") }\n')
            script = repository / "scripts/ci-e2e-build.sh"
            script.parent.mkdir()
            script.write_text('printf "old helper" > "$3/custom-proxy"\n')
            git("add", ".")
            git("commit", "-qm", "product and old helper")
            product = git("rev-parse", "HEAD").decode().strip()
            script.write_text('printf "pinned helper" > "$3/custom-proxy"\n')
            test.write_text(test.read_text().replace("old CLI test", "pinned CLI test"))
            git("add", ".")
            git("commit", "-qm", "helper-only change")
            pin = git("rev-parse", "HEAD").decode().strip()
            def checkout(name, sha, destination):
                destination.mkdir()
                if name == "kuasar-sandbox/orchestrator":
                    with tarfile.open(fileobj=io.BytesIO(git("archive", sha))) as tree:
                        tree.extractall(destination, filter="data")
            for rebuild in (False, True):
                plan = {"sources": test_revisions(product), "test_revisions": test_revisions(product),
                        "test_overlays": [], "product_sources":
                        {"node-ctl": {"kuasar-sandbox/orchestrator": product}} if rebuild else {},
                        "lanes": {"x86_64": {"profile": subject.profiles(["orchestrator"], "x86_64")}}}
                plan["test_revisions"]["orchestrator"]["sha"] = pin
                sources = root / str(rebuild)
                with patch.object(builder, "checkout", side_effect=checkout):
                    builder.materialize(plan, "x86_64", sources)
                    helper = builder.helper_sources(plan, "x86_64", sources)
                self.assertEqual(plan["sources"]["orchestrator"]["sha"], product)
                self.assertEqual((sources / "orchestrator/cmd/product").read_bytes(), b"unchanged product input")
                self.assertEqual(helper == sources, not rebuild)
                output = root / ("helper-" + str(rebuild))
                output.mkdir()
                subprocess.run(["bash", str(helper / "orchestrator/scripts/ci-e2e-build.sh"),
                                "fixtures", "x86_64", str(output)], check=True)
                self.assertEqual((output / "custom-proxy").read_bytes(), b"pinned helper")
                builder.build_orchestrator_cli_tests(helper, "x86_64", output, os.environ.copy())
                result = subprocess.check_output([str(output / "orch-cli.test"), "-test.v"], text=True)
                self.assertIn("pinned CLI test", result)
                self.assertNotIn("old CLI test", result)
                if rebuild:
                    self.assertIn("old helper", (sources / "orchestrator/scripts/ci-e2e-build.sh").read_text())

    def test_build_launches_nonexecutable_framework_helper_and_preserves_failure(self):
        spec = importlib.util.spec_from_file_location("builder", Path(__file__).with_name("build-artifacts.py"))
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        plan = {"schema": 1, "framework_sha": "a" * 40, "owners": ["platform"],
                "test_overlays": [], "product_sources": {}, "test_revisions": test_revisions(),
                "sources": test_revisions(),
                "lanes": {arch: {"products": [], "profile": subject.profiles(["platform"], arch)}
                          for arch in subject.ARCHES}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "ci/hosted/exact-assets-tools.sh"
            helper.parent.mkdir(parents=True)
            (root / "test/e2e/lib").mkdir(parents=True)
            (root / "test/e2e/e2e").write_text("#!/usr/bin/env python3\n")
            (root / "test/e2e/e2e").chmod(0o755)
            (root / "test/e2e/lib/common.sh").write_text("#!/usr/bin/env bash\n")
            helper.write_text('#!/usr/bin/env bash\n'
                              'printf "%s\\n" "$TARGET_ARCH" > "$KUASAR_E2E_TOOL_OUTPUT/invoked"\n'
                              'exit 23\n')
            helper.chmod(0o644)  # Match the framework script's Git mode.
            output = root / "build-output"
            credentials = {key: "" for key in ("GH_TOKEN", "GITHUB_TOKEN", "CALLER_TOKEN", "KUASAR_CI_APP_PRIVATE_KEY")}
            with patch.object(builder, "ROOT", root), patch.object(builder, "materialize"), \
                 patch.object(builder.platform, "machine", return_value="x86_64"), \
                 patch.object(builder.subprocess, "check_output", return_value=("a" * 40 + "\n")), \
                 patch.dict(os.environ, credentials):
                with self.assertRaises(subprocess.CalledProcessError) as failure:
                    builder.build(plan, "x86_64", root / "assets", root / "sources", output)
            self.assertEqual(failure.exception.returncode, 23)
            self.assertEqual((output / "helpers/invoked").read_text(), "x86_64\n")
            self.assertFalse((output / "outputs.json").exists())


class ArtifactExecutionContracts(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("executor", Path(__file__).with_name("run-artifact-tests.py"))
        self.executor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.executor)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "bin").mkdir()
        (self.root / "provenance.json").write_text("{}")
        self.case = "test/e2e/accelerator/run_all.sh"
        script = self.root / self.case
        script.parent.mkdir(parents=True)
        script.write_text('set -eu\nprintf "%s" "$TMPDIR" > "$BIN/scratch"\n'
                          'sudo -n install -d -o root -g root -m 700 "$TMPDIR/owned"\n'
                          'sudo -n touch "$TMPDIR/owned/source-set.json"\n')
        self.plan = {"lanes": {"x86_64": {}}, "test_revisions": test_revisions()}
        self.provenance = {"profile": {"cases": [self.case]}, "helpers": {}, "embedded": {"init": "a" * 64},
                           "test_revisions": test_revisions()}
        self.result = self.root / "result.json"
        self.addCleanup(self.remove_scratch)

    def remove_scratch(self):
        marker = self.root / "bin/scratch"
        if marker.exists():
            state = Path(marker.read_text()).parent
            self.assertEqual(state.parent, Path("/var/tmp"))
            self.assertTrue(state.name.startswith("ki-"))
            subprocess.run(["sudo", "-n", "rm", "-rf", "--", str(state)], check=True)

    def execute(self, shard="core"):
        credentials = {key: "" for key in ("GH_TOKEN", "GITHUB_TOKEN", "CALLER_TOKEN", "KUASAR_CI_APP_PRIVATE_KEY")}
        with patch.object(self.executor.artifacts, "verify_workspace", return_value=self.provenance), \
             patch.object(self.executor.platform, "machine", return_value="x86_64"), patch.dict(os.environ, credentials):
            return self.executor.execute(self.plan, "x86_64", shard, self.root, self.result)

    def test_orchestrator_receives_prepared_cli_helper_and_selected_products(self):
        case = "test/e2e/orchestrator/run_all.sh"
        script = self.root / case
        script.parent.mkdir(parents=True)
        script.write_text('set -eu\n[ "$ORCH_CLI_TEST_BIN" = "$(dirname "$BIN")/fixtures/bin/orch-cli.test" ]\n')
        self.provenance["profile"]["cases"] = [case]
        self.provenance["helpers"] = {"orch-cli.test": {}}
        self.assertEqual(self.execute("orchestrator")["conclusion"], "success")

    def test_working_set_receives_exact_source_manifest_and_preserves_exit(self):
        case = "test/e2e/sandboxer/run_all.sh"
        script = self.root / case
        script.parent.mkdir(parents=True)
        script.write_text("exit 0\n")
        self.provenance["profile"]["cases"] = [case]
        self.plan["sources"] = test_revisions("c" * 40)
        self.plan["lanes"]["x86_64"]["extra_checks"] = {"sandboxer": ["working-set-smoke"]}
        perf = self.root / "test/perf"
        perf.mkdir()
        (perf / "working-set-netns.sh").write_text('exec bash "$@"\n')
        (perf / "sandbox-perf-working-set.sh").write_text(
            'set -eu\ncp "$KUASAR_REVISION_MANIFEST" "$BIN/recorded-revisions.tsv"\n'
            'exit "$TEST_WORKING_SET_EXIT"\n')
        archive = self.root / "image.tar"
        archive.write_bytes(b"prepared image")
        identity = "sha256:" + "b" * 64
        self.provenance["images"] = {"python": {"archive": archive.name, "sha256": subject.digest(archive),
                                               "image_id": identity, "platform": "linux/amd64"}}
        original_run = subprocess.run
        def run(command, **kwargs):
            if command[:3] == ["docker", "image", "load"]:
                return subprocess.CompletedProcess(command, 0)
            return original_run(command, **kwargs)
        image = [{"Id": identity, "Os": "linux", "Architecture": "amd64"}]
        for exit_code in (0, 41):
            with self.subTest(exit_code=exit_code), patch.dict(os.environ, TEST_WORKING_SET_EXIT=str(exit_code)), \
                 patch.object(self.executor.subprocess, "run", side_effect=run), \
                 patch.object(self.executor.subprocess, "check_output", return_value=json.dumps(image).encode()):
                if exit_code:
                    with self.assertRaisesRegex(ValueError, "selected working-set smoke failed"):
                        self.execute("sandboxer")
                else:
                    self.execute("sandboxer")
                result = json.loads(self.result.read_text())
                self.assertEqual(result["conclusion"], "failure" if exit_code else "success")
                self.assertEqual([item["exit_code"] for item in result["timings"]], [0, exit_code])
                with (self.root / "bin/recorded-revisions.tsv").open() as manifest:
                    rows = list(csv.DictReader(manifest, delimiter="\t"))
                self.assertEqual(rows, [{"repository": record["repository"], "requested_ref": record["sha"],
                                         "resolved_sha": record["sha"], "role": record["role"]}
                                        for record in self.plan["sources"].values()])

    def test_privileged_case_state_is_removed_before_success(self):
        result = self.execute()
        self.assertEqual(result["conclusion"], "success")
        self.assertEqual(result["timings"][0]["exit_code"], 0)
        self.assertFalse(Path((self.root / "bin/scratch").read_text()).parent.exists())

    def test_cleanup_failure_keeps_result_failed(self):
        original_run = subprocess.run
        def run(command, **kwargs):
            if command[:5] == ["sudo", "-n", "rm", "-rf", "--"]:
                raise subprocess.CalledProcessError(23, command)
            return original_run(command, **kwargs)
        with patch.object(self.executor.shutil, "rmtree", side_effect=PermissionError("root-owned state")), \
             patch.object(self.executor.subprocess, "run", side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError) as failure:
                self.execute()
        self.assertEqual(failure.exception.returncode, 23)
        result = json.loads(self.result.read_text())
        self.assertEqual(result["timings"][0]["exit_code"], 0)
        self.assertEqual(result["conclusion"], "failure")

    def test_loaded_image_identity_and_platform_are_required_before_case_execution(self):
        archive = self.root / "image.tar"
        archive.write_bytes(b"prepared image bytes")
        identity = "sha256:" + "b" * 64
        self.provenance["images"] = {"python": {"archive": archive.name, "sha256": subject.digest(archive),
                                               "image_id": identity, "platform": "linux/amd64"}}
        original_run = subprocess.run
        def run(command, **kwargs):
            if command[:3] == ["docker", "image", "load"]:
                return subprocess.CompletedProcess(command, 0)
            if command[0] == "bash":
                self.assertEqual(kwargs["env"]["E2E_IMAGE"], identity)
            return original_run(command, **kwargs)
        for record in ({"Id": identity, "Os": "linux", "Architecture": "arm64"},
                       {"Id": "sha256:" + "c" * 64, "Os": "linux", "Architecture": "amd64"}):
            with self.subTest(record=record), patch.object(self.executor.subprocess, "run", side_effect=run), \
                 patch.object(self.executor.subprocess, "check_output", return_value=json.dumps([record]).encode()):
                with self.assertRaisesRegex(ValueError, "loaded image differs"):
                    self.execute()
            self.assertEqual(json.loads(self.result.read_text())["timings"], [])
            self.assertEqual(json.loads(self.result.read_text())["conclusion"], "failure")
        record = {"Id": identity, "Os": "linux", "Architecture": "amd64"}
        with patch.object(self.executor.subprocess, "run", side_effect=run), \
             patch.object(self.executor.subprocess, "check_output", return_value=json.dumps([record]).encode()):
            self.assertEqual(self.execute()["conclusion"], "success")

    def test_changed_image_archive_is_rejected_before_docker_load(self):
        archive = self.root / "image.tar"
        archive.write_bytes(b"changed bytes")
        self.provenance["images"] = {"python": {"archive": archive.name, "sha256": "0" * 64}}
        with patch.object(self.executor.subprocess, "run") as command:
            with self.assertRaisesRegex(ValueError, "prepared image bytes changed"):
                self.execute()
        command.assert_not_called()
        self.assertEqual(json.loads(self.result.read_text())["conclusion"], "failure")


class ArtifactContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ("mkfs.erofs", "fsck.erofs", "dump.erofs"):
            if not shutil.which(tool):
                raise RuntimeError("required trusted host reader is missing: " + tool)
        if not os.environ.get("KUASAR_RUNTIME_READER"):
            raise RuntimeError("KUASAR_RUNTIME_READER must select the existing pinned Runtime verifier")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.plan = {
            "schema": 1, "framework_sha": "a" * 40, "owners": ["accelerator"],
            "baseline": {"version": "release-v1.2.3", "sha": "b" * 40, "units": {
                unit: {"version": (unit + "-" if unit in ("runtime", "vmlinux") else "") + "v1.2.3",
                       "sha": "c" * 40} for unit in subject.UNITS}, "assets": []},
            "lanes": {arch: {"products": ["manifest-ctl"], "profile": subject.profiles(["accelerator"], arch)}
                      for arch in subject.ARCHES},
            "test_overlays": ["accelerator"],
            "test_revisions": test_revisions(),
            "product_sources": {"manifest-ctl": {"accelerator": "e" * 40}},
        }
        self.files = {}
        for arch in subject.ARCHES:
            files = {name: elf(arch, name) for name in subject.PRODUCTS}
            files["vmlinux"] = kernel(arch)
            files["sandbox-runtime.bundle"] = runtime(self.root, arch, files)
            self.files[arch] = files
            for unit, selected in self.plan["baseline"]["units"].items():
                name = subject.archive_name(unit, selected["version"], arch)
                record = archive(self.assets / name, {"bin/" + name: data for name, data in files.items()
                                                      if subject.PRODUCTS[name] == unit})
                self.plan["baseline"]["assets"].append(record)
        tests = {f"test/e2e/{owner}/run_all.sh": b"#!/bin/sh\nexit 0\n" for owner in subject.OWNERS}
        tests["test/e2e/accelerator/lib/removed-helper.py"] = b"old helper"
        self.plan["baseline"]["assets"].append(archive(self.assets / "platform-release-v1.2.3.tar.gz", tests))

    def delta(self, arch="x86_64"):
        root = self.root / f"delta-{arch}"
        (root / "bin").mkdir(parents=True)
        records = {}
        for name in self.plan["lanes"][arch]["products"]:
            path = root / "bin" / name
            path.write_bytes(elf(arch, "candidate " + name))
            path.chmod(0o755)
            records[name] = {"sha256": subject.digest(path), "sources": self.plan["product_sources"][name]}
        tests = root / "test/e2e/accelerator"
        (tests / "lib").mkdir(parents=True)
        (tests / "run_all.sh").write_text("#!/bin/sh\nexit 0\n")
        (tests / "run_all.sh").chmod(0o755)
        (tests / "lib/new-helper.py").write_text("new helper")
        framework = root / "framework-tests"
        (framework / "lib").mkdir(parents=True)
        (framework / "e2e").write_text("#!/usr/bin/env python3\n")
        (framework / "e2e").chmod(0o755)
        (framework / "lib/common.sh").write_text("#!/usr/bin/env bash\n")
        self.metadata = {"plan_id": subject.identity(self.plan), "arch": arch, "products": records,
                         "test_revisions": self.plan["test_revisions"],
                         "framework_tests": subject.tree_files(framework),
                         "tests": {"accelerator": subject.tree_files(tests)}, "build_context": {"host": "x86_64"}}
        (root / "outputs.json").write_text(json.dumps(self.metadata))
        return root

    def compose(self, arch="x86_64", delta=None):
        return subject.compose(self.plan, arch, self.assets, delta or self.delta(arch), self.root / "workspaces" / arch)

    def test_two_isolated_architectures_retain_baseline_bytes_and_replace_complete_owner(self):
        for arch in subject.ARCHES:
            provenance = self.compose(arch)
            workspace = self.root / "workspaces" / arch
            self.assertEqual(provenance["products"]["store-ctl"]["sha256"],
                             hashlib.sha256(self.files[arch]["store-ctl"]).hexdigest())
            self.assertEqual(provenance["products"]["manifest-ctl"]["origin"], "candidate")
            self.assertFalse((workspace / "test/e2e/accelerator/lib/removed-helper.py").exists())
            self.assertTrue((workspace / "test/e2e/accelerator/lib/new-helper.py").exists())
            subject.verify_workspace(workspace, self.plan, arch)
        self.assertNotEqual(subject.digest(self.root / "workspaces/x86_64/bin/manifest-ctl"),
                            subject.digest(self.root / "workspaces/aarch64/bin/manifest-ctl"))

    def test_test_only_pin_changes_keep_every_product_byte_and_reject_mismatches(self):
        self.plan["product_sources"] = {}
        for lane in self.plan["lanes"].values():
            lane["products"] = []
        self.plan["test_revisions"]["accelerator"]["sha"] = "f" * 40
        for arch in subject.ARCHES:
            delta = self.delta(arch)
            wrong = copy.deepcopy(self.metadata)
            wrong["test_revisions"]["accelerator"]["sha"] = "c" * 40
            (delta / "outputs.json").write_text(json.dumps(wrong))
            with self.assertRaisesRegex(ValueError, "build test pins"):
                self.compose(arch, delta)
            (delta / "outputs.json").write_text(json.dumps(self.metadata))
            provenance = self.compose(arch, delta)
            for name, product in provenance["products"].items():
                self.assertEqual(product["origin"], "baseline")
                self.assertEqual(product["sha256"], hashlib.sha256(self.files[arch][name]).hexdigest())
            workspace = self.root / "workspaces" / arch
            self.assertEqual((workspace / "test/e2e/accelerator/lib/new-helper.py").read_text(), "new helper")
            self.assertEqual(provenance["test_revisions"], self.plan["test_revisions"])
            broken = json.loads((workspace / "provenance.json").read_text())
            broken["test_revisions"]["accelerator"]["sha"] = "c" * 40
            (workspace / "provenance.json").write_text(json.dumps(broken))
            with self.assertRaisesRegex(ValueError, "prepared test pins"):
                subject.verify_workspace(workspace, self.plan, arch)
        for wrong in ({}, {**test_revisions(), "orchestrator": {"repository": "other/repo", "sha": "c" * 40, "role": "release"}}):
            with self.assertRaisesRegex(ValueError, "test pin"):
                subject.validate_test_revisions(wrong)

    def test_release_and_later_baseline_preserve_independent_test_pins(self):
        spec = importlib.util.spec_from_file_location("resolver", Path(__file__).with_name("resolve-artifacts.py"))
        resolver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(resolver)
        binding_spec = importlib.util.spec_from_file_location("binding", Path(__file__).resolve().parents[2] / "release/bind-validation.py")
        binding = importlib.util.module_from_spec(binding_spec)
        binding_spec.loader.exec_module(binding)
        version, sha = self.plan["baseline"]["version"], self.plan["baseline"]["sha"]
        units = self.plan["baseline"]["units"]
        pins = {owner: "f" * 40 for owner in subject.OWNERS if owner != "platform"}
        manifest = "version: " + version + "\ncomponents:\n" + "".join(
            f"  {unit}: {record['version']}\n" for unit, record in units.items())
        manifest += "test_revisions:\n" + "".join(f"  {owner}: {pin}\n" for owner, pin in pins.items())
        (self.root / "selection.tsv").write_text("".join(f"{unit}\t{units[unit]['version']}\n" for unit in subject.UNITS))
        pin_file = self.root / "test-revisions.json"
        pin_file.write_text(json.dumps(pins))
        (self.assets / "SHA256SUMS").write_text("".join(f"{value}  {name}\n" for name, value in subject.tree_files(self.assets).items()))
        with patch.object(resolver, "source_text", return_value=manifest), patch.object(resolver, "public"), \
             patch.object(resolver.release, "tag_sha", side_effect=lambda repo, tag: sha if repo == resolver.PLATFORM else "c" * 40), \
             patch.dict(os.environ, {"RELEASE_VERSION": version, "PLATFORM_SOURCE_SHA": sha}):
            plan = resolver.exact_assets_plan("a" * 40, self.root)
            self.assertEqual(plan["test_revisions"]["orchestrator"]["sha"], "f" * 40)
            self.assertEqual(plan["baseline"]["units"]["orchestrator"]["sha"], "c" * 40)
            pin_file.write_text(json.dumps({**pins, "orchestrator": "c" * 40}))
            with self.assertRaisesRegex(ValueError, "staged test pins"):
                resolver.exact_assets_plan("a" * 40, self.root)
            pin_file.unlink()
            with self.assertRaises(FileNotFoundError):
                resolver.exact_assets_plan("a" * 40, self.root)
            pin_file.write_text(json.dumps(pins))
            results = {arch: {"arch": arch, "plan_id": subject.identity(plan), "conclusion": "success",
                "test_revisions": plan["test_revisions"], "profile": plan["lanes"][arch]["profile"]} for arch in subject.ARCHES}
            wrong = copy.deepcopy(results)
            wrong["aarch64"]["test_revisions"]["orchestrator"]["sha"] = "c" * 40
            with self.assertRaisesRegex(ValueError, "result test pins"):
                subject.collect_results(plan, wrong)
            notes = self.root / "release-notes.md"
            notes.write_text("Exact stage\n")
            binding.bind(self.root, plan, subject.collect_results(plan, results))
            state = {"tag_name": version, "target_commitish": sha, "draft": False, "prerelease": False,
                "id": 1, "body": notes.read_text(), "assets": [dict(record, id=index, state="uploaded")
                    for index, record in enumerate(plan["baseline"]["assets"], 1)]}
            run = {"id": 1, "status": "completed", "conclusion": "success", "html_url": "https://example.invalid/run/1",
                   "display_title": resolver.release.aggregate_run_title(version, sha)}
            with patch.object(resolver.release, "api_optional", return_value=state), \
                 patch.object(resolver.release, "aggregate_runs", return_value=[run]):
                baseline = resolver.aggregate(version)
                self.assertEqual(baseline["test_revisions"], plan["test_revisions"])
                with patch.object(resolver, "baseline", return_value=baseline), \
                     patch.object(resolver, "changed_files", return_value=["docs/ci.md"]), \
                     patch.dict(os.environ, {"CANDIDATE_REPOSITORY": resolver.PLATFORM, "CANDIDATE_PR": "1",
                        "CANDIDATE_SHA": "e" * 40, "CANDIDATE_BASE_SHA": sha, "CANDIDATE_HEAD_SHA": "d" * 40,
                        "CANDIDATE_BASE_REF": "main", "COMPANION_CANDIDATES": "[]"}):
                    candidate = resolver.source_plan("a" * 40)
                self.assertEqual(candidate["test_revisions"]["orchestrator"], plan["test_revisions"]["orchestrator"])
                self.assertEqual(candidate["sources"]["orchestrator"]["sha"], "c" * 40)
                self.assertEqual(candidate["lanes"]["x86_64"]["products"], [])
                recorded = json.loads(resolver.PROFILE_BINDING.search(state["body"])[1])
                recorded["test_revisions"]["orchestrator"]["sha"] = "c" * 40
                state["body"] = "<!-- kuasar-integration-validation " + json.dumps(recorded) + " -->"
                with self.assertRaisesRegex(ValueError, "published test pins"):
                    resolver.aggregate(version)

    def test_baseline_and_candidate_tampering_fail(self):
        delta = self.delta()
        with (delta / "bin/manifest-ctl").open("ab") as file:
            file.write(b"tamper")
        with self.assertRaisesRegex(ValueError, "candidate digest"):
            self.compose(delta=delta)
        asset = self.assets / self.plan["baseline"]["assets"][0]["name"]
        with asset.open("ab") as file:
            file.write(b"tamper")
        with self.assertRaisesRegex(ValueError, "baseline asset digest"):
            self.compose(delta=delta)

    def test_unplanned_product_and_wrong_architecture_fail(self):
        delta = self.delta()
        (delta / "bin/connector-ctl").write_bytes(elf("x86_64", "unowned"))
        with self.assertRaisesRegex(ValueError, "undeclared candidate"):
            self.compose(delta=delta)
        (delta / "bin/connector-ctl").unlink()
        path = delta / "bin/manifest-ctl"
        path.write_bytes(elf("aarch64", "wrong target"))
        self.metadata["products"]["manifest-ctl"]["sha256"] = subject.digest(path)
        (delta / "outputs.json").write_text(json.dumps(self.metadata))
        with self.assertRaisesRegex(ValueError, "wrong ELF architecture"):
            self.compose(delta=delta)

    def test_candidate_init_must_reach_embedded_runtime(self):
        self.plan["lanes"]["x86_64"]["products"] = ["sandbox-init"]
        self.plan["product_sources"]["sandbox-init"] = {"sandboxer": "f" * 40}
        with self.assertRaisesRegex(ValueError, "embedded init differs"):
            self.compose()

    def test_usage_probe_is_a_required_target_helper(self):
        self.plan["owners"] = ["sandboxer"]
        for arch in subject.ARCHES:
            self.plan["lanes"][arch]["profile"] = subject.profiles(["sandboxer"], arch)
        delta = self.delta()
        with self.assertRaisesRegex(ValueError, "helper selection differs"):
            self.compose(delta=delta)
        helpers = subject.planned_helpers(self.plan["lanes"]["x86_64"]["profile"])
        self.assertEqual(helpers["usage-probe"], "sandboxer")
        (delta / "helpers").mkdir()
        self.metadata["helpers"] = {}
        for name, owner in helpers.items():
            path = delta / "helpers" / name
            path.write_bytes(elf("x86_64", name))
            path.chmod(0o755)
            self.metadata["helpers"][name] = {"sha256": subject.digest(path), "source_sha":
                self.plan["framework_sha"] if owner == "framework" else self.plan["test_revisions"][owner]["sha"]}
        (delta / "outputs.json").write_text(json.dumps(self.metadata))
        provenance = self.compose(delta=delta)
        probe = self.root / "workspaces/x86_64/fixtures/bin/usage-probe"
        self.assertEqual(subject.digest(probe), provenance["helpers"]["usage-probe"]["sha256"])
        probe.unlink()
        with self.assertRaisesRegex(ValueError, "prepared workspace changed"):
            subject.verify_workspace(self.root / "workspaces/x86_64", self.plan, "x86_64")

    def test_cli_helper_requires_exact_test_pin_and_survives_preparation(self):
        self.plan["owners"] = ["orchestrator"]
        for arch in subject.ARCHES:
            self.plan["lanes"][arch]["profile"] = subject.profiles(["orchestrator"], arch)
        self.plan["test_revisions"]["orchestrator"]["sha"] = "f" * 40
        delta = self.delta()
        helpers = subject.planned_helpers(self.plan["lanes"]["x86_64"]["profile"])
        self.assertEqual(helpers["orch-cli.test"], "orchestrator")
        self.assertNotIn("orch-cli.test", subject.planned_helpers(self.plan["lanes"]["aarch64"]["profile"]))
        with self.assertRaisesRegex(ValueError, "helper selection differs"):
            self.compose(delta=delta)
        (delta / "helpers").mkdir()
        self.metadata["helpers"] = {}
        for name, owner in helpers.items():
            path = delta / "helpers" / name
            path.write_bytes(elf("x86_64", name))
            path.chmod(0o755)
            self.metadata["helpers"][name] = {"sha256": subject.digest(path), "source_sha":
                self.plan["framework_sha"] if owner == "framework" else self.plan["test_revisions"][owner]["sha"]}
        helper = self.metadata["helpers"]["orch-cli.test"]
        helper["source_sha"] = "c" * 40
        (delta / "outputs.json").write_text(json.dumps(self.metadata))
        with self.assertRaisesRegex(ValueError, "test helper identity mismatch: orch-cli.test"):
            self.compose(delta=delta)
        helper["source_sha"] = "f" * 40
        (delta / "outputs.json").write_text(json.dumps(self.metadata))
        provenance = self.compose(delta=delta)
        prepared = self.root / "workspaces/x86_64/fixtures/bin/orch-cli.test"
        self.assertEqual(subject.digest(prepared), provenance["helpers"]["orch-cli.test"]["sha256"])
        prepared.unlink()
        with self.assertRaisesRegex(ValueError, "prepared workspace changed"):
            subject.verify_workspace(self.root / "workspaces/x86_64", self.plan, "x86_64")

    def test_guest_execution_binds_init_to_the_validated_runtime_bytes(self):
        # A legal unchanged runtime may embed a different init revision than the
        # separately published sandboxer unit. Candidate-init equality is checked
        # separately; execution must retain this baseline's embedded identity.
        files = dict(self.files["x86_64"])
        files["sandbox-init"] = elf("x86_64", "separately published init")
        name = subject.archive_name("sandboxer", "v1.2.3", "x86_64")
        record = archive(self.assets / name, {"bin/" + name: data for name, data in files.items()
                                            if subject.PRODUCTS[name] == "sandboxer"})
        self.plan["baseline"]["assets"] = [record if entry["name"] == name else entry
                                           for entry in self.plan["baseline"]["assets"]]
        provenance = self.compose()
        self.assertNotEqual(provenance["embedded"]["init"], provenance["products"]["sandbox-init"]["sha256"])
        spec = importlib.util.spec_from_file_location("executor", Path(__file__).with_name("run-artifact-tests.py"))
        executor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(executor)
        def execute(command, *, cwd, env):
            self.assertEqual(env["KUASAR_EXPECTED_RUNTIME_INIT_SHA256"], provenance["embedded"]["init"])
            return subprocess.CompletedProcess(command, 0)
        with patch.object(executor.platform, "machine", return_value="x86_64"), \
             patch.object(executor.subprocess, "run", side_effect=execute) as case:
            result = executor.execute(self.plan, "x86_64", "core", self.root / "workspaces/x86_64", self.root / "result.json")
            self.assertEqual(result["conclusion"], "success")
            self.assertEqual(case.call_count, 1)

    def test_missing_selected_inputs_cannot_fall_back_to_source(self):
        self.plan["baseline"]["assets"] = [record for record in self.plan["baseline"]["assets"]
                                                  if "aarch64" not in record["name"]]
        with self.assertRaisesRegex(ValueError, "explicit ARM initialization"):
            self.compose("aarch64")
        delta = self.delta()
        (delta / "test/e2e/accelerator/run_all.sh").unlink()
        self.metadata["tests"]["accelerator"].pop("run_all.sh")
        (delta / "outputs.json").write_text(json.dumps(self.metadata))
        with self.assertRaisesRegex(ValueError, "missing candidate owner entry"):
            self.compose(delta=delta)

    def test_orchestrator_config_and_app_are_product_inputs(self):
        expected = sorted(name for name, unit in subject.PRODUCTS.items() if unit == "orchestrator")
        for path in ("config/config.go", "app/proxy/proxy.go"):
            self.assertEqual(subject.changed_products({"orchestrator": [path]}), expected)
        for path in ("docs/orchestrator.md", "test/e2e/run_all.sh", "app/proxy/proxy_test.go"):
            self.assertEqual(subject.changed_products({"orchestrator": [path]}), [])

    def test_linked_product_closure_and_platform_test_only_change(self):
        self.assertEqual(subject.changed_products({"platform": ["test/e2e/run_all.sh"]}), [])
        self.assertEqual(subject.changed_products({"accelerator": ["docs/accelerator.md"]}), [])
        products = subject.changed_products({"sandboxer": ["cmd/sandbox-init/main.go"]})
        self.assertIn("sandbox-runtime.bundle", products)
        self.assertNotIn("node-ctl", products)
        products = subject.changed_products({"accelerator": ["pkg/flatten/flatten.go"]})
        self.assertIn("flatten-ctl", products)
        self.assertIn("sandbox-runtime.bundle", products)
        self.assertNotIn("sandbox-ctl", products)

    def test_results_require_both_exact_architectures_and_predeclared_profiles(self):
        results = {arch: {"arch": arch, "plan_id": subject.identity(self.plan), "conclusion": "success",
                          "test_revisions": self.plan["test_revisions"],
                          "profile": self.plan["lanes"][arch]["profile"]} for arch in subject.ARCHES}
        subject.collect_results(self.plan, results)
        with self.assertRaisesRegex(ValueError, "both architecture"):
            subject.collect_results(self.plan, {"x86_64": results["x86_64"]})
        results["aarch64"] = copy.deepcopy(results["aarch64"])
        results["aarch64"]["profile"]["cases"] = []
        with self.assertRaisesRegex(ValueError, "did not pass"):
            subject.collect_results(self.plan, results)

    def test_shards_require_all_selected_cases_and_one_prepared_identity(self):
        self.plan["owners"] = ["platform"]
        for arch in subject.ARCHES:
            self.plan["lanes"][arch]["profile"] = subject.profiles(["platform"], arch)
        arch = "x86_64"
        results = {shard: {"arch": arch, "shard": shard, "plan_id": subject.identity(self.plan),
                          "test_revisions": self.plan["test_revisions"],
                          "cases": cases, "conclusion": "success", "provenance_sha256": "a" * 64}
                   for shard, cases in subject.shards(self.plan["lanes"][arch]["profile"]).items()}
        subject.collect_shard_results(self.plan, arch, results)
        incomplete = dict(results)
        del incomplete["sandboxer"]
        with self.assertRaisesRegex(ValueError, "shard results"):
            subject.collect_shard_results(self.plan, arch, incomplete)
        results["orchestrator"]["provenance_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "different prepared"):
            subject.collect_shard_results(self.plan, arch, results)

    def test_transport_rejects_unsafe_inputs_and_preserves_executable_mode(self):
        path = self.root / "transport.tar.gz"
        with tarfile.open(path, "w:gz") as output:
            entry = tarfile.TarInfo("bin/tool")
            entry.size, entry.mode = len(b"executable"), 0o755
            output.addfile(entry, io.BytesIO(b"executable"))
        destination = self.root / "transport"
        transport.extract(path, destination)
        self.assertEqual((destination / "bin/tool").read_bytes(), b"executable")
        self.assertEqual((destination / "bin/tool").stat().st_mode & 0o777, 0o755)
        for name, kind, mode in (("../escape", tarfile.REGTYPE, 0o644),
                                 ("link", tarfile.SYMTYPE, 0o644),
                                 ("writable", tarfile.REGTYPE, 0o777)):
            with self.subTest(name=name), tarfile.open(path, "w:gz") as output:
                entry = tarfile.TarInfo(name)
                entry.type, entry.mode, entry.linkname = kind, mode, "/outside"
                output.addfile(entry)
            with self.assertRaises(ValueError):
                transport.extract(path, self.root / "rejected")
            self.assertFalse((self.root / "rejected").exists())

    def test_publication_binds_both_results_to_unchanged_stage_bytes(self):
        spec = importlib.util.spec_from_file_location("binding", Path(__file__).resolve().parents[2] / "release/bind-validation.py")
        binding = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(binding)
        self.plan["mode"] = "exact-assets"
        self.plan["baseline"]["staged"] = True
        results = {arch: {"arch": arch, "plan_id": subject.identity(self.plan), "conclusion": "success",
                          "test_revisions": self.plan["test_revisions"],
                          "profile": self.plan["lanes"][arch]["profile"]} for arch in subject.ARCHES}
        validation = subject.collect_results(self.plan, results)
        notes = self.root / "release-notes.md"
        (self.root / "test-revisions.json").write_text(json.dumps(
            {owner: record["sha"] for owner, record in self.plan["test_revisions"].items() if owner != "platform"}))
        notes.write_text("Original release notes\n")
        hashes = subject.tree_files(self.assets)
        binding.bind(self.root, self.plan, validation)
        self.assertEqual(subject.tree_files(self.assets), hashes)
        self.assertTrue(notes.read_text().startswith("Original release notes\n"))
        notes.write_text("Original release notes\n")
        asset = self.assets / next(iter(hashes))
        with asset.open("ab") as output:
            output.write(b"changed after validation")
        with self.assertRaisesRegex(ValueError, "publisher bytes differ"):
            binding.bind(self.root, self.plan, validation)
        self.assertEqual(notes.read_text(), "Original release notes\n")

    def test_connector_source_failures_block_connector_and_platform(self):
        spec = importlib.util.spec_from_file_location("source_checks", Path(__file__).with_name("run-source-checks.py"))
        source_checks = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(source_checks)
        self.plan["sources"] = {owner: {"repository": "kuasar-sandbox/" + owner, "sha": "c" * 40}
                                for owner in subject.OWNERS}
        def checkout(repository, sha, destination):
            destination.mkdir()
        def execute(command, *, cwd, env):
            code = 33 if cwd.name == "connector" and command == ["bash", "scripts/ci-source-checks.sh"] else 0
            return subprocess.CompletedProcess(command, code)
        for owner in ("connector", "platform"):
            self.plan["owners"] = [owner]
            for arch in subject.ARCHES:
                self.plan["lanes"][arch]["profile"] = subject.profiles([owner], arch)
            result = self.root / (owner + "-source-result.json")
            with patch.object(source_checks.build, "checkout", side_effect=checkout), \
                 patch.object(source_checks.subprocess, "run", side_effect=execute):
                with self.assertRaisesRegex(ValueError, "required source check failed: connector-unit-race-vet"):
                    source_checks.execute(self.plan, self.root / (owner + "-sources"), result)
            record = json.loads(result.read_text())
            self.assertEqual(record["conclusion"], "failure")
            self.assertEqual(record["checks"][-1]["name"], "connector-unit-race-vet")
            self.assertEqual(record["checks"][-1]["exit_code"], 33)


if __name__ == "__main__":
    unittest.main()
