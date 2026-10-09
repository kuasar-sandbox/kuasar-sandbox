#!/usr/bin/env python3
"""Focused guards for the temporary, non-Workbench legacy control."""
from contextlib import contextmanager, ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import yaml

SPEC = importlib.util.spec_from_file_location("task216_legacy", Path(__file__).with_name("verify-workbench-216.py"))
task = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(task)
BOOTSTRAP = b"exact admitted bootstrap fixture\n"


@contextmanager
def fixture(arch="x86_64", *, memory="8589934592", quota="200000 100000"):
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory)
        private = root / "task"
        sources = private / "sources"
        legacy = root / "legacy"
        sources.mkdir(parents=True)
        (legacy / "ci/hosted").mkdir(parents=True)
        bootstrap = BOOTSTRAP
        (legacy / "ci/hosted/bootstrap.sh").write_bytes(bootstrap)
        record = {"framework_sha": "a" * 40, "sources": {owner: {"repository": repository, "sha": "a" * 40}
                              for owner, repository in task.REPOSITORIES.items()}}
        task.write(sources / "frozen.json", record)
        task.write(sources / "integration-plan.json", {"fixture": True})
        for owner in record["sources"]:
            (sources / owner).mkdir()
        env = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_TEMP": str(root),
               "KUASAR_BUILD_JOBS": "2", "GOMAXPROCS": "2", "CARGO_BUILD_JOBS": "2",
               "CMAKE_BUILD_PARALLEL_LEVEL": "2", "GOFLAGS": "-p=2", "HOME": str(private / "home"),
               "GOTOOLCHAIN": "auto",
               "TMPDIR": str(private / "tmp"), "GOPATH": str(private / "home/go"),
               "GOCACHE": str(private / "home/go-cache"), "GOMODCACHE": str(private / "home/go/pkg/mod"),
               "CARGO_HOME": str(private / "home/.cargo")}
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            special = {"/proc/self/cgroup": "0::/task\n", "/sys/fs/cgroup/task/memory.max": memory + "\n",
                       "/sys/fs/cgroup/task/cpu.max": quota + "\n",
                       "/proc/self/status": "NoNewPrivs:\t1\nCapEff:\t0000000000000000\nCapBnd:\t0000000000000000\n"}
            return special[str(path)] if str(path) in special else original_read(path, *args, **kwargs)

        def output(argv, **kwargs):
            if "rev-parse" in argv:
                return task.LEGACY_FRAMEWORK if argv[2] == legacy else "a" * 40
            return ""

        stack.enter_context(patch.dict(os.environ, env, clear=True))
        stack.enter_context(patch.object(task, "LEGACY_BOOTSTRAP_SHA256", hashlib.sha256(bootstrap).hexdigest()))
        stack.enter_context(patch.object(task.os, "getuid", return_value=1001))
        stack.enter_context(patch.object(task.os, "getpid", return_value=1))
        stack.enter_context(patch.object(task.os, "statvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)))
        stack.enter_context(patch.object(task.os, "access", return_value=False))
        stack.enter_context(patch.object(task.os, "sched_getaffinity", return_value={0, 1}))
        stack.enter_context(patch.object(task.platform, "machine", return_value="x86_64"))
        stack.enter_context(patch.object(Path, "read_text", read))
        stack.enter_context(patch.object(task, "output", side_effect=output))
        stack.enter_context(patch.object(task.subprocess, "check_output", return_value=bootstrap))
        stack.enter_context(patch.object(task.subprocess, "run"))
        stack.enter_context(patch.object(task, "frozen", return_value=record))
        yield SimpleNamespace(arch=arch, sources=sources, legacy_framework=legacy), record, private


class LegacyControl(unittest.TestCase):
    def setUp(self):
        # Resource fixtures must not query the developer's Docker daemon or
        # add du invocations to the isolation/cleanup command expectations.
        capture = patch.object(task, "space_snapshot", return_value={"phase": "fixture"})
        self.space = capture.start()
        self.addCleanup(capture.stop)

    def test_both_targets_keep_the_original_x86_host_and_no_workbench_identity(self):
        for arch in ("x86_64", "aarch64"):
            with self.subTest(arch=arch), fixture(arch) as (args, record, private):
                actual, frozen, memory, _ = task.legacy_inputs(args)
                self.assertEqual(actual, args.sources)
                self.assertEqual(frozen, record)
                self.assertEqual(memory, 8 * 1024**3)
                self.assertTrue((private / "home/.cargo").is_dir())

    def test_credentials_and_fake_workbench_identity_are_rejected_before_execution(self):
        for env, message in (({"GH_TOKEN": "fixture"}, "credentials"),
                             ({"KUASAR_WORKBENCH_IMAGE_ID": "sha256:fixture"}, "Workbench identity")):
            with self.subTest(env=list(env)), fixture() as (args, _, _), patch.dict(os.environ, env):
                with self.assertRaisesRegex(ValueError, message):
                    task.legacy_inputs(args)

    def test_foreign_native_runner_and_unbounded_parallelism_are_rejected(self):
        with fixture("aarch64") as (args, _, _), patch.object(task.platform, "machine", return_value="aarch64"):
            with self.assertRaisesRegex(ValueError, "Hosted x86"):
                task.legacy_inputs(args)
        with fixture() as (args, _, _), patch.object(task.os, "sched_getaffinity", return_value=set(range(4))):
            with self.assertRaisesRegex(ValueError, "two-job"):
                task.legacy_inputs(args)

    def test_modified_legacy_bootstrap_and_warm_candidate_state_are_rejected(self):
        with fixture() as (args, _, _):
            (args.legacy_framework / "ci/hosted/bootstrap.sh").write_text("changed bootstrap")
            with self.assertRaisesRegex(ValueError, "bootstrap bytes changed"):
                task.legacy_inputs(args)
        with fixture() as (args, _, private):
            (private / "home/go-cache").mkdir(parents=True)
            (private / "home/go-cache/entry").write_text("old compilation")
            with self.assertRaisesRegex(ValueError, "warm directory"):
                task.legacy_inputs(args)

    def test_declared_budget_requires_actual_private_cgroup_limits(self):
        for memory, quota in (("max", "200000 100000"), ("17179869184", "200000 100000"),
                              ("8589934592", "max 100000"), ("8589934592", "400000 100000")):
            with self.subTest(memory=memory, quota=quota), fixture(memory=memory, quota=quota) as (args, _, _):
                with self.assertRaisesRegex(ValueError, "task-local 2 CPU / 8 GiB"):
                    task.legacy_inputs(args)

    def test_readonly_root_and_private_pid_are_actual_admission_requirements(self):
        with fixture() as (args, _, _), patch.object(task.os, "getpid", return_value=123):
            with self.assertRaisesRegex(ValueError, "private PID"):
                task.legacy_inputs(args)
        with fixture() as (args, _, _), patch.object(task.os, "statvfs", return_value=SimpleNamespace(f_flag=0)):
            with self.assertRaisesRegex(ValueError, "read-only-root"):
                task.legacy_inputs(args)

    def test_ignored_build_inputs_cannot_masquerade_as_a_cold_clean_checkout(self):
        with fixture() as (args, _, _):
            original = task.output

            def output(argv, **kwargs):
                return "bin/x86_64/prebuilt" if "--ignored" in argv else original(argv, **kwargs)

            with patch.object(task, "output", side_effect=output):
                with self.assertRaisesRegex(ValueError, "ignored build inputs"):
                    task.legacy_inputs(args)

    def test_shared_compile_and_package_keep_exit_and_distinct_product_identity(self):
        for fail in (None, "helper", "package"):
            with self.subTest(fail=fail), fixture("aarch64") as (args, _, private):
                rows = [("guest-runtime", name) for name in task.artifacts.PRODUCTS]
                exact_products = {name: {"sha256": "b" * 64} for _, name in rows}

                def command(evidence, label, argv, **kwargs):
                    evidence.record["stages"].append({"stage": label, "argv": list(map(str, argv)), "exit_code": 0})

                def helpers(sources, arch, plan, destination, evidence, environment, producer):
                    self.assertEqual(environment["GOCACHE"], str(private / "home/go-cache"))
                    self.assertEqual(environment["GOMODCACHE"], str(private / "home/go/pkg/mod"))
                    self.assertEqual(environment["RUNNER_TEMP"], str(private / "tmp"))
                    self.assertEqual(environment["GOWORK"], str(args.sources / "go.work"))
                    self.assertIsNone(producer["image_id"])
                    self.assertEqual(producer["host_arch"], "x86_64")
                    self.assertIn("full-manifest", [row["stage"] for row in evidence.record["stages"]])
                    evidence.record["stages"].append({"stage": "helper-build", "exit_code": 17 if fail == "helper" else 0})
                    if fail == "helper":
                        raise subprocess.CalledProcessError(17, ["exact-helper-fixture"])
                    task.write(destination / "helpers.json", {"fixture": "same private process/cache"})

                with patch.object(task, "manifest", return_value=rows), \
                        patch.object(task, "products", return_value=exact_products), \
                        patch.object(task.Evidence, "run", command), \
                        patch.object(task, "helper_payload", side_effect=helpers), \
                        patch.object(task, "package_all", side_effect=ValueError("package failure") if fail == "package" else None) as package, \
                        patch.object(task, "native_cache", side_effect=AssertionError("legacy must not use Workbench cache")):
                    if fail == "helper":
                        with self.assertRaises(subprocess.CalledProcessError) as error:
                            task.legacy_cold(args)
                        self.assertEqual(error.exception.returncode, 17)
                    elif fail == "package":
                        with self.assertRaisesRegex(ValueError, "package failure"):
                            task.legacy_cold(args)
                    else:
                        task.legacy_cold(args)
                result = json.loads((private / "legacy-verification/result.json").read_text())
                self.assertEqual(result["conclusion"], "failure" if fail else "success")
                self.assertEqual(result["phase"], "legacy-cold")
                self.assertIsNone(result["image_id"])
                self.assertEqual(result["products"], exact_products)
                stages = [entry["stage"] for entry in result["stages"]]
                self.assertIn("full-build", stages)
                self.assertIn("full-manifest", stages)
                self.assertEqual(result["go_toolchain_mode"], "auto")
                self.assertIn("toolchain/go-effective", stages)
                self.assertTrue(all("validator/" + owner in stages for owner in task.VALIDATORS))
                if fail == "helper":
                    package.assert_not_called()
                else:
                    package.assert_called_once()
                    self.assertEqual(result["helpers_sha256"], task.artifacts.digest(private / "legacy-verification/integration-helpers/helpers.json"))

    def test_normal_workbench_admission_still_rejects_a_host_task_path(self):
        with fixture() as (args, _, _):
            with self.assertRaisesRegex(ValueError, "Workbench /src"):
                task.build_inputs(args)

    def test_host_stage_keeps_real_exit_and_owned_timeout_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = task.Evidence(Path(directory) / "host", {})
            status = task.legacy_stage(evidence, "real-failure", [sys.executable, "-c", "print('failure preserved'); exit(17)"])
            self.assertEqual(status, 17)
            self.assertEqual(evidence.record["stages"][-1]["exit_code"], 17)
            self.assertIn("failure preserved", (evidence.directory / "logs/real-failure.log").read_text())
            for failure, expected in ((subprocess.TimeoutExpired("fixture", 1), 124), (task.LegacyInterrupted(15), 143)):
                process = Mock(pid=123456)
                process.wait.side_effect = [failure, 0]
                with patch.object(task.subprocess, "Popen", return_value=process), patch.object(task.subprocess, "run") as kill:
                    self.assertEqual(task.legacy_stage(evidence, "interrupted-" + str(expected), ["fixture"]), expected)
                    kill.assert_called_once_with(["sudo", "-n", "kill", "-TERM", "--", "-123456"], check=False)

    def test_host_controller_keeps_selected_auto_toolchain_and_never_passes_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "task216-legacy.fixture"
            sources = private / "sources"
            sources.mkdir(parents=True)
            (sources / "frozen.json").write_text("exact frozen identity")
            legacy = root / "legacy"
            (legacy / "ci/hosted").mkdir(parents=True)
            (legacy / "ci/hosted/bootstrap.sh").write_bytes(BOOTSTRAP)
            tools = root / "provided/bin"
            tools.mkdir(parents=True)
            for name in ("go", "rustc", "cargo", "strace"):
                (tools / name).write_text("provided tool fixture")
            selected_mode = "go1.24.13+auto"
            calls = []

            def query(command, **kwargs):
                if command == ["go", "env", "GOTOOLCHAIN"]:
                    self.assertEqual(kwargs["env"]["GOWORK"], "off")
                    return selected_mode
                return task.LEGACY_FRAMEWORK

            def stage(evidence, label, command, *, environment=None, timeout=3600):
                calls.append((label, command))
                if label == "bootstrap":
                    self.assertEqual(command[-1], "artifact-cross")
                    separator = command.index("-i")
                    environment = dict(value.split("=", 1) for value in command[separator + 1:command.index("bash")])
                    self.assertFalse(any("TOKEN" in key or "SECRET" in key for key in environment))
                    self.assertEqual(environment["HOME"], str(private / "bootstrap-home"))
                    self.assertIn("--property=MemoryMax=8G", command)
                    self.assertIn("--property=CPUQuota=200%", command)
                    provision = Path(environment["RUNNER_TEMP"]) / "kuasar-hosted.fixture"
                    (provision / "bin").mkdir(parents=True)
                    Path(environment["GITHUB_ENV"]).write_text(
                        "KUASAR_BUILD_JOBS=2\nKUASAR_HOSTED_ROOT=" + str(provision)
                        + "\nKUASAR_RUNTIME_READER=" + str(provision / "bin/runtime-payloads.py") + "\n")
                else:
                    separator = command.index("-i")
                    environment_args = [value for value in command[separator + 1:] if isinstance(value, str) and "=" in value]
                    self.assertIn("GOTOOLCHAIN=" + selected_mode, environment_args)
                    self.assertNotIn("GOTOOLCHAIN=local", environment_args)
                    self.assertFalse(any("TOKEN=" in value or "SECRET=" in value for value in environment_args))
                    for guard in ("--property=ProtectHome=tmpfs", "--property=ProtectSystem=strict", "--property=MemoryMax=8G",
                                  "--property=CPUQuota=200%", "--mount-proc", "--pid", "--bounding-set=-all", "--no-new-privs"):
                        self.assertIn(guard, command)
                    self.assertIn("legacy-cold", command)
                return 0

            args = SimpleNamespace(arch="aarch64", sources=sources, legacy_framework=legacy, output=root / "host-output")
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
                                         "RUNNER_TEMP": str(root), "GH_TOKEN": "host-orchestration-fixture",
                                         "ACTIONS_RUNTIME_TOKEN": "host-cache-fixture"}), \
                    patch.object(task.platform, "machine", return_value="x86_64"), \
                    patch.object(task.os, "getuid", return_value=1001), \
                    patch.object(task.os, "sched_getaffinity", return_value={4, 5, 6, 7}), \
                    patch.object(task, "frozen", return_value={"framework_sha": "a" * 40}), \
                    patch.object(task, "LEGACY_BOOTSTRAP_SHA256", hashlib.sha256(BOOTSTRAP).hexdigest()), \
                    patch.object(task, "output", side_effect=query), \
                    patch.object(task.shutil, "which", side_effect=lambda name: str(tools / name)), \
                    patch.object(task, "stop_legacy_unit", return_value=0), \
                    patch.object(task, "retain_legacy_readers") as retained, \
                    patch.object(task, "legacy_stage", side_effect=stage):
                self.assertEqual(task.legacy_run(args), 0)
            self.assertEqual([label for label, _ in calls], ["bootstrap", "candidate"])
            receipt = json.loads((args.output / "host/result.json").read_text())
            self.assertEqual(receipt["conclusion"], "success")
            self.assertEqual(receipt["candidate_environment"]["GOTOOLCHAIN"], selected_mode)
            self.assertNotIn("GH_TOKEN", receipt["candidate_environment"])
            self.assertNotIn("GH_TOKEN", receipt["bootstrap_environment"])
            self.assertEqual(receipt["bootstrap_budget"]["memory_max"], 8 * 1024**3)
            self.assertEqual(retained.call_args.args[2], "x86_64")

    def test_foreign_unit_is_never_stopped(self):
        record = {"unit": "task216-legacy-" + "a" * 24 + ".service", "unit_description": "owned task"}
        inspected = SimpleNamespace(returncode=0, stdout="LoadState=loaded\nDescription=another task\n")
        with patch.object(task.subprocess, "run", return_value=inspected) as calls:
            with self.assertRaisesRegex(ValueError, "ownership changed"):
                task.stop_legacy_unit(record)
            self.assertEqual(calls.call_count, 1)
            self.assertEqual(calls.call_args.args[0][:2], ["systemctl", "show"])

    def test_bootstrap_failure_keeps_its_exit_when_owned_cleanup_also_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = root / "bootstrap.sh"
            bootstrap.write_bytes(BOOTSTRAP)
            evidence = task.Evidence(root / "host", {})
            with patch.object(task, "LEGACY_BOOTSTRAP_SHA256", hashlib.sha256(BOOTSTRAP).hexdigest()), \
                    patch.object(task, "legacy_stage", return_value=17), \
                    patch.object(task, "stop_legacy_unit", side_effect=ValueError("foreign owner")):
                self.assertEqual(task.legacy_bootstrap_stage(evidence, bootstrap, "artifact-build", [0, 1], root), 17)
            self.assertEqual(evidence.record["bootstrap_cleanup_exit_code"], 1)
            self.assertIn("foreign owner", evidence.record["bootstrap_cleanup_error"])

    def test_finish_attempts_both_owned_units_and_keeps_state_if_bootstrap_cannot_stop(self):
        for failure in (1, ValueError("foreign bootstrap owner")):
            with self.subTest(failure=str(failure)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                private = root / "task216-legacy.fixture"
                private.mkdir()
                destination = root / "output"
                task.write(destination / "host/result.json", {"owner_uid": os.getuid(), "framework_sha": "a" * 40,
                    "task_root": str(private), "conclusion": "running", "unit": "candidate", "unit_description": "owned candidate",
                    "bootstrap_unit": "bootstrap", "bootstrap_unit_description": "owned bootstrap"})
                seen = []

                def stop(record):
                    seen.append(record["unit"])
                    if record["unit"] == "bootstrap":
                        if isinstance(failure, Exception):
                            raise failure
                        return failure
                    return 0

                with patch.dict(os.environ, {"RUNNER_TEMP": str(root)}), \
                        patch.object(task, "output", return_value="a" * 40), \
                        patch.object(task, "stop_legacy_unit", side_effect=stop), \
                        patch.object(task.subprocess, "run", side_effect=AssertionError("must not delete active state")):
                    self.assertEqual(task.legacy_finish(SimpleNamespace(output=destination, task_root=private)), 1)
                self.assertEqual(seen, ["bootstrap", "candidate"])
                self.assertTrue(private.is_dir())
                self.assertFalse((destination / "validated").exists())
                receipt = json.loads((destination / "host/result.json").read_text())
                self.assertEqual(receipt["conclusion"], "failure")
                self.assertEqual(receipt["cleanup_exit_code"], 0)

    def test_finish_stops_before_safe_copy_and_refuses_links_or_fifos(self):
        for unsafe in (None, "link", "fifo"):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                private = root / "task216-legacy.fixture"
                candidate = private / "legacy-verification"
                candidate.mkdir(parents=True)
                (candidate / "result.json").write_text('{"conclusion":"success"}')
                (candidate / "packages").mkdir()
                secret = root / "private-host-file"
                secret.write_text("never follow")
                if unsafe == "link":
                    (candidate / "packages/unsafe").symlink_to(secret)
                if unsafe == "fifo":
                    os.mkfifo(candidate / "packages/unsafe")
                destination = root / "output"
                task.write(destination / "host/result.json", {"owner_uid": os.getuid(), "framework_sha": "a" * 40,
                    "task_root": str(private), "conclusion": "success"})
                events = []

                def stop(_record):
                    events.append("stop")
                    return 0

                shared = task.module("legacy_fixture_shared_copy", task.ROOT / "ci/hosted/workbench.py")
                real_copy = shared.copy_evidence

                def copy(source, target):
                    self.assertTrue(events and events[0] == "stop")
                    events.append("copy")
                    real_copy(source, target)

                def remove(command, **kwargs):
                    self.assertEqual(list(command[:5]), ["sudo", "-n", "rm", "-rf", "--"])
                    self.assertEqual(command[5], private)
                    shutil.rmtree(private)

                with patch.dict(os.environ, {"RUNNER_TEMP": str(root)}), \
                        patch.object(task, "output", return_value="a" * 40), \
                        patch.object(task, "stop_legacy_unit", side_effect=stop), \
                        patch.object(task, "module", return_value=SimpleNamespace(copy_evidence=copy)), \
                        patch.object(task.subprocess, "run", side_effect=remove):
                    status = task.legacy_finish(SimpleNamespace(output=destination, task_root=private))
                self.assertEqual(status, 0 if unsafe is None else 1)
                self.assertFalse(private.exists())
                self.assertEqual(secret.read_text(), "never follow")
                self.assertFalse((destination / "validated/packages/unsafe").exists())
                result = json.loads((destination / "host/result.json").read_text())
                self.assertEqual(result["conclusion"], "success" if unsafe is None else "failure")

    def test_temporary_workflow_reuses_gates_without_cross_control_dependencies(self):
        jobs = yaml.safe_load((task.ROOT / ".github/workflows/workbench-216-validation.yml").read_text())["jobs"]
        self.assertIs(jobs["legacy-x86"]["steps"], jobs["legacy-arm"]["steps"])
        for lane, arch in (("x86", "x86_64"), ("arm", "aarch64")):
            control = jobs["legacy-" + lane]
            self.assertEqual(control["runs-on"], "ubuntu-latest")
            self.assertEqual(control["env"]["TARGET_ARCH"], arch)
            self.assertEqual(control["needs"], "prepare")
            selected = jobs["prepare-legacy-" + lane]
            self.assertEqual(selected["needs"], ["prepare", "legacy-" + lane])
            self.assertEqual(selected["runs-on"], "ubuntu-24.04")
            self.assertEqual(selected["env"]["DELTA_COMMAND"], "legacy-packaged-delta")
            self.assertIs(selected["steps"], jobs["prepare-integration-" + lane]["steps"])
            e2e = jobs["e2e-legacy-" + lane]
            self.assertEqual(e2e["needs"], ["prepare", "prepare-legacy-" + lane] + (["legacy-readers-arm"] if lane == "arm" else []))
            self.assertEqual(e2e["strategy"], jobs["e2e-" + lane]["strategy"])
            self.assertIs(e2e["steps"], jobs["e2e-" + lane]["steps"])
            self.assertEqual(e2e["env"]["RESULT_PREFIX"], "workbench-216-legacy")
            self.assertIn("legacy-" + lane, jobs["validation-results"]["needs"])
            for step in control["steps"]:
                if "run" in step:
                    subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)
                if "GH_TOKEN" in step.get("env", {}):
                    self.assertEqual(step["id"], "inputs")
                if step.get("id") == "finish":
                    self.assertIn("always()", step["if"])
                    self.assertIn("--task-root", step["run"])
        self.assertIs(jobs["performance-legacy-x86"]["steps"], jobs["performance-x86"]["steps"])
        self.assertNotIn("performance-legacy-arm", jobs)
        readers = jobs["legacy-readers-arm"]
        self.assertEqual(readers["runs-on"], "ubuntu-24.04-arm")
        self.assertEqual(readers["needs"], "prepare")
        self.assertIn("legacy-readers-arm", jobs["validation-results"]["needs"])
        self.assertEqual([step["with"]["ref"] for step in readers["steps"] if step.get("uses", "").startswith("actions/checkout")],
                         ["${{ job.workflow_sha }}", task.LEGACY_FRAMEWORK])
        for step in readers["steps"]:
            if "run" in step:
                subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)
                self.assertNotIn("--profile source", step["run"])
            self.assertNotIn("GH_TOKEN", step.get("env", {}))
        for name in ("e2e-x86", "e2e-arm", "performance-x86"):
            steps = jobs[name]["steps"]
            self.assertTrue(any("${{ env.RESULT_PREFIX }}-readers-" in step.get("with", {}).get("name", "") for step in steps))
            self.assertTrue(any("check-legacy-readers" in step.get("run", "") for step in steps))
        for lane, arch in (("x86", "x86_64"), ("arm", "aarch64")):
            uploads = [step["with"]["name"] for step in jobs["helpers-" + lane]["steps"]
                       if step.get("uses", "").startswith("actions/upload-artifact")]
            self.assertIn("workbench-216-readers-" + arch + "-${{ github.run_id }}", uploads)


