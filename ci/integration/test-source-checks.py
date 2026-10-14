#!/usr/bin/env python3
"""Complete source gates, exact-test pins and failure-propagation regressions."""

import importlib.util
import hashlib
import itertools
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import artifacts
import source_results
from test_fixtures import restore_using
from test_fixtures import CASES, select_plan


SPEC = importlib.util.spec_from_file_location("source_checks", Path(__file__).with_name("run-source-checks.py"))
SUBJECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUBJECT)


class SourceChecksTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="source-gates-")
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
        with patch.object(SUBJECT.source_inputs, "copy_run_inputs", side_effect=restore_using(self.checkout)) as checkout, \
             patch.object(SUBJECT.subprocess, "run") as run:
            SUBJECT.materialize(self.plan, self.sources, self.output)
        run.assert_not_called()
        expected = {"platform", "orchestrator", "sandboxer", "connector", "accelerator"}
        checkout.assert_called_once()
        layout = checkout.call_args.args[1]
        self.assertEqual(set(layout), expected)
        for owner, record in layout.items():
            self.assertEqual(record, self.plan['test_revisions'][owner])
            self.assertNotEqual(record['sha'], 'c' * 40)
        self.assertFalse((self.sources / "go.work").exists())
        result = self.result()
        self.assertEqual(result["phase"], "materialize")
        self.assertEqual(result["conclusion"], "success")
        self.assertEqual(result["checks"], [])
        self.assertEqual(set(result["sources"]), expected)
        self.assertTrue(all(row["exit_code"] == 0 for row in result["actions"]))

    def test_materialize_failure_records_partial_actions_and_real_exit(self):
        select_plan(self.plan, ["connector"])
        with patch.object(SUBJECT.source_inputs, "copy_run_inputs", side_effect=subprocess.CalledProcessError(17, ["git", "fetch"])):
            with self.assertRaises(subprocess.CalledProcessError):
                SUBJECT.materialize(self.plan, self.sources, self.output)
        self.assertEqual(self.result()["exit_code"], 17)
        self.assertEqual(self.result()["actions"][-1]["exit_code"], 17)
        self.assertFalse((self.sources / SUBJECT.SOURCE_RECORD).exists())

    def run_checks(self, *, failure=None, group='all'):
        commands = []
        def run(command, *, cwd, env):
            commands.append((command, cwd, dict(env)))
            return subprocess.CompletedProcess(command, failure[1] if failure and command == failure[0] else 0)
        with patch.object(SUBJECT.source_inputs, "copy_run_inputs", side_effect=restore_using(self.checkout)), \
             patch.object(SUBJECT.subprocess, "run", side_effect=run), \
             patch.dict(os.environ, {"TMPDIR": "/build/t"}):
            SUBJECT.execute(self.plan, self.sources, self.output, group=group)
        return commands

    def test_complete_source_gate_keeps_owner_entries_and_uffd_performance(self):
        commands = self.run_checks()
        result = self.result()
        self.assertEqual([row["name"] for row in result["checks"]],
                         ["platform-contracts", "platform-privileged-perf-contracts", "connector-unit-race-vet", "sandboxer-unit-race-vet",
                          "orchestrator-unit-race-vet", "accelerator-fixtures", "uffd-source-benchmark"])
        self.assertEqual(commands[0][0], ["make", "test-ci-tools", "test-release-tools"])
        self.assertEqual(commands[1][0], ["sudo", "-n", "-E", "make", "test-perf-tools"])
        for command, _, _ in commands[2:5]:
            self.assertEqual(command, ["bash", "scripts/ci-source-checks.sh"])
        for _, directory, env in commands:
            self.assertEqual(env["TMPDIR"], "/build/t")
            self.assertEqual(env["ORG"], str(self.sources))
            self.assertEqual(env["TARGET_ARCH"], "x86_64")
            self.assertEqual(env["REQUIRE_WORKING_SET_PRIVILEGED"], "1")
        self.assertEqual(commands[-1][0], ["bash", "test/perf/uffd-performance-gate.sh"])
        self.assertEqual(result["test_revisions"], self.plan["test_revisions"])
        self.assertEqual(result["framework_sha"], self.plan["framework_sha"])

    def test_groups_preserve_all_commands_conditions_and_dependencies(self):
        owners = list(artifacts.OWNERS)
        for mode in ('source', 'exact-assets'):
            for count in range(1, len(owners) + 1):
                for selected in itertools.combinations(owners, count):
                    with self.subTest(mode=mode, selected=selected):
                        self.plan['mode'] = mode
                        select_plan(self.plan, list(selected))
                        all_checks = SUBJECT.checks_for(self.plan, self.sources)
                        a = SUBJECT.checks_for(self.plan, self.sources, 'A')
                        b = SUBJECT.checks_for(self.plan, self.sources, 'B')
                        self.assertEqual(a, [row for row in all_checks if row[0] != 'orchestrator-unit-race-vet'])
                        self.assertEqual(b, [row for row in all_checks if row[0] == 'orchestrator-unit-race-vet'])
                        self.assertEqual(len(a) + len(b), len(all_checks))
                        self.assertEqual(a[0][1], ['make', 'test-ci-tools', 'test-release-tools'])
                        self.assertEqual(a[1][1], ['sudo', '-n', '-E', 'make', 'test-perf-tools'])
                        self.assertEqual(source_results.groups_for(self.plan, True), ['A', 'B'] if b else ['A'])
                        self.assertEqual(source_results.groups_for(self.plan), ['all'])
        with self.assertRaises(ValueError):
            SUBJECT.checks_for(self.plan, self.sources, 'unknown')

    def grouped_evidence(self, groups=('A', 'B')):
        run = dict(id=123, run_attempt=2, head_sha='a' * 40)
        jobs, available, documents = [], [], {}
        for i, group in enumerate(groups, 1):
            jobs.append(dict(id=100 + i, run_id=123, run_attempt=1, status='completed', conclusion='success',
                             name=f'ci / source-checks ({group})', runner_name=f'host-{i}',
                             started_at='2026-10-14T00:00:00Z', completed_at='2026-10-14T01:00:00Z'))
            available.append(dict(id=i, name=source_results.artifact_name(123, 1, group), expired=False,
                                  workflow_run=dict(id=123, head_sha=run['head_sha']), created_at='2026-10-14T00:59:00Z'))
            result = SUBJECT.new_result(self.plan, 'all')
            result.update(group=group, arch='x86_64', conclusion='success', exit_code=0,
                          sources=SUBJECT.source_records(self.plan)[1],
                          workbench=dict(image_id='sha256:' + 'c' * 64, framework_sha=self.plan['framework_sha']),
                          checks=[dict(name=n, command=c, directory=str(d), exit_code=0)
                                  for n, c, d in SUBJECT.checks_for(self.plan, Path('/src'), group)])
            receipt = dict(conclusion='success', cleanup_exit_code=0, delete_output_exit_code=0,
                           mode='system', framework_sha=self.plan['framework_sha'], arch='x86_64', cpus=2,
                           memory_gib=8, commands=[dict(exit_code=0)], image=dict(image_id='sha256:' + 'c' * 64))
            documents[i] = dict(result=result, receipt=receipt, instance=dict(id=f'instance-{i}', container_id=f'container-{i}'))
        return [run, jobs, available, documents]

    def validate_evidence(self, data, groups=('A', 'B')):
        run, jobs, available, documents = data
        return source_results.validate(self.plan, list(groups), run, jobs, available, documents.__getitem__)

    def test_current_source_results_and_legal_earlier_attempt_reuse(self):
        for groups in (('all',), ('A', 'B')):
            result = self.validate_evidence(self.grouped_evidence(groups), groups)
            self.assertEqual(result['conclusion'], 'success')
            self.assertEqual(result['run_attempt'], 2)
            self.assertTrue(all(row['run_attempt'] == 1 for row in result['groups']))

    def test_source_results_reject_missing_duplicate_stale_and_failed_evidence(self):
        def change(path, value):
            def mutate(data):
                obj = data
                for part in path[:-1]:
                    obj = obj[part]
                obj[path[-1]] = value
            return mutate
        mutations = [
            lambda d: d[1].pop(), lambda d: d[1].append(copy.deepcopy(d[1][0])),
            lambda d: d[2].pop(), lambda d: d[2].append(copy.deepcopy(d[2][0])),
            change([0, 'head_sha'], ''), change([0, 'run_attempt'], 0),
            change([1, 0, 'run_id'], 999), change([1, 0, 'run_attempt'], 3),
            change([1, 0, 'status'], 'in_progress'), change([1, 0, 'conclusion'], 'cancelled'),
            change([1, 0, 'conclusion'], 'failure'), change([1, 0, 'conclusion'], ''),
            change([1, 1, 'runner_name'], 'host-1'), change([2, 0, 'expired'], True),
            change([2, 0, 'workflow_run', 'head_sha'], 'b' * 40),
            change([2, 0, 'workflow_run', 'id'], 456),
            change([2, 0, 'created_at'], '2026-10-13T00:00:00Z'),
            change([3, 1, 'result', 'plan_id'], 'b' * 64),
            change([3, 1, 'result', 'test_revisions'], {}),
            change([3, 1, 'result', 'sources'], {}),
            change([3, 1, 'result', 'group'], 'B'), change([3, 1, 'result', 'checks'], []),
            change([3, 1, 'result', 'checks', 0, 'exit_code'], 2),
            change([3, 1, 'result', 'checks', 0, 'command'], ['make', 'test-ci-tools']),
            change([3, 1, 'result', 'exit_code'], 1),
            change([3, 1, 'result', 'workbench', 'image_id'], 'sha256:' + 'd' * 64),
            change([3, 1, 'receipt', 'cleanup_exit_code'], 1),
            change([3, 1, 'receipt', 'delete_output_exit_code'], None),
            change([3, 1, 'receipt', 'commands'], []),
            change([3, 1, 'receipt', 'cpus'], 4), change([3, 1, 'receipt', 'memory_gib'], 4),
            change([3, 2, 'instance', 'id'], 'instance-1'),
            change([3, 2, 'instance', 'container_id'], 'container-1'),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                data = self.grouped_evidence()
                mutate(data)
                with self.assertRaises(ValueError):
                    self.validate_evidence(data)

    def test_new_producer_attempt_invalidates_old_artifact(self):
        for conclusion in ('success', 'failure', 'cancelled', None):
            with self.subTest(conclusion=conclusion):
                data = self.grouped_evidence()
                latest = dict(data[1][0], id=301, run_attempt=2, conclusion=conclusion)
                data[1].append(latest)
                with self.assertRaises(ValueError):
                    self.validate_evidence(data)
        data = self.grouped_evidence()
        data[1].append(dict(data[1][0], id=301, run_attempt=2))
        data[2].append(dict(data[2][0], id=3, name=source_results.artifact_name(123, 2, 'A')))
        data[3][3] = copy.deepcopy(data[3][1])
        self.assertEqual([row['run_attempt'] for row in self.validate_evidence(data)['groups']], [2, 1])

    def test_duplicate_older_attempt_rejected_in_unsorted_history(self):
        data = self.grouped_evidence()
        older = copy.deepcopy(data[1][0])
        data[1] = [dict(older, id=301, run_attempt=2), older, dict(older, id=302), data[1][1]]
        data[2].append(dict(data[2][0], id=3, name=source_results.artifact_name(123, 2, 'A')))
        data[3][3] = copy.deepcopy(data[3][1])
        with self.assertRaisesRegex(ValueError, 'duplicate source producer'):
            self.validate_evidence(data)

    def test_each_group_executes_original_commands_and_propagates_failure(self):
        for group in ('A', 'B'):
            with self.subTest(group=group):
                self.sources = self.root / group
                commands = self.run_checks(group=group)
                self.assertEqual([(command, directory) for command, directory, _ in commands],
                                 [(c, d) for _, c, d in SUBJECT.checks_for(self.plan, self.sources, group)])
                self.assertEqual(self.result()['group'], group)
                self.sources = self.root / (group + '-failure')
                first = SUBJECT.checks_for(self.plan, self.sources, group)[0]
                with self.assertRaisesRegex(ValueError, first[0]):
                    self.run_checks(group=group, failure=(first[1], 29))
                self.assertEqual(self.result()['exit_code'], 29)

    def test_downloaded_artifacts_keep_attempt_and_group_directories(self):
        available = [dict(id=1, name=source_results.artifact_name(123, 2, 'A')),
                     dict(id=2, name=source_results.artifact_name(123, 1, 'A'))]
        base = self.root / available[0]['name'] / 'nested/source-system-evidence'
        (base / 'output').mkdir(parents=True)
        for name in ('output/source-result.json', 'receipt.json', 'instance.json'):
            (base / name).write_text('{}')
        self.assertEqual(source_results.read_artifact(self.root, available, 1),
                         dict(result={}, receipt={}, instance={}))
        (base / 'output/source-result.json').unlink()
        with self.assertRaises(ValueError):
            source_results.read_artifact(self.root, available, 1)

    def test_single_pattern_match_is_flattened_for_all_or_a(self):
        for group in ('all', 'A'):
            with self.subTest(group=group), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                available = [dict(id=1, name=source_results.artifact_name(123, 2, group)),
                             dict(id=9, name='unrelated-artifact')]
                base = root / '_temp/source-system-evidence'
                (base / 'output').mkdir(parents=True)
                for name in ('output/source-result.json', 'receipt.json', 'instance.json'):
                    (base / name).write_text('{}')
                self.assertEqual(source_results.read_artifact(root, available, 1),
                                 dict(result={}, receipt={}, instance={}))
                for other in (source_results.artifact_name(123, 1, group),
                              source_results.artifact_name(123, 2, 'B')):
                    with self.assertRaisesRegex(ValueError, 'missing downloaded'):
                        source_results.read_artifact(root, available + [dict(id=2, name=other)], 1)
                (base / 'receipt.json').unlink()
                (base / 'receipt.json').symlink_to(base / 'instance.json')
                with self.assertRaisesRegex(ValueError, 'invalid source evidence'):
                    source_results.read_artifact(root, available, 1)

    def test_owner_failure_retains_real_exit_and_stops_later_checks(self):
        with self.assertRaisesRegex(ValueError, "required source check failed: connector-unit-race-vet"):
            self.run_checks(failure=(["bash", "scripts/ci-source-checks.sh"], 33))
        self.assertEqual(self.result()["conclusion"], "failure")
        self.assertEqual(self.result()["exit_code"], 33)
        self.assertEqual(self.result()["checks"][-1]["exit_code"], 33)

    def test_owner_scope_does_not_expand_into_unrelated_checks(self):
        select_plan(self.plan, ["connector"])
        self.run_checks()
        self.assertEqual([row["name"] for row in self.result()["checks"]],
                         ["platform-contracts", "platform-privileged-perf-contracts", "connector-unit-race-vet"])
        self.assertEqual(set(self.result()["sources"]), {"connector", "platform"})

    def test_real_privileged_perf_failure_is_required(self):
        with self.assertRaisesRegex(ValueError, "required source check failed: platform-privileged-perf-contracts"):
            self.run_checks(failure=(["sudo", "-n", "-E", "make", "test-perf-tools"], 37))
        self.assertEqual(self.result()["exit_code"], 37)
        self.assertEqual([row["name"] for row in self.result()["checks"]],
                         ["platform-contracts", "platform-privileged-perf-contracts"])

    def test_published_and_candidate_entries_execute_unchanged_without_new_arguments(self):
        select_plan(self.plan, ["connector"])
        real_run = subprocess.run
        for label, checks in (("published", "unit\npinned-bpf\nvet"),
                              ("candidate", "helper\nunit\npinned-bpf\nnew-regression\nvet")):
            with self.subTest(source=label):
                self.sources = self.root / label
                script = '#!/bin/bash\nset -euo pipefail\n[ "$#" = 0 ]\nprintf "%s\\n" "' + checks + '" > executed\n'
                def checkout(repository, sha, destination):
                    destination.mkdir()
                    if destination.name == "connector":
                        (destination / "scripts").mkdir()
                        (destination / "scripts/ci-source-checks.sh").write_text(script)
                def run(command, *, cwd, env):
                    if command == ["bash", "scripts/ci-source-checks.sh"]:
                        return real_run(command, cwd=cwd, env=env, timeout=10)
                    return subprocess.CompletedProcess(command, 0)
                with patch.object(SUBJECT.source_inputs, "copy_run_inputs", side_effect=restore_using(checkout)), \
                     patch.object(SUBJECT.subprocess, "run", side_effect=run):
                    SUBJECT.execute(self.plan, self.sources, self.output)
                self.assertEqual((self.sources / "connector/executed").read_text(), checks + "\n")
                self.assertEqual((self.sources / "connector/scripts/ci-source-checks.sh").read_text(), script)
                self.assertEqual(self.result()["conclusion"], "success")

    def test_required_tap_and_netns_capabilities_cannot_be_skipped(self):
        tools = self.root / "tools"
        tools.mkdir()
        identity = tools / "id"
        unavailable = tools / "ip"
        unavailable.write_text("#!/bin/sh\nexit 1\n")
        unavailable.chmod(0o755)
        scripts = Path(__file__).resolve().parents[2] / "test/perf"
        suffix = hashlib.sha256(self.root.name.encode()).hexdigest()[:8]
        for prefix in ("wstt", "wsnt"):
            self.addCleanup((Path("/tmp") / f"{prefix}-{suffix}.lock").unlink, missing_ok=True)
        for uid in (1000, 0):
            identity.write_text("#!/bin/sh\necho " + str(uid) + "\n")
            identity.chmod(0o755)
            for name in ("working_set_tap_test.sh", "working_set_netns_test.sh"):
                with self.subTest(uid=uid, script=name):
                    result = subprocess.run(["bash", scripts / name],
                                            env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
                                                 "RUNNER_NAME": self.root.name,
                                                 "REQUIRE_WORKING_SET_PRIVILEGED": "1"},
                                            capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("required", result.stderr)
                    self.assertNotIn("skipped", result.stderr)

    def test_uffd_failure_remains_fatal_and_preserves_exit(self):
        with self.assertRaisesRegex(ValueError, "uffd-source-benchmark"):
            self.run_checks(failure=(["bash", "test/perf/uffd-performance-gate.sh"], 42))
        self.assertEqual(self.result()["exit_code"], 42)
        self.assertEqual(self.result()["checks"][-1]["exit_code"], 42)

    def test_missing_executor_records_command_failure(self):
        with patch.object(SUBJECT.source_inputs, "copy_run_inputs", side_effect=restore_using(self.checkout)), \
             patch.object(SUBJECT.subprocess, "run", side_effect=FileNotFoundError("make")):
            with self.assertRaises(FileNotFoundError):
                SUBJECT.execute(self.plan, self.sources, self.output)
        self.assertEqual(self.result()["conclusion"], "failure")
        self.assertEqual(self.result()["exit_code"], 127)
        self.assertEqual(self.result()["checks"][-1]["exit_code"], 127)
        self.assertEqual(self.result()["error"], "make")

    def test_credentials_fail_before_checkout_or_source_execution(self):
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "CALLER_TOKEN", "KUASAR_CI_APP_PRIVATE_KEY"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "credential-fixture"}), \
                 patch.object(SUBJECT.source_inputs, "copy_run_inputs") as checkout, \
                 patch.object(SUBJECT.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "must not receive " + name):
                    SUBJECT.materialize(self.plan, self.sources, self.output)
                checkout.assert_not_called()
                run.assert_not_called()

    def test_materialized_receipt_rejects_changed_plan_before_execution(self):
        with patch.object(SUBJECT.source_inputs, "copy_run_inputs", side_effect=restore_using(self.checkout)):
            SUBJECT.materialize(self.plan, self.sources, self.output)
        self.plan["test_revisions"]["connector"]["sha"] = "f" * 40
        with patch.object(SUBJECT.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "differ from admitted plan"):
                SUBJECT.execute(self.plan, self.sources, self.output, materialized=True)
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
        with patch.object(SUBJECT.source_inputs, "copy_run_inputs", side_effect=restore_using(lambda repository, sha, target:
                          shutil.copytree(upstreams[target.name], target))):
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
        with patch.object(SUBJECT.source_inputs, "copy_run_inputs") as checkout, \
             patch.object(SUBJECT.subprocess, "run", side_effect=run):
            SUBJECT.execute(self.plan, self.sources, self.output, materialized=True)
            SUBJECT.execute(self.plan, self.sources, self.output, materialized=True)
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
                SUBJECT.execute(self.plan, self.sources, self.output, materialized=True)
        self.assertTrue(all(command[0] == "git" for command, _ in seen))

    def test_workspace_cannot_add_unselected_module_before_checks(self):
        self.real_sources()
        seen = []
        with patch.object(SUBJECT.subprocess, "run", side_effect=self.executor(seen, bad_workspace=True)):
            with self.assertRaisesRegex(ValueError, "dependency closure"):
                SUBJECT.execute(self.plan, self.sources, self.output, materialized=True)
        self.assertEqual(self.result()["checks"], [])


if __name__ == "__main__":
    unittest.main()
