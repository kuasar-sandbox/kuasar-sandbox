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
import sys
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

    def source_scope(self, changes):
        records = []
        for owner in sorted(changes):
            revision = self.plan['test_revisions'][owner]
            records.append(dict(repository=revision['repository'], base_ref='main',
                                pull_request_number=1, base_sha='b' * 40, head_sha='c' * 40,
                                candidate_sha=revision['sha']))
        records.sort(key=lambda row: row['repository'])
        for index, record in enumerate(records):
            owner = SUBJECT.source_lane.owner(record['repository'])
            self.plan['test_revisions'][owner]['role'] = 'candidate' if index == 0 else 'companion'
        self.plan['candidate_records'] = records
        self.plan['source_scope'] = dict(schema=1, sources=copy.deepcopy(records),
                                        changes={owner: sorted(set(paths)) for owner, paths in changes.items()})

    def test_source_scope_uses_pr_owners_without_release_delta_expansion(self):
        self.source_scope({'platform': ['ci/integration/test-new.py']})
        selected, records = SUBJECT.source_records(self.plan)
        self.assertEqual(selected, {'platform'})
        self.assertEqual(records, self.plan['test_revisions'])
        with patch.object(SUBJECT.source_inputs, 'copy_run_inputs', side_effect=restore_using(self.checkout)), \
             patch.object(SUBJECT.subprocess, 'run') as run:
            SUBJECT.materialize(self.plan, self.sources, self.output)
        run.assert_not_called()
        self.assertEqual(set(self.result()['sources']), set(artifacts.OWNERS))
        checks = SUBJECT.checks_for(self.plan, self.sources)
        self.assertEqual([row[0] for row in checks],
                         ['platform-contracts', 'platform-test-script:ci/integration/test-new.py'])
        self.assertTrue(all(row[2] == self.sources / 'platform' for row in checks))
        self.assertEqual(SUBJECT.checks_for(self.plan, self.sources, 'B'), [])
        self.assertEqual(source_results.groups_for(self.plan, True), ['A'])

    def test_source_scope_covers_each_changed_go_owner_and_no_unrelated_gates(self):
        original = copy.deepcopy(self.plan)
        expected = {
            'connector': [('connector-go-tests', ['env', 'CGO_ENABLED=0', 'go', 'test', '-count=1', './...']),
                          ('connector-unit-race-vet', ['bash', 'scripts/ci-source-checks.sh'])],
            'sandboxer': [('sandboxer-unit-race-vet', ['bash', 'scripts/ci-source-checks.sh'])],
            'orchestrator': [('orchestrator-unit-race-vet', ['bash', 'scripts/ci-source-checks.sh'])],
            'accelerator': [('accelerator-source-tests', ['make', 'test', 'vet']),
                            ('accelerator-no-rocksdb-tests',
                             ['env', 'CGO_ENABLED=0', 'go', 'test', '-count=1', '-tags', 'no_rocksdb', './...'])],
            'guest-runtime': [('guest-runtime-source-tests', ['make', 'test', 'vet'])],
        }
        for owner, commands in expected.items():
            with self.subTest(owner=owner):
                self.plan = copy.deepcopy(original)
                self.source_scope({owner: ['pkg/example/example_test.go']})
                artifacts.check_plan(self.plan)
                checks = SUBJECT.checks_for(self.plan, self.sources)
                self.assertEqual(checks[0], ('platform-contracts',
                                            ['make', 'test-ci-tools', 'test-release-tools'], self.sources / 'platform'))
                self.assertEqual(checks[1:], [(name, command, self.sources / owner) for name, command in commands])

    def test_source_scope_requires_privileged_perf_contracts_only_for_perf_changes(self):
        self.source_scope({'platform': ['test/perf/working_set_tap_test.sh']})
        checks = SUBJECT.checks_for(self.plan, self.sources)
        self.assertEqual([row[0] for row in checks],
                         ['platform-contracts', 'platform-privileged-perf-contracts',
                          'platform-test-script:test/perf/working_set_tap_test.sh'])
        self.assertEqual(checks[1][1], ['sudo', '-n', '-E', 'make', 'test-perf-tools'])
        self.assertNotIn('uffd-source-benchmark', [row[0] for row in checks])

    def test_source_scope_new_tests_are_mandatory_in_their_owner_group(self):
        self.source_scope({'connector': ['scripts/test-new.py'],
                           'orchestrator': ['test/source/test-new.sh']})
        all_checks = SUBJECT.checks_for(self.plan, self.sources)
        a = SUBJECT.checks_for(self.plan, self.sources, 'A')
        b = SUBJECT.checks_for(self.plan, self.sources, 'B')
        self.assertEqual(a, [row for row in all_checks if row[2].name != 'orchestrator'])
        self.assertEqual(b, [row for row in all_checks if row[2].name == 'orchestrator'])
        self.assertEqual([row[0] for row in b],
                         ['orchestrator-unit-race-vet', 'orchestrator-test-script:test/source/test-new.sh'])
        self.assertEqual(b[-1][1], ['python3', '-B', SUBJECT.TEST_SCRIPT_EXECUTOR, '--test-script',
                                  'kuasar-sandbox/orchestrator', 'test/source/test-new.sh'])
        self.assertEqual(self.validate_evidence(self.grouped_evidence())['conclusion'], 'success')
        for mutation in ('missing', 'substituted', 'failed'):
            with self.subTest(mutation=mutation):
                data = self.grouped_evidence()
                checks = data[3][2]['result']['checks']
                if mutation == 'missing':
                    checks.pop()
                elif mutation == 'substituted':
                    checks[-1]['command'] = ['true']
                else:
                    checks[-1]['exit_code'] = 19
                with self.assertRaises(ValueError):
                    self.validate_evidence(data)

    def test_source_scope_changed_script_failure_preserves_its_real_exit(self):
        path = 'ci/integration/test-new.py'
        self.source_scope({'platform': [path]})
        command = SUBJECT.checks_for(self.plan, self.sources)[-1][1]
        with self.assertRaisesRegex(ValueError, 'platform-test-script:' + path):
            self.run_checks(failure=(command, 29))
        self.assertEqual(self.result()['exit_code'], 29)
        self.assertEqual(self.result()['checks'][-1]['exit_code'], 29)

    def test_source_scope_metadata_and_doc_companions_only_require_documentation_package(self):
        self.source_scope({'guest-runtime': ['release/guide-inputs.txt'],
                           'orchestrator': ['README.md'], 'platform': ['docs/contributing.md']})
        selected, records = SUBJECT.source_records(self.plan)
        self.assertEqual(selected, set())
        self.assertEqual(records, self.plan['test_revisions'])
        self.run_checks(group='A')
        checks = self.result()['checks']
        self.assertEqual([row['name'] for row in checks], ['platform-contracts', 'source-documentation-package'])
        self.assertEqual(checks[1]['command'],
                         ['python3', '-B', '/inputs/release/ci/integration/check-source-documentation.py',
                          '--plan', '/src/.source-plan.json', '--sources', '/src',
                          '--output', '/output/source-documentation.json'])
        self.assertEqual(source_results.groups_for(self.plan, True), ['A'])
        self.assertEqual(self.validate_evidence(self.grouped_evidence(('A',)), ('A',))['conclusion'], 'success')
        evidence = self.grouped_evidence(('A',))
        evidence[3][1]['result']['checks'].pop()
        with self.assertRaises(ValueError):
            self.validate_evidence(evidence, ('A',))

    def test_source_scope_mixed_fixtures_tests_and_metadata_select_only_test_owners(self):
        self.source_scope({'accelerator': ['pkg/tarstream/testdata/legacy-byte-holes.tar'],
                           'connector': ['pkg/vswitch/new_test.go'],
                           'guest-runtime': ['release/guide-migrations.json'],
                           'orchestrator': ['docs/README.md']})
        selected, _ = SUBJECT.source_records(self.plan)
        self.assertEqual(selected, {'accelerator', 'connector'})
        self.assertEqual([row[0] for row in SUBJECT.checks_for(self.plan, self.sources)],
                         ['platform-contracts', 'source-documentation-package', 'connector-go-tests',
                          'connector-unit-race-vet', 'accelerator-source-tests', 'accelerator-no-rocksdb-tests'])
        self.assertEqual(source_results.groups_for(self.plan, True), ['A'])

    def test_source_scope_documentation_package_failure_is_required(self):
        self.source_scope({'platform': ['test/e2e/assemble_docs.py']})
        command = SUBJECT.checks_for(self.plan, self.sources)[1][1]
        with self.assertRaisesRegex(ValueError, 'source-documentation-package'):
            self.run_checks(failure=(command, 23))
        self.assertEqual(self.result()['exit_code'], 23)

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
             patch.object(SUBJECT, "workspace", side_effect=lambda root, owner, env: dict(env, GOWORK="off")), \
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

    def test_unexecuted_exact_asset_source_checks_cannot_report_success(self):
        self.plan['mode'] = 'exact-assets'
        with self.assertRaisesRegex(ValueError, 'report skipped'):
            source_results.validate(self.plan, [], {}, [], [], lambda _: None)

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
                        self.assertEqual(source_results.groups_for(self.plan, True), [] if mode == 'exact-assets' else ['A', 'B'] if b else ['A'])
                        self.assertEqual(source_results.groups_for(self.plan), [] if mode == 'exact-assets' else ['all'])
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
                     patch.object(SUBJECT.subprocess, "run", side_effect=run), \
                     patch.object(SUBJECT, "workspace", side_effect=lambda root, owner, env: dict(env, GOWORK="off")):
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
                (source / "go.mod").write_text("module github.com/kuasar-sandbox/" + owner + "\n\ngo 1.26.0\n")
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

    def executor(self, seen):
        real_run = subprocess.run
        def run(command, **kwargs):
            seen.append((command, kwargs))
            if command[0] == "git" or Path(command[0]).name == "go":
                return real_run(command, **kwargs)
            return subprocess.CompletedProcess(command, 0)
        return run

    @unittest.skipUnless(shutil.which("go"), "Go compiler required")
    def test_fresh_materialized_sources_verify_git_and_reuse_exact_workspace(self):
        self.real_sources()
        seen = []
        run = self.executor(seen)
        with patch.object(SUBJECT.source_inputs, "copy_run_inputs") as checkout, \
             patch.object(SUBJECT.subprocess, "run", side_effect=run):
            SUBJECT.execute(self.plan, self.sources, self.output, materialized=True)
            SUBJECT.execute(self.plan, self.sources, self.output, materialized=True)
            checkout.assert_not_called()
        self.assertEqual(sum(Path(command[0]).name == "go" and command[1:3] == ["work", "init"] for command, _ in seen), 1)
        for command, kwargs in seen:
            if command[0] == "bash":
                self.assertEqual(kwargs["env"]["GOWORK"], str(self.sources / ".ci/go-workspaces/connector/go.work"))
            elif command[0] == "make":
                self.assertEqual(kwargs["env"]["GOWORK"], "off")
        self.assertEqual(self.result()["sources"]["connector"], self.plan["test_revisions"]["connector"])

    def test_untracked_source_injection_fails_before_any_compiler(self):
        self.real_sources()
        (self.sources / "connector/injected.go").write_text("package injected\n")
        seen = []
        with patch.object(SUBJECT.subprocess, "run", side_effect=self.executor(seen)):
            with self.assertRaisesRegex(ValueError, "untracked inputs"):
                SUBJECT.execute(self.plan, self.sources, self.output, materialized=True)
        self.assertTrue(all(command[0] == "git" for command, _ in seen))

    @unittest.skipUnless(shutil.which("go"), "Go compiler required")
    def test_workspace_cannot_add_unselected_module_before_owner_checks(self):
        self.real_sources()
        env = SUBJECT.workspace(self.sources, 'connector', dict(os.environ))
        path = Path(env['GOWORK'])
        path.write_text(path.read_text() + '\nuse ' + str(self.sources/'platform') + '\n')
        seen = []
        with patch.object(SUBJECT.subprocess, "run", side_effect=self.executor(seen)):
            with self.assertRaisesRegex(ValueError, "selected module closure"):
                SUBJECT.execute(self.plan, self.sources, self.output, materialized=True)
        self.assertNotIn('connector-unit-race-vet', [row['name'] for row in self.result()['checks']])


class ChangedTestScriptTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='changed-source-test-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for args in (['init', '--quiet'], ['config', 'user.name', 'Source Test Fixture'],
                     ['config', 'user.email', 'source-test@example.invalid'],
                     ['config', 'commit.gpgsign', 'false']):
            self.git(*args)

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout

    def commit(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        self.git('add', path)
        self.git('commit', '--quiet', '-m', 'source test fixture')
        return target

    def run_test(self, path, repository='kuasar-sandbox/connector'):
        real_run = subprocess.run
        results = []
        def run(command, **kwargs):
            if command[0] in ('python3', 'bash'):
                result = real_run(command, **kwargs, capture_output=True, text=True, timeout=10)
                results.append(result)
                return result
            return real_run(command, **kwargs)
        with patch.object(SUBJECT.subprocess, 'run', side_effect=run):
            status = SUBJECT.run_test_script(repository, path, self.root)
        return status, results

    def test_unregistered_python_and_shell_entries_really_execute_from_repository_root(self):
        scripts = {'scripts/test-new.py': 'from pathlib import Path\nPath("python-ran").write_text("yes")\n'
                                        'assert Path("python-ran").read_text() == "yes"\n',
                   'scripts/test-new.sh': 'set -eu\n[ "$#" = 0 ]\nprintf yes > shell-ran\n'}
        for path, text in scripts.items():
            with self.subTest(path=path):
                self.commit(path, text)
                status, results = self.run_test(path)
                self.assertEqual(status, 0, results[0].stderr)
        self.assertEqual((self.root / 'python-ran').read_text(), 'yes')
        self.assertEqual((self.root / 'shell-ran').read_text(), 'yes')

    def test_unittest_without_main_and_dashed_filename_runs_candidate_imports(self):
        self.commit('scripts/artifacts.py', 'value = "candidate-module"\n')
        self.commit('scripts/test-new.py',
                    'from pathlib import Path\nfrom unittest import TestCase\nimport artifacts\n'
                    'class CandidateTest(TestCase):\n'
                    '    def test_candidate(self):\n'
                    '        self.assertEqual(artifacts.value, "candidate-module")\n'
                    '        Path("unittest-ran").write_text("yes")\n')
        status, results = self.run_test('scripts/test-new.py')
        self.assertEqual(status, 0, results[0].stderr)
        self.assertIn('Ran 1 test', results[0].stderr)
        self.assertEqual((self.root / 'unittest-ran').read_text(), 'yes')

    def test_failed_unittest_and_empty_suite_cannot_report_success(self):
        for path, text, expected in [
            ('scripts/test-failure.py', 'import unittest\nclass Failure(unittest.TestCase):\n'
             '    def test_failure(self):\n        self.fail("candidate-test-failed")\n', 'candidate-test-failed'),
            ('scripts/test-empty.py', 'import unittest\n', 'discovered zero tests'),
            ('scripts/test-exit.py', 'import unittest\nraise SystemExit(0)\n', 'exited before discovery'),
        ]:
            with self.subTest(path=path):
                self.commit(path, text)
                status, results = self.run_test(path)
                self.assertNotEqual(status, 0)
                self.assertIn(expected, results[0].stderr)

    def test_standalone_mock_imports_do_not_select_an_empty_unittest_suite(self):
        for index, statement in enumerate(('from unittest.mock import patch', 'import unittest.mock as mock',
                                            'from unittest import mock')):
            path = f'scripts/test-mock-{index}.py'
            self.commit(path, statement + '\nassert True\n')
            status, results = self.run_test(path)
            self.assertEqual(status, 0, results[0].stderr)
            self.assertIn('source standalone assertions executed: 1', results[0].stdout)

    def test_standalone_definitions_logging_false_guards_and_early_exit_are_not_tests(self):
        cases = [
            'def test_bad():\n    assert False\n',
            'def test_bad():\n    assert False\nprint("loaded")\n',
            'if __name__ != "__main__":\n    assert False\n',
            'if False and __name__ == "__main__":\n    assert False\n',
            'if __name__ == "__main__":\n    pass\n\ndef test_bad():\n    assert False\n',
            'raise SystemExit(0)\nassert False\n',
            'def test_bad(): assert False\n',
            'if False: assert False\n',
        ]
        for index, text in enumerate(cases):
            with self.subTest(index=index):
                path = f'scripts/test-no-execution-{index}.py'
                self.commit(path, text)
                status, results = self.run_test(path)
                self.assertNotEqual(status, 0)
                self.assertIn('executed zero assertions', results[0].stderr)
        self.commit('scripts/test-import-only.py', 'from pathlib import Path\nprint("loaded")\n')
        with self.assertRaisesRegex(ValueError, 'no supported executable assertion'):
            self.run_test('scripts/test-import-only.py')

    def test_standalone_main_executes_assertions_and_preserves_real_failures(self):
        for index, value in enumerate(('True', 'False')):
            path = f'scripts/test-main-{index}.py'
            self.commit(path, 'def check():\n    assert ' + value + '\n'
                              'if __name__ == "__main__":\n    check()\n')
            status, results = self.run_test(path)
            self.assertEqual(status, index, results[0].stderr)

    def test_argument_helpers_execute_their_exact_fixture_driver(self):
        for index, ((repository, path), driver) in enumerate(sorted(SUBJECT.source_lane.TEST_DRIVERS.items())):
            with self.subTest(repository=repository, path=path):
                suffix = str(index)
                text = 'raise SystemExit("helper requires fixture arguments")\n' if path.endswith('.py') else 'exit 77\n'
                self.commit(path, '# helper in ' + repository + '\n' + text)
                self.commit(driver, 'set -eu\nprintf yes > driver-ran-' + suffix + '\n')
                status, results = self.run_test(path, repository)
                self.assertEqual(status, 0, results[0].stderr)
                self.assertEqual((self.root / ('driver-ran-' + suffix)).read_text(), 'yes')

    def test_argument_helper_and_driver_blobs_are_both_required(self):
        repository = 'kuasar-sandbox/connector'
        path, driver = 'scripts/test-publisher.sh', 'scripts/test-release.sh'
        target = self.commit(path, 'exit 77\n')
        with self.assertRaisesRegex(ValueError, 'missing source test driver'):
            self.run_test(path, repository)
        caller = self.commit(driver, 'exit 0\n')
        caller.write_text('exit 31\n')
        with self.assertRaisesRegex(ValueError, 'differs from its selected Git blob'):
            self.run_test(path, repository)
        caller.write_text('exit 0\n')
        target.write_text('exit 0\n')
        with self.assertRaisesRegex(ValueError, 'differs from its selected Git blob'):
            self.run_test(path, repository)

    def test_deleted_argument_helper_still_exercises_its_caller(self):
        repository = 'kuasar-sandbox/connector'
        path, driver = 'scripts/test-publisher.sh', 'scripts/test-release.sh'
        self.commit(path, 'exit 77\n')
        self.commit(driver, 'exit 31\n')
        self.git('rm', path)
        self.git('commit', '--quiet', '-m', 'delete fixture helper')
        status, _ = self.run_test(path, repository)
        self.assertEqual(status, 31)

    def test_shell_failure_preserves_exit_status(self):
        self.commit('scripts/test-failure.sh', 'exit 37\n')
        status, _ = self.run_test('scripts/test-failure.sh')
        self.assertEqual(status, 37)

    def test_changed_test_command_line_entry_preserves_failure(self):
        path = 'scripts/test-cli.sh'
        self.commit(path, 'exit 31\n')
        result = subprocess.run([sys.executable, '-B', SUBJECT.__file__, '--test-script',
                                 'kuasar-sandbox/connector', path], cwd=self.root,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 31, result.stderr)

    def test_deleted_test_is_distinct_from_missing_modified_or_injected_test(self):
        path = 'scripts/test-state.py'
        original = 'assert True\n'
        target = self.commit(path, original)
        target.unlink()
        with self.assertRaisesRegex(ValueError, 'missing regular source test'):
            self.run_test(path)
        target.write_text('assert False\n')
        with self.assertRaisesRegex(ValueError, 'differs from its selected Git blob'):
            self.run_test(path)
        target.write_text(original)
        self.git('rm', path)
        self.git('commit', '--quiet', '-m', 'delete source test')
        status, results = self.run_test(path)
        self.assertEqual((status, results), (0, []))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(original)
        with self.assertRaisesRegex(ValueError, 'unexpected working file'):
            self.run_test(path)

    def test_symlink_and_parent_traversal_do_not_execute(self):
        path = 'scripts/test-state.py'
        target = self.commit(path, 'assert True\n')
        target.unlink()
        target.symlink_to(self.root / 'missing')
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            self.run_test(path)
        with self.assertRaisesRegex(ValueError, 'unsupported source test script'):
            self.run_test('scripts/../scripts/test-state.py')

    def test_perf_script_uses_the_existing_privileged_execution_context(self):
        path = 'test/perf/working_set_tap_test.sh'
        self.commit(path, 'exit 0\n')
        entry = subprocess.check_output(['git', 'ls-tree', '-z', 'HEAD', '--', path], cwd=self.root)
        with patch.object(SUBJECT.subprocess, 'check_output', return_value=entry), \
             patch.object(SUBJECT.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(SUBJECT.run_test_script('kuasar-sandbox/kuasar-sandbox', path, self.root), 0)
        run.assert_called_once_with(['sudo', '-n', '-E', 'bash', path], cwd=self.root)


if __name__ == "__main__":
    unittest.main()