validation = task.module("legacy_existing_packaged_fixtures", task.ROOT / "ci/hosted/test-workbench-216-validation.py")
validation.task = task


class LegacyReaders(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "bootstrap-readers"
        (self.source / "bin").mkdir(parents=True)
        self.frozen = self.root / "frozen.json"
        task.write(self.frozen, {"framework_sha": "a" * 40})
        self.destination = self.root / "retained"
        self.populate(self.source, "aarch64")

    def populate(self, root, arch):
        (root / "bin").mkdir(parents=True, exist_ok=True)
        header = bytearray(64)
        header[:7] = b"\x7fELF\x02\x01\x01"
        struct.pack_into("<H", header, 18, {"x86_64": 62, "aarch64": 183}[arch])
        for name in task.READER_FILES[:3]:
            (root / "bin" / name).write_bytes(header)
        reader = Path(os.environ["KUASAR_RUNTIME_READER"])
        shutil.copy2(reader, root / "bin/runtime-payloads.py")
        (root / "erofs-readers.COPYING").write_text("reader source license fixture\n")

    def check(self, arch="aarch64"):
        args = SimpleNamespace(frozen=self.frozen, readers=self.destination, arch=arch)
        with patch.object(task, "frozen", return_value={"framework_sha": "a" * 40}):
            task.check_legacy_readers(args)

    def test_native_readers_keep_exact_files_and_reject_corruption_or_wrong_architecture(self):
        task.retain_legacy_readers(self.source, self.destination, "aarch64", self.frozen)
        self.check()
        with self.assertRaisesRegex(ValueError, "another frozen"):
            self.check("x86_64")
        target = self.destination / "fsck.erofs"
        original = target.read_bytes()
        target.write_bytes(original + b"corrupted")
        with self.assertRaisesRegex(ValueError, "another frozen"):
            self.check()
        target.write_bytes(original)
        os.mkfifo(self.destination / "unsafe")
        with self.assertRaisesRegex(ValueError, "unexpected legacy reader files"):
            self.check()

    def test_a_correct_receipt_cannot_change_the_reader_elf_architecture(self):
        task.retain_legacy_readers(self.source, self.destination, "aarch64", self.frozen)
        receipt_path = self.destination / "readers.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["arch"] = "x86_64"
        task.write(receipt_path, receipt)
        with self.assertRaisesRegex(ValueError, "wrong ELF architecture"):
            self.check("x86_64")

    def test_linked_trusted_reader_output_is_never_exported(self):
        target = self.source / "bin/fsck.erofs"
        target.unlink()
        target.symlink_to(self.frozen)
        with self.assertRaisesRegex(ValueError, "missing or linked"):
            task.retain_legacy_readers(self.source, self.destination, "aarch64", self.frozen)
        self.assertFalse(self.destination.exists())

    def test_source_profile_is_not_an_allowed_reader_or_product_bootstrap(self):
        evidence = task.Evidence(self.root / "host", {})
        with patch.object(task, "legacy_stage") as stage:
            with self.assertRaisesRegex(ValueError, "unapproved legacy bootstrap profile"):
                task.legacy_bootstrap_stage(evidence, self.root / "bootstrap.sh", "source", [0, 1], self.root)
        stage.assert_not_called()

    def test_reader_controller_keeps_arm_profile_failure_and_stops_before_retaining(self):
        for code in (0, 17):
            with self.subTest(code=code):
                private = self.root / ("task216-legacy.readers-" + str(code))
                private.mkdir()
                legacy = self.root / ("legacy-" + str(code))
                (legacy / "ci/hosted").mkdir(parents=True)
                (legacy / "ci/hosted/bootstrap.sh").write_bytes(BOOTSTRAP)
                args = SimpleNamespace(task_root=private, frozen=self.frozen, legacy_framework=legacy,
                                       output=self.root / ("output-" + str(code)))
                events = []

                def bootstrap(evidence, path, profile, cpus, root):
                    self.assertEqual(profile, "artifact-arm")
                    self.assertEqual(cpus, [4, 5])
                    self.assertEqual(root, private)
                    readers = evidence.directory / "provision/readers"
                    self.populate(readers, "aarch64")
                    (evidence.directory / "bootstrap.env").write_text(
                        "KUASAR_HOSTED_ROOT=" + str(readers) + "\nKUASAR_BUILD_JOBS=2\n")
                    events.append("bootstrap")
                    return code

                original_retain = task.retain_legacy_readers
                def retain(*arguments):
                    self.assertEqual(events, ["bootstrap", "stop"])
                    events.append("retain")
                    return original_retain(*arguments)

                with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
                                             "RUNNER_TEMP": str(self.root)}), \
                        patch.object(task.os, "getuid", return_value=1001), \
                        patch.object(task.os, "sched_getaffinity", return_value={4, 5, 6, 7}), \
                        patch.object(task.platform, "machine", return_value="aarch64"), \
                        patch.object(task, "frozen", return_value={"framework_sha": "a" * 40}), \
                        patch.object(task, "output", return_value=task.LEGACY_FRAMEWORK), \
                        patch.object(task, "LEGACY_BOOTSTRAP_SHA256", hashlib.sha256(BOOTSTRAP).hexdigest()), \
                        patch.object(task, "legacy_bootstrap_stage", side_effect=bootstrap), \
                        patch.object(task, "stop_legacy_unit", side_effect=lambda record: events.append("stop") or 0), \
                        patch.object(task, "retain_legacy_readers", side_effect=retain):
                    self.assertEqual(task.legacy_readers_run(args), code)
                receipt = json.loads((args.output / "host/result.json").read_text())
                self.assertEqual(receipt["exit_code"], code)
                self.assertEqual(receipt["conclusion"], "success" if code == 0 else "failure")
                self.assertEqual((private / "legacy-verification/readers/readers.json").exists(), code == 0)


