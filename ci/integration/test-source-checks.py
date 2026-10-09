#!/usr/bin/env python3
"""Source-gate phase, exact-test-pin and failure-propagation regressions."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import artifacts
from test_fixtures import CASES, select_plan


SPEC = importlib.util.spec_from_file_location("source_checks", Path(__file__).with_name("run-source-checks.py"))
SUBJECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUBJECT)


class SourceChecksTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="source-phases-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sources = self.root / "sources"
        self.output = self.root / "result.json"
        self.plan = {"schema": 2, "mode": "source", "case_files": CASES,
                     "framework_sha": "a" * 40, "baseline": {},
                     "test_overlays": list(artifacts.OWNERS),
                     "test_revisions": artifacts.release_test_revisions(
                         {owner: "d" * 40 for owner in artifacts.OWNERS if owner != "platform"}, "e" * 40),
                     "sources": {owner: {"repository": "kuasar-sandbox/" + owner, "sha": "c" * 40}
                                 for owner in artifacts.OWNERS},
                     "lanes": {arch: {"products": []} for arch in artifacts.ARCHES}}
        select_plan(self.plan, ["platform"])
        self.credentials = patch.dict(os.environ, {name: "" for name in
            ("GH_TOKEN", "GITHUB_TOKEN", "CALLER_TOKEN", "KUASAR_CI_APP_PRIVATE_KEY")})
        self.credentials.start()
        self.addCleanup(self.credentials.stop)

    @staticmethod
    def checkout(repository, sha, destination):
        destination.mkdir()

    def result(self):
        return json.loads(self.output.read_text())

    def test_materialize_only_uses_exact_test_pins_and_transitive_closure(self):
        select_plan(self.plan, ["orchestrator"])
        with patch.object(SUBJECT.build, "checkout", side_effect=self.checkout) as checkout, \
             patch.object(SUBJECT.subprocess, "run") as run:
            SUBJECT.materialize(self.plan, self.sources, self.output)
        run.assert_not_called()
        expected = {"platform", "orchestrator", "sandboxer", "connector", "accelerator"}
        self.assertEqual({call.args[2].name for call in checkout.call_args_list}, expected)
        for call in checkout.call_args_list:
            pin = self.plan["test_revisions"][call.args[2].name]
            self.assertEqual(call.args[:2], (pin["repository"], pin["sha"]))
            self.assertNotEqual(call.args[1], "c" * 40)
        self.assertFalse((self.sources / "go.work").exists())
        result = self.result()
        self.assertEqual(result["phase"], "materialize")
        self.assertEqual(result["conclusion"], "success")
        self.assertEqual(result["checks"], [])
        self.assertEqual(set(result["sources"]), expected)
        self.assertTrue(all(row["exit_code"] == 0 for row in result["actions"]))

    def test_materialize_failure_records_partial_actions_and_real_exit(self):
        select_plan(self.plan, ["connector"])
        with patch.object(SUBJECT.build, "checkout", side_effect=subprocess.CalledProcessError(17, ["git", "fetch"])):
            with self.assertRaises(subprocess.CalledProcessError):
                SUBJECT.materialize(self.plan, self.sources, self.output)
        self.assertEqual(self.result()["exit_code"], 17)
        self.assertEqual(self.result()["actions"][-1]["exit_code"], 17)
        self.assertFalse((self.sources / SUBJECT.SOURCE_RECORD).exists())

    def run_phase(self, phase, *, failure=None):
        commands = []
        def run(command, *, cwd, env):
            commands.append((command, cwd, dict(env)))
            return subprocess.CompletedProcess(command, failure[1] if failure and command == failure[0] else 0)
        with patch.object(SUBJECT.build, "checkout", side_effect=self.checkout), \
             patch.object(SUBJECT.subprocess, "run", side_effect=run):
            SUBJECT.execute(self.plan, self.sources, self.output, phase=phase)
        return commands

    def test_ordinary_preserves_contract_unit_race_vet_and_fixture_commands(self):
        commands = self.run_phase("ordinary")
        result = self.result()
        self.assertEqual([row["name"] for row in result["checks"]],
                         ["platform-contracts", "connector-unit-race-vet", "sandboxer-unit-race-vet",
                          "orchestrator-unit-race-vet", "accelerator-fixtures"])
        self.assertEqual(commands[0][0], ["make", "test-ci-tools", "test-release-tools", "test-perf-tools"])
        for command, directory, env in commands:
            if command[0] == "bash":
                self.assertEqual(command, ["bash", "scripts/ci-source-checks.sh", "--ordinary"])
            self.assertEqual(env["TMPDIR"], "/build/tmp")
            self.assertEqual(env["ORG"], str(self.sources))
            self.assertEqual(env["TARGET_ARCH"], "x86_64")
        self.assertEqual(result["test_revisions"], self.plan["test_revisions"])
        self.assertEqual(result["framework_sha"], self.plan["framework_sha"])

    def test_privileged_keeps_every_declared_owner_and_uffd_performance(self):
        commands = self.run_phase("privileged")
        self.assertEqual([row["name"] for row in self.result()["checks"]],
                         ["connector-privileged", "sandboxer-privileged", "orchestrator-privileged",
                          "uffd-source-benchmark"])
        for command, _, _ in commands[:-1]:
            self.assertEqual(command, ["bash", "scripts/ci-source-checks.sh", "--privileged"])
        self.assertEqual(commands[-1][0], ["bash", "test/perf/uffd-performance-gate.sh"])

    def test_default_all_keeps_existing_commands_and_failure_assertion(self):
        with self.assertRaisesRegex(ValueError, "required source check failed: connector-unit-race-vet"):
            self.run_phase("all", failure=(["bash", "scripts/ci-source-checks.sh"], 33))
        self.assertEqual(self.result()["conclusion"], "failure")
        self.assertEqual(self.result()["exit_code"], 33)
        self.assertEqual(self.result()["checks"][-1]["exit_code"], 33)

    def test_owner_scope_does_not_expand_into_unrelated_checks(self):
        select_plan(self.plan, ["connector"])
        self.run_phase("privileged")
        self.assertEqual([row["name"] for row in self.result()["checks"]], ["connector-privileged"])
        self.assertEqual(set(self.result()["sources"]), {"connector", "platform"})

    def test_uffd_failure_remains_fatal_and_preserves_exit(self):
        with self.assertRaisesRegex(ValueError, "uffd-source-benchmark"):
            self.run_phase("privileged", failure=(["bash", "test/perf/uffd-performance-gate.sh"], 42))
        self.assertEqual(self.result()["exit_code"], 42)
        self.assertEqual(self.result()["checks"][-1]["exit_code"], 42)

    def test_missing_executor_records_command_failure(self):
        with patch.object(SUBJECT.build, "checkout", side_effect=self.checkout), \
             patch.object(SUBJECT.subprocess, "run", side_effect=FileNotFoundError("make")):
            with self.assertRaises(FileNotFoundError):
                SUBJECT.execute(self.plan, self.sources, self.output, phase="ordinary")
        self.assertEqual(self.result()["conclusion"], "failure")
        self.assertEqual(self.result()["exit_code"], 127)
        self.assertEqual(self.result()["checks"][-1]["exit_code"], 127)
        self.assertEqual(self.result()["error"], "make")

    def test_credentials_fail_before_checkout_or_source_execution(self):
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "CALLER_TOKEN", "KUASAR_CI_APP_PRIVATE_KEY"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "credential-fixture"}), \
                 patch.object(SUBJECT.build, "checkout") as checkout, \
                 patch.object(SUBJECT.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "must not receive " + name):
                    SUBJECT.materialize(self.plan, self.sources, self.output)
                checkout.assert_not_called()
                run.assert_not_called()

    def test_materialized_receipt_rejects_changed_plan_before_execution(self):
        with patch.object(SUBJECT.build, "checkout", side_effect=self.checkout):
            SUBJECT.materialize(self.plan, self.sources, self.output)
        self.plan["test_revisions"]["connector"]["sha"] = "f" * 40
        with patch.object(SUBJECT.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "differ from admitted plan"):
                SUBJECT.execute(self.plan, self.sources, self.output, materialized=True, phase="ordinary")
            run.assert_not_called()

    def real_sources(self):
        select_plan(self.plan, ["connector"])
        upstreams = {}
        for owner in ("connector", "platform"):
            source = self.root / ("upstream-" + owner)
            source.mkdir()
            (source / "README").write_text("source fixture\n")
            files = ["README"]
            if owner != "platform":
                (source / "go.mod").write_text("module example.test/" + owner + "\n\ngo 1.26.0\n")
                files.append("go.mod")
            for command in (["git", "init", "--quiet", source],
                            ["git", "-C", source, "config", "user.name", "Chen Xiaohui"],
                            ["git", "-C", source, "config", "user.email", "graych@gmail.com"],
                            ["git", "-C", source, "config", "commit.gpgsign", "false"],
                            ["git", "-C", source, "add", *files],
                            ["git", "-C", source, "commit", "--quiet", "-m", "source fixture"]):
                subprocess.run(command, check=True)
            self.plan["test_revisions"][owner]["sha"] = subprocess.check_output(
                ["git", "-C", source, "rev-parse", "HEAD"], text=True).strip()
            upstreams[owner] = source
        with patch.object(SUBJECT.build, "checkout", side_effect=lambda repository, sha, target:
                          shutil.copytree(upstreams[target.name], target)):
            SUBJECT.materialize(self.plan, self.sources, self.output)

    def executor(self, seen, *, bad_workspace=False):
        real_run = subprocess.run
        def run(command, **kwargs):
            seen.append((command, kwargs))
            if command[0] == "git":
                return real_run(command, **kwargs)
            if command[:3] == ["go", "work", "init"]:
                (self.sources / "go.work").write_text("go 1.26.0\nuse ./connector\n")
            if command[:3] == ["go", "work", "edit"]:
                use = [{"DiskPath": "./connector"}]
                if bad_workspace:
                    use.append({"DiskPath": "../other"})
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"Use": use}))
            return subprocess.CompletedProcess(command, 0)
        return run

    def test_fresh_materialized_sources_verify_git_and_reuse_exact_workspace(self):
        self.real_sources()
        seen = []
        run = self.executor(seen)
        with patch.object(SUBJECT.build, "checkout") as checkout, \
             patch.object(SUBJECT.subprocess, "run", side_effect=run):
            SUBJECT.execute(self.plan, self.sources, self.output, materialized=True, phase="ordinary")
            SUBJECT.execute(self.plan, self.sources, self.output, materialized=True, phase="privileged")
            checkout.assert_not_called()
        self.assertEqual(sum(command[:3] == ["go", "work", "init"] for command, _ in seen), 1)
        for command, kwargs in seen:
            if command[0] in ("bash", "make"):
                self.assertEqual(kwargs["env"]["GOWORK"], str(self.sources / "go.work"))
        self.assertEqual(self.result()["sources"]["connector"], self.plan["test_revisions"]["connector"])

    def test_untracked_source_injection_fails_before_any_compiler(self):
        self.real_sources()
        (self.sources / "connector/injected.go").write_text("package injected\n")
        seen = []
        with patch.object(SUBJECT.subprocess, "run", side_effect=self.executor(seen)):
            with self.assertRaisesRegex(ValueError, "untracked inputs"):
                SUBJECT.execute(self.plan, self.sources, self.output, materialized=True, phase="privileged")
        self.assertTrue(all(command[0] == "git" for command, _ in seen))

    def test_workspace_cannot_add_unselected_module_before_checks(self):
        self.real_sources()
        seen = []
        with patch.object(SUBJECT.subprocess, "run", side_effect=self.executor(seen, bad_workspace=True)):
            with self.assertRaisesRegex(ValueError, "dependency closure"):
                SUBJECT.execute(self.plan, self.sources, self.output, materialized=True, phase="ordinary")
        self.assertEqual(self.result()["checks"], [])


if __name__ == "__main__":
    unittest.main()