class SpaceSnapshots(unittest.TestCase):
    def test_task_size_uses_allocated_blocks_and_does_not_follow_source_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sources"
            source.mkdir()
            with (source / "sparse").open("wb") as stream:
                stream.truncate(1024 * 1024)
            before = task.space_path(source, size=True)
            (root / "outside").write_bytes(b"x" * 32768)
            (source / "link").symlink_to(root / "outside")
            after = task.space_path(source, size=True)
            self.assertEqual(before["size_exit_code"], 0)
            self.assertEqual(after["size_exit_code"], 0)
            self.assertLess(after["allocated_bytes"], 32768)
            self.assertGreater(after["filesystem"]["total_bytes"], 0)

    def test_missing_state_is_explicit_and_measures_its_existing_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = task.space_path(root / "removed/state", size=True)
            self.assertIs(row["exists"], False)
            self.assertEqual(row["filesystem"]["measured_at"], str(root))
            self.assertNotIn("allocated_bytes", row)

    def test_linked_source_root_is_not_scanned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "link").symlink_to(root, target_is_directory=True)
            with patch.object(task.subprocess, "run") as run:
                row = task.space_path(root / "link", size=True)
            self.assertIn("linked", row["error"])
            self.assertNotIn("filesystem", row)
            run.assert_not_called()
            (root / "loop").symlink_to("loop")
            self.assertIn("error", task.space_path(root / "loop", size=True))

    def test_measurement_failure_is_recorded_without_a_fake_size(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(task.subprocess, "run", side_effect=subprocess.TimeoutExpired("du", 30)):
                row = task.space_path(Path(directory), size=True)
            self.assertIn("TimeoutExpired", row["error"])
            self.assertNotIn("allocated_bytes", row)
            self.assertIn("filesystem", row)

    def test_docker_root_uses_only_filesystem_counters_and_no_global_size_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            completed = SimpleNamespace(returncode=0, stdout=str(root) + "\n")
            with patch.object(task.subprocess, "run", return_value=completed) as run:
                row = task.space_snapshot("after", {}, docker=True)
            run.assert_called_once_with(["docker", "info", "--format", "{{.DockerRootDir}}"], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertIn("not peak", row["method"])
            self.assertEqual(row["docker_storage"]["path"], str(root))
            self.assertNotIn("allocated_bytes", row["docker_storage"])
            self.assertGreaterEqual(row["measurement_seconds"], 0)

    def test_missing_docker_is_explicit_and_no_zero_cost_is_invented(self):
        completed = SimpleNamespace(returncode=1, stdout="")
        with patch.object(task.subprocess, "run", return_value=completed):
            row = task.space_snapshot("before", {}, docker=True)
        self.assertEqual(row["docker_info_exit_code"], 1)
        self.assertIn("error", row["docker_storage"])
        self.assertNotIn("filesystem", row["docker_storage"])

    def test_host_receipt_stays_outside_candidate_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = root / "sources"
            sources.mkdir()
            args = SimpleNamespace(sources=sources, output=sources / "space.json", phase="before")
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_TEMP": str(root)}), \
                    patch.object(task.os, "getuid", return_value=1001), \
                    patch.object(task, "space_snapshot", return_value={}) as capture:
                with self.assertRaisesRegex(ValueError, "outside sources"):
                    task.disk_snapshot(args)
                capture.assert_not_called()
                args.output = root / "evidence/space.json"
                task.disk_snapshot(args)
                self.assertTrue(args.output.is_file())
                self.assertEqual(capture.call_args.args[0], "before")
                self.assertEqual(capture.call_args.args[1]["sources"], sources)
                with self.assertRaisesRegex(ValueError, "fresh host path"):
                    task.disk_snapshot(args)


class LegacyPackagedInputs(validation.PackagedInputContracts):
    def setUp(self):
        super().setUp()
        stages = ["workspace", "full-build", "full-manifest", "toolchain/go", "toolchain/go-effective", "helper-build"]
        if "basic.demo.sh" in self.plan["lanes"][self.arch]["selection"]["cases"]:
            stages.append("helper-wheels")
        stages += ["validator/" + owner for owner in task.VALIDATORS]
        stages += [operation + "/" + unit for unit in task.UNITS for operation in ("package", "validate")]
        self.cold.update(phase="legacy-cold", image_id=None, legacy_framework_sha=task.LEGACY_FRAMEWORK,
                         legacy_bootstrap_sha256=task.LEGACY_BOOTSTRAP_SHA256, host_arch="x86_64", uid=1001,
                         inputs=self.record, memory_max=8 * 1024**3, build_jobs=2, cpus=[0, 1],
                         go_toolchain_mode="auto", source_root="/private/task216-legacy.fixture/sources",
                         stages=[{"stage": stage, "exit_code": 0} for stage in stages])
        helper_path = self.helpers / "integration-helpers/helpers.json"
        helper = json.loads(helper_path.read_text())
        helper.update({key: self.cold[key] for key in (
            "image_id", "legacy_framework_sha", "legacy_bootstrap_sha256", "host_arch", "source_root")})
        task.write(helper_path, helper)
        self.cold["helpers_sha256"] = task.artifacts.digest(helper_path)
        task.write(self.packages / "result.json", self.cold)

    def delta(self, name="delta"):
        packaged = task.packaged_delta

        def legacy(args):
            args.command = "legacy-packaged-delta"
            return packaged(args)

        with patch.object(task, "packaged_delta", side_effect=legacy):
            return super().delta(name)

    def test_legacy_products_keep_same_job_helper_identity(self):
        delta = self.delta()
        context = json.loads((delta / "outputs.json").read_text())["build_context"]
        self.assertNotIn("workbench", context)
        self.assertEqual(context["legacy_control"]["legacy_framework_sha"], task.LEGACY_FRAMEWORK)
        self.assertNotIn("workbench", context["test_helpers"])
        self.assertEqual(context["test_helpers"]["legacy_control"], context["legacy_control"])

    def test_changed_or_workbench_helpers_cannot_replace_legacy_helpers(self):
        path = self.helpers / "integration-helpers/helpers.json"
        original = json.loads(path.read_text())
        for key, value in (("image_id", self.image), ("source_root", "/other/job"),
                           ("host_arch", "aarch64"), ("legacy_bootstrap_sha256", "0" * 64)):
            changed = {**original, key: value}
            task.write(path, changed)
            self.cold["helpers_sha256"] = task.artifacts.digest(path)
            task.write(self.packages / "result.json", self.cold)
            with self.assertRaisesRegex(ValueError, "same-job producer", msg=key):
                self.delta("helper-producer-" + key)
        task.write(path, original)
        with self.assertRaisesRegex(ValueError, "same-job producer"):
            self.delta("changed-helper-receipt")

    def test_helper_revision_or_frozen_plan_mismatch_is_refused(self):
        # Keep the same-job receipt matching so the original precise source-pin
        # assertion is still exercised, not masked by the stronger outer hash.
        path = self.helpers / "integration-helpers/helpers.json"
        helper = json.loads(path.read_text())
        name = next(iter(helper["helpers"]))
        helper["helpers"][name]["source_sha"] = "e" * 40
        task.write(path, helper)
        self.cold["helpers_sha256"] = task.artifacts.digest(path)
        task.write(self.packages / "result.json", self.cold)
        with self.assertRaisesRegex(ValueError, "compiled helper identity changed"):
            self.delta("helper-mismatch")
        (self.plan_directory / "integration-plan.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "frozen full integration plan changed"):
            self.delta("plan-mismatch")

    def test_workbench_entry_rejects_legacy_and_legacy_refuses_wrong_identity(self):
        with self.assertRaisesRegex(ValueError, "another frozen"):
            super().delta("pretend-workbench")
        for key, value in (("host_arch", "aarch64"), ("image_id", self.image), ("uid", 0),
                           ("legacy_bootstrap_sha256", "0" * 64), ("memory_max", 16 * 1024**3)):
            old = self.cold[key]
            self.cold[key] = value
            task.write(self.packages / "result.json", self.cold)
            with self.assertRaisesRegex(ValueError, "legacy packages lack", msg=key):
                self.delta("wrong-" + key)
            self.cold[key] = old
        self.cold["stages"][-1]["exit_code"] = 1
        task.write(self.packages / "result.json", self.cold)
        with self.assertRaisesRegex(ValueError, "stages did not all pass"):
            self.delta("failed-validate")


class ArmLegacyPackagedInputs(LegacyPackagedInputs):
    ARCH = "aarch64"


if __name__ == "__main__":
    unittest.main()
