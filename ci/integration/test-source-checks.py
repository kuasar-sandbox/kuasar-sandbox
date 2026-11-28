#!/usr/bin/env python3
"""Complete source gates, exact-test pins and failure-propagation regressions."""

import importlib.util
from collections import Counter
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
import source_stages
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
        self.assertEqual(source_results.groups_for(self.plan, True), ['owner-platform'])

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

    def test_full_product_changes_keep_the_same_basic_gates_as_source_only(self):
        original = copy.deepcopy(self.plan)
        for owner, path in (('accelerator', 'pkg/manifest/ingest/zero.go'),
                            ('connector', 'cmd/connector-ctl/main.go'),
                            ('guest-runtime', 'cmd/flatten-ctl/main.go')):
            with self.subTest(owner=owner):
                self.plan = copy.deepcopy(original)
                select_plan(self.plan, [owner])
                self.plan['changes'] = {owner: [path]}
                full = [(name, command) for name, command, cwd in
                        SUBJECT.checks_for(self.plan, self.sources) if cwd == self.sources / owner]
                self.source_scope({owner: [path.removesuffix('.go') + '_test.go']})
                source_only = [(name, command) for name, command, cwd in
                               SUBJECT.checks_for(self.plan, self.sources) if cwd == self.sources / owner]
                self.assertTrue(source_only)
                self.assertEqual(full, source_only)

    def test_explicit_gate_input_selects_its_complete_owner_gate(self):
        original = copy.deepcopy(self.plan)
        for repository, path in sorted(SUBJECT.source_lane.GATE_INPUTS):
            owner = SUBJECT.source_lane.owner(repository)
            with self.subTest(repository=repository, path=path):
                self.plan = copy.deepcopy(original)
                self.source_scope({owner: [path]})
                artifacts.check_plan(self.plan)
                selected, _ = SUBJECT.source_records(self.plan)
                self.assertEqual(selected, {owner})
                checks = SUBJECT.checks_for(self.plan, self.sources)
                matching = [row for row in checks if row[0] == owner + '-unit-race-vet']
                self.assertEqual(matching, [(owner + '-unit-race-vet',
                    ['bash', 'scripts/ci-source-checks.sh'], self.sources / owner)])
                self.assertFalse(any('-test-script:' in row[0] for row in checks))
                group = 'B' if owner == 'orchestrator' else 'A'
                self.assertIn(matching[0], SUBJECT.checks_for(self.plan, self.sources, group))
                self.assertIn(matching[0], SUBJECT.checks_for(self.plan, self.sources, 'owner-' + owner))

    def test_pid1_or_wire_changes_keep_guest_race_tests_in_full_and_source_lanes(self):
        for path, source_only in (('cmd/sandbox-init/main.go', False),
                                  ('pkg/guestabi/proto/proto.go', False),
                                  ('internal/wireio/full_test.go', True)):
            with self.subTest(path=path):
                select_plan(self.plan, ['guest-runtime'])
                self.plan['changes'] = {'guest-runtime': [path]}
                if source_only:
                    self.source_scope(self.plan['changes'])
                rows = SUBJECT.checks_for(self.plan, self.sources)
                self.assertEqual([(name, command) for name, command, cwd in rows
                                  if cwd == self.sources / 'guest-runtime'],
                                 [('guest-runtime-source-tests', ['make', 'test', 'vet']),
                                  ('guest-runtime-race', ['env', 'CGO_ENABLED=1', 'go', 'test',
                                                          '-race', '-count=1', './...'])])
        # Ordinary Guest product changes still need the basic checks, but only
        # PID 1/wire changes select the additional race pass.
        self.plan.pop('source_scope')
        self.plan['changes'] = {'guest-runtime': ['docs/build.md', 'cmd/flatten-ctl/main.go']}
        self.assertEqual([(name, command) for name, command, cwd in
                          SUBJECT.checks_for(self.plan, self.sources)
                          if cwd == self.sources / 'guest-runtime'],
                         [('guest-runtime-source-tests', ['make', 'test', 'vet'])])
        select_plan(self.plan, ['connector'])
        self.assertFalse(any(cwd == self.sources / 'guest-runtime'
                             for _, _, cwd in SUBJECT.checks_for(self.plan, self.sources)))

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
        self.assertEqual(SUBJECT.checks_for(self.plan, self.sources, 'owner-orchestrator'), b)
        owner_groups = source_results.groups_for(self.plan, True)
        self.assertEqual(owner_groups, ['owner-platform', 'owner-connector', 'owner-orchestrator'])
        self.assertEqual(self.validate_evidence(self.grouped_evidence(owner_groups), owner_groups)['conclusion'], 'success')
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
        self.assertEqual(source_results.groups_for(self.plan, True), ['owner-platform'])
        self.assertEqual(self.validate_evidence(self.grouped_evidence(('owner-platform',)),
                                               ('owner-platform',))['conclusion'], 'success')
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
        self.assertEqual(source_results.groups_for(self.plan, True),
                         ['owner-platform', 'owner-connector', 'owner-accelerator'])

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
        expected = {"platform", "orchestrator", "sandboxer", "connector", "accelerator", "guest-runtime"}
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
             patch.object(SUBJECT, "workspace", side_effect=lambda root, owner, env: dict(env, GOWORK="off", TEST_WORKSPACE=owner)), \
             patch.dict(os.environ, {"TMPDIR": "/build/t"}):
            SUBJECT.execute(self.plan, self.sources, self.output, group=group)
        return commands

    def test_complete_source_gate_keeps_owner_entries_and_uffd_performance(self):
        commands = self.run_checks()
        result = self.result()
        self.assertEqual([row["name"] for row in result["checks"]],
                         ["platform-contracts", "platform-privileged-perf-contracts", "connector-go-tests",
                          "connector-unit-race-vet", "sandboxer-unit-race-vet", "orchestrator-unit-race-vet",
                          "accelerator-source-tests", "accelerator-no-rocksdb-tests", "guest-runtime-source-tests",
                          "uffd-source-benchmark"])
        self.assertEqual(commands[0][0], ["make", "test-ci-tools", "test-release-tools"])
        self.assertEqual(commands[1][0], ["sudo", "-n", "-E", "make", "test-perf-tools"])
        for command, _, _ in commands[3:6]:
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
                        self.assertEqual(source_results.legacy_groups_for(self.plan), ['A', 'B'] if b else ['A'])
                        owner_groups, owned = [], []
                        for group in SUBJECT.OWNER_GROUPS:
                            part = SUBJECT.checks_for(self.plan, self.sources, group)
                            self.assertEqual(part, [row for row in all_checks if 'owner-' + row[2].name == group])
                            if part:
                                owner_groups.append(group)
                                owned.extend(part)
                        identity = lambda rows: Counter((name, tuple(command), str(cwd)) for name, command, cwd in rows)
                        self.assertEqual(identity(owned), identity(all_checks))
                        self.assertTrue(all(count == 1 for count in identity(owned).values()))
                        self.assertEqual(source_results.groups_for(self.plan, True), [] if mode == 'exact-assets' else owner_groups)
                        self.assertEqual(source_results.groups_for(self.plan), [] if mode == 'exact-assets' else ['all'])
        with self.assertRaises(ValueError):
            SUBJECT.checks_for(self.plan, self.sources, 'unknown')

    def test_owner_projection_fails_closed_for_unknown_or_nested_directories(self):
        for directory in (self.sources / 'unknown', self.sources / 'platform' / 'ci', self.root / 'elsewhere'):
            with self.subTest(directory=str(directory)), self.assertRaises(ValueError):
                SUBJECT.owner_group(('unexpected', ['true'], directory), self.sources)
        self.plan['mode'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'unknown source scheduling mode'):
            source_results.groups_for(self.plan, True)

    def test_schedule_cli_emits_complete_owner_or_legacy_all_schedule(self):
        plan_path = self.root / 'plan.json'
        plan_path.write_text(json.dumps(self.plan))
        for schedule, expected in (('grouped', list(SUBJECT.OWNER_GROUPS)), ('all', ['all'])):
            with self.subTest(schedule=schedule):
                result = subprocess.run([sys.executable, '-B', source_results.__file__,
                                         '--plan', str(plan_path), '--schedule', schedule],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)

    def test_owner_projection_keeps_scoped_race_scripts_and_documentation(self):
        self.source_scope({'guest-runtime': ['internal/wireio/full_test.go', 'release/guide-migrations.json'],
                           'connector': ['scripts/test-new.py'],
                           'orchestrator': ['test/source/test-new.sh'],
                           'accelerator': ['pkg/tarstream/testdata/legacy-byte-holes.tar'],
                           'platform': ['docs/contributing.md']})
        groups = source_results.groups_for(self.plan, True)
        self.assertEqual(groups, ['owner-platform', 'owner-connector', 'owner-accelerator',
                                  'owner-guest-runtime', 'owner-orchestrator'])
        rows = SUBJECT.checks_for(self.plan, self.sources)
        for group in groups:
            self.assertEqual(SUBJECT.checks_for(self.plan, self.sources, group),
                             [row for row in rows if 'owner-' + row[2].name == group])
        self.assertIn('guest-runtime-race', [row[0] for row in rows])
        self.assertIn('source-documentation-package', [row[0] for row in
                      SUBJECT.checks_for(self.plan, self.sources, 'owner-platform')])
        self.assertEqual(self.validate_evidence(self.grouped_evidence(groups), groups)['conclusion'], 'success')

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
        for groups in (('all',), ('A', 'B'), SUBJECT.OWNER_GROUPS):
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
        for groups in (('A', 'B'), SUBJECT.OWNER_GROUPS):
            for index, mutate in enumerate(mutations):
                with self.subTest(groups=groups, mutation=index):
                    data = self.grouped_evidence(groups)
                    mutate(data)
                    with self.assertRaises(ValueError):
                        self.validate_evidence(data, groups)

    def test_new_producer_attempt_invalidates_old_artifact(self):
        for groups in (('A', 'B'), SUBJECT.OWNER_GROUPS):
            for conclusion in ('success', 'failure', 'cancelled', None):
                with self.subTest(groups=groups, conclusion=conclusion):
                    data = self.grouped_evidence(groups)
                    latest = dict(data[1][0], id=301, run_attempt=2, conclusion=conclusion,
                                  status='in_progress' if conclusion is None else 'completed')
                    data[1].append(latest)
                    with self.assertRaises(ValueError):
                        self.validate_evidence(data, groups)
            data = self.grouped_evidence(groups)
            data[1].append(dict(data[1][0], id=301, run_attempt=2))
            data[2].append(dict(data[2][0], id=1000, name=source_results.artifact_name(123, 2, groups[0])))
            data[3][1000] = copy.deepcopy(data[3][1])
            self.assertEqual([row['run_attempt'] for row in self.validate_evidence(data, groups)['groups']],
                             [2] + [1] * (len(groups) - 1))

    def test_owner_results_reject_partial_mixed_and_unknown_schedules(self):
        groups = SUBJECT.OWNER_GROUPS
        for incomplete in (groups[:-1], groups[1:], ('A',) + groups, ('all',) + groups,
                           ('owner-platform', 'B'), groups + (groups[0],)):
            with self.subTest(groups=incomplete), self.assertRaisesRegex(ValueError, 'unexpected source group plan'):
                self.validate_evidence(self.grouped_evidence(incomplete), incomplete)
        for name in ('source-checks (owner-unknown)', 'source-checks (C)', 'source-checks',
                     'source-checks (owner-platform) extra', 'source-checks-unknown'):
            with self.subTest(name=name):
                data = self.grouped_evidence(groups)
                data[1].append(dict(data[1][0], name='ci / ' + name, id=800))
                with self.assertRaisesRegex(ValueError, 'unknown source producer'):
                    self.validate_evidence(data, groups)

    def test_collector_independently_rejects_command_union_omissions_and_duplicates(self):
        # Make every per-group result agree with a broken projection; the
        # independent complete command multiset must still reject the union.
        groups = SUBJECT.OWNER_GROUPS
        original = source_results.checks.checks_for
        for mutation in ('omission', 'duplicate'):
            data = self.grouped_evidence(groups)
            target = 'owner-platform' if mutation == 'omission' else 'owner-connector'
            def broken(plan, sources, group='all'):
                rows = original(plan, sources, group)
                if group == target:
                    return rows[1:] if mutation == 'omission' else rows + [original(plan, sources)[0]]
                return rows
            document = data[3][groups.index(target) + 1]['result']
            document['checks'] = [dict(name=n, command=c, directory=str(d), exit_code=0)
                                  for n, c, d in broken(self.plan, Path('/src'), target)]
            with self.subTest(mutation=mutation), patch.object(source_results.checks, 'checks_for', side_effect=broken):
                with self.assertRaisesRegex(ValueError, 'every command exactly once'):
                    self.validate_evidence(data, groups)

    def test_duplicate_older_attempt_rejected_in_unsorted_history(self):
        data = self.grouped_evidence()
        older = copy.deepcopy(data[1][0])
        data[1] = [dict(older, id=301, run_attempt=2), older, dict(older, id=302), data[1][1]]
        data[2].append(dict(data[2][0], id=3, name=source_results.artifact_name(123, 2, 'A')))
        data[3][3] = copy.deepcopy(data[3][1])
        with self.assertRaisesRegex(ValueError, 'duplicate source producer'):
            self.validate_evidence(data)

    def test_each_group_executes_original_commands_and_propagates_failure(self):
        for group in ('A', 'B', *SUBJECT.OWNER_GROUPS):
            with self.subTest(group=group):
                self.sources = self.root / group
                commands = self.run_checks(group=group)
                self.assertEqual([(command, directory) for command, directory, _ in commands],
                                 [(c, d) for _, c, d in SUBJECT.checks_for(self.plan, self.sources, group)])
                self.assertEqual(self.result()['group'], group)
                for row, (_, directory, env) in zip(self.result()['checks'], commands):
                    self.assertEqual(env['TEST_WORKSPACE'],
                                     'sandboxer' if row['name'] == 'uffd-source-benchmark' else directory.name)
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

    def test_single_pattern_match_is_flattened_for_legacy_or_owner(self):
        for group in ('all', 'A', 'owner-platform', 'owner-guest-runtime'):
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
                         ["platform-contracts", "platform-privileged-perf-contracts", "connector-go-tests",
                          "connector-unit-race-vet"])
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


class SourceStageTests(unittest.TestCase):
    setUp = SourceChecksTests.setUp
    source_scope = SourceChecksTests.source_scope
    grouped_evidence = SourceChecksTests.grouped_evidence
    validate_evidence = SourceChecksTests.validate_evidence

    def enable(self, text=None):
        owner = self.sources / 'orchestrator'
        owner.mkdir(parents=True, exist_ok=True)
        entry = owner / source_stages.ENTRY
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(text or '#!/bin/bash\n# kuasar-source-stages: orchestrator-source-v1\n'
                         'set -eu\n[ "$1" = --ci-stage ]\nprintf "%s\\n" "$2" > observed-stage\n'
                         'exit "${FIXTURE_STAGE_EXIT:-0}"\n')
        entry.chmod(0o644)
        for argv in (['init', '--quiet'], ['config', 'user.name', 'Stage Fixture'],
                     ['config', 'user.email', 'stage@example.invalid'], ['config', 'commit.gpgsign', 'false'],
                     ['add', source_stages.ENTRY], ['commit', '--quiet', '--allow-empty', '-m', 'stage fixture']):
            subprocess.run(['git', *argv], cwd=owner, check=True, capture_output=True)
        self.identity = dict(repository=source_stages.REPOSITORY,
                             sha=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=owner, text=True).strip(),
                             tree=subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=owner, text=True).strip())
        self.plan['test_revisions']['orchestrator']['sha'] = self.identity['sha']
        self.plan['run_inputs'] = {'candidates/orchestrator': self.identity}
        real_run = subprocess.run
        def git_only(command, **kwargs):
            self.assertEqual(command[:3], ['git', 'ls-tree', '-z'])
            return real_run(command, **kwargs)
        with patch.object(source_stages.subprocess, 'run', side_effect=git_only) as run:
            selected = source_stages.discover({'orchestrator': owner}, {'orchestrator': self.identity})
        self.assertLessEqual(run.call_count, 1)
        if selected is not None:
            self.plan['source_stages'] = selected
        return entry

    @staticmethod
    def packages(count=102):
        names = ['internal/orch', 'internal/proxyapp'] + [f'pkg/p{i:03}' for i in range(max(0, count - 2))]
        return sorted([dict(import_path=source_stages.MODULE + '/' + name, has_tests=i < 82)
                       for i, name in enumerate(names[:count])], key=lambda row: row['import_path'])

    def proof(self, stage, rows=None, directory='/src/orchestrator'):
        result = dict(schema=1, plan_id=artifacts.identity(self.plan), protocol=self.plan['source_stages']['protocol'],
                      stage=stage, source_contract=copy.deepcopy(self.plan['source_stages']),
                      conclusion='success', exit_code=0)
        argv = [['bash', source_stages.ENTRY, '--ci-stage', stage]]
        if stage in source_stages.RACE_STAGES:
            rows = self.packages() if rows is None else rows
            names = source_stages.partition(rows)[stage]
            test_flags = {row['import_path']: row['has_tests'] for row in rows}
            result['race'] = dict(policy=source_stages.RACE_POLICY,
                context=dict(GOVERSION='go1.26.11', GOOS='linux', GOARCH='amd64', CGO_ENABLED='1',
                             GOFLAGS='', workspace_sha256='a' * 64), inventory=rows, selected=names,
                results=[dict(import_path=name, action='pass' if test_flags[name] else 'skip') for name in names])
            argv = [source_stages.ENV_COMMAND, source_stages.LIST_COMMAND]
            if names:
                argv.append(source_stages.RACE_COMMAND + names)
        result['commands'] = [dict(command=command, directory=directory, exit_code=0, wall_seconds=0.1)
                              for command in argv]
        return result

    def evidence(self):
        groups = source_results.groups_for(self.plan, True)
        data = self.grouped_evidence(groups)
        for item in data[3].values():
            stage = source_stages.stage_for(item['result']['group'])
            if stage:
                item['result']['source_stage'] = self.proof(stage)
        return groups, data

    def test_v2_uses_one_owner_race_gate_and_rejects_legacy_partitions(self):
        self.enable('#!/bin/bash\n# kuasar-source-stages: orchestrator-source-v2\nexit 0\n')
        stages = source_stages.CASE_STAGES
        self.assertEqual(source_stages.stages_for(self.plan), stages)
        groups, data = self.evidence()
        self.assertEqual([source_stages.stage_for(g) for g in groups if source_stages.stage_for(g)], list(stages))
        source_stages.validate_set(self.plan, {stage: self.proof(stage) for stage in stages})
        proof = source_stages.execute(self.plan, self.sources, 'race', self.output, os.environ.copy())
        self.assertEqual([r['command'] for r in proof['commands']], [['bash', source_stages.ENTRY, '--ci-stage', 'race']])
        source_stages.validate_proof(self.plan, 'race', proof, directory=str(self.sources / 'orchestrator'))
        with self.assertRaisesRegex(ValueError, 'unknown execution stage'):
            source_stages.execute(self.plan, self.sources, 'race-1', self.output, os.environ.copy())
        with self.assertRaisesRegex(ValueError, 'missing or unexpected'):
            source_stages.validate_set(self.plan, {stage: self.proof(stage) for stage in stages if stage != 'race'})
        self.validate_evidence(data, groups)

    def test_v2_failed_case_gate_cannot_produce_success(self):
        self.enable('#!/bin/bash\n# kuasar-source-stages: orchestrator-source-v2\nexit 17\n')
        with self.assertRaises(subprocess.CalledProcessError):
            source_stages.execute(self.plan, self.sources, 'race', self.output, os.environ.copy())
        proof = json.loads(self.output.read_text())
        self.assertEqual(proof['exit_code'], 17)
        with self.assertRaisesRegex(ValueError, 'identity or success'):
            source_stages.validate_proof(self.plan, 'race', proof)

    def test_static_declaration_is_bound_to_admitted_tree_and_never_executes_owner(self):
        entry = self.enable()
        artifacts.check_plan(self.plan)
        self.assertFalse((entry.parent.parent / 'observed-stage').exists())
        self.assertEqual(self.plan['source_stages']['entry']['sha256'], hashlib.sha256(entry.read_bytes()).hexdigest())
        for field in ('sha', 'tree'):
            wrong = dict(self.identity, **{field: 'a' * 40})
            if field == 'tree':
                with self.assertRaises(subprocess.CalledProcessError):
                    source_stages.discover({'orchestrator': entry.parent.parent}, {'orchestrator': wrong})
            else:
                changed = copy.deepcopy(self.plan)
                changed['source_stages']['source'][field] = wrong[field]
                with self.assertRaisesRegex(ValueError, 'owner identity'):
                    artifacts.check_plan(changed)

    def test_absent_and_unknown_protocol_keep_every_whole_owner_command(self):
        original = SUBJECT.checks_for(self.plan, self.sources)
        groups = source_results.groups_for(self.plan, True)
        for header in ('#!/bin/bash\n', '# kuasar-source-stages: orchestrator-source-v99\n'):
            self.enable(header + 'echo do-not-execute > touched\n')
            self.assertNotIn('source_stages', self.plan)
            self.assertEqual(SUBJECT.checks_for(self.plan, self.sources), original)
            self.assertEqual(source_results.groups_for(self.plan, True), groups)
            self.assertFalse((self.sources / 'orchestrator/touched').exists())

    def test_duplicate_malformed_and_linked_declarations_are_rejected(self):
        headers = ['# kuasar-source-stages: orchestrator-source-v1\n' * 2,
                   '# kuasar-source-stages: orchestrator-source-v1 extra\n',
                   '# kuasar-source-stages: $(touch injected)\n']
        for header in headers:
            with self.subTest(header=header), self.assertRaises(ValueError):
                self.enable(header + 'exit 0\n')
        entry = self.enable()
        entry.unlink()
        entry.symlink_to('/dev/null')
        with self.assertRaises(OSError):
            source_stages.discover({'orchestrator': entry.parent.parent}, {'orchestrator': self.identity})
        entry.unlink()
        entry.parent.rename(entry.parent.with_name('real-scripts'))
        entry.parent.symlink_to('real-scripts', target_is_directory=True)
        with self.assertRaises(OSError):
            source_stages.discover({'orchestrator': entry.parent.parent}, {'orchestrator': self.identity})

    def test_untracked_oversized_or_changed_declared_entry_cannot_activate(self):
        entry = self.enable()
        entry.write_text(entry.read_text() + '# changed\n')
        with self.assertRaisesRegex(ValueError, 'Git blob'):
            source_stages.discover({'orchestrator': entry.parent.parent}, {'orchestrator': self.identity})
        with self.assertRaisesRegex(ValueError, 'differs from the plan'):
            source_stages.verify_entry(self.plan, entry.parent.parent)
        entry.write_bytes(b'#' * (source_stages.ENTRY_LIMIT + 1))
        with self.assertRaisesRegex(ValueError, 'bounded regular'):
            source_stages.discover({'orchestrator': entry.parent.parent}, {'orchestrator': self.identity})

    def test_contract_has_closed_stage_policy_identity_and_no_commands(self):
        self.enable()
        for mutate in (lambda v: v.update(commands=['true']), lambda v: v['stages'].pop(),
                       lambda v: v.update(race_partition='future'), lambda v: v.update(protocol='future'),
                       lambda v: v['entry'].update(path='../other.sh'),
                       lambda v: v['source'].update(tree='c' * 40), lambda v: v.update(owner='sandboxer')):
            plan = copy.deepcopy(self.plan)
            mutate(plan['source_stages'])
            with self.assertRaises(ValueError):
                artifacts.check_plan(plan)
        for value in (None, {}, []):
            with self.assertRaises(ValueError):
                artifacts.check_plan(dict(self.plan, source_stages=value))
        with self.assertRaises(ValueError):
            source_stages.contract(dict(self.plan, mode='exact-assets'))

    def test_staged_projection_keeps_other_owners_and_extra_checks_exactly_once(self):
        self.enable()
        self.source_scope({'orchestrator': ['test/source/test-new.sh'], 'connector': ['pkg/new_test.go']})
        groups = source_results.groups_for(self.plan, True)
        self.assertEqual(groups, ['owner-platform', 'owner-connector', *source_stages.GROUPS])
        actual = [row for group in groups for row in SUBJECT.checks_for(self.plan, self.sources, group)]
        expected = source_stages.expand(SUBJECT.checks_for(self.plan, self.sources), self.sources)
        self.assertEqual(Counter((n, tuple(c), str(d)) for n, c, d in actual),
                         Counter((n, tuple(c), str(d)) for n, c, d in expected))
        self.assertEqual(actual[-1][0], 'orchestrator-test-script:test/source/test-new.sh')
        self.assertEqual(sum(name.endswith('test-new.sh') for name, _, _ in actual), 1)
        self.assertEqual(SUBJECT.checks_for(self.plan, self.sources, 'owner-orchestrator')[0][1],
                         ['bash', 'scripts/ci-source-checks.sh'])
        self.assertEqual(source_results.groups_for(self.plan, False), ['all'])
        self.source_scope({'platform': ['ci/integration/test-new.py']})
        self.assertEqual(source_results.groups_for(self.plan, True), ['owner-platform'])

    def test_every_complete_historical_or_declared_schedule_is_accepted(self):
        self.enable()
        for groups in (['all'], ['A', 'B'], list(SUBJECT.OWNER_GROUPS)):
            self.assertEqual(self.validate_evidence(self.grouped_evidence(groups), groups)['conclusion'], 'success')
        groups, data = self.evidence()
        self.assertEqual(self.validate_evidence(data, groups)['conclusion'], 'success')

    def test_missing_mixed_duplicate_and_unknown_stage_schedules_are_rejected(self):
        self.enable()
        groups, data = self.evidence()
        for wrong in (groups[:-1], groups + ['owner-orchestrator'], groups + [source_stages.GROUPS[0]],
                      ['owner-platform', source_stages.GROUPS[0]]):
            with self.subTest(groups=wrong), self.assertRaises(ValueError):
                self.validate_evidence(data, wrong)
        for label in ('source-checks (owner-orchestrator--unknown)', 'source-checks (race-1)'):
            bad = copy.deepcopy(data)
            bad[1][0]['name'] = label
            with self.assertRaisesRegex(ValueError, 'unknown source producer'):
                self.validate_evidence(bad, groups)

    def test_102_package_union_includes_all_twenty_no_test_and_future_packages(self):
        rows = self.packages()
        self.assertEqual(len(rows), 102)
        self.assertEqual(sum(not row['has_tests'] for row in rows), 20)
        groups = source_stages.partition(rows)
        self.assertEqual(groups['race-1'], [source_stages.MODULE + '/internal/orch'])
        self.assertEqual(groups['race-2'], [source_stages.MODULE + '/internal/proxyapp'])
        self.assertEqual(Counter(name for names in groups.values() for name in names),
                         Counter(row['import_path'] for row in rows))
        future = dict(import_path=source_stages.MODULE + '/future/new', has_tests=False)
        updated = source_stages.partition(sorted([*rows, future], key=lambda row: row['import_path']))
        self.assertEqual(sum(future['import_path'] in names for names in updated.values()), 1)
        for count in (1, 2, 3):
            tiny = source_stages.partition(self.packages(count))
            self.assertEqual(sum(map(len, tiny.values())), count)

    def test_inventory_rejects_duplicates_errors_dependencies_and_unsafe_operands(self):
        rows = self.packages(4)
        invalid = [[], rows + [rows[0]], list(reversed(rows)),
                   [dict(import_path='-run=NoTests', has_tests=True)],
                   [dict(import_path=source_stages.MODULE + '/../outside', has_tests=True)],
                   [dict(import_path=source_stages.MODULE + '/pkg', has_tests=1)]]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                source_stages.partition(value)
        for field in ('Error', 'DepsErrors', 'Incomplete', 'ForTest'):
            with self.assertRaises(ValueError):
                source_stages.decode_inventory(json.dumps({'ImportPath': source_stages.MODULE + '/p', field: True}))

    def test_real_go_discovery_includes_race_only_packages_and_same_goflags(self):
        # Package discovery is a real Go invocation, without compiling or
        # executing product code. The race tag is implicit in the old gate.
        root = self.root / 'tagged-module'
        root.mkdir()
        (root / 'go.mod').write_text('module ' + source_stages.MODULE + '\n\ngo 1.22\n')
        for name, tag in (('base', ''), ('raceonly', 'race'), ('customtag', 'stagefixture')):
            directory = root / name
            directory.mkdir()
            header = '//go:build ' + tag + '\n\n' if tag else ''
            (directory / 'package.go').write_text(header + 'package ' + name + '\n')
            if tag == 'race':
                (directory / 'package_test.go').write_text(
                    header + 'package ' + name + '\nimport "testing"\nfunc TestRaceOnly(t *testing.T) {}\n')
        env = dict(os.environ, CGO_ENABLED='1', GOFLAGS='-tags=stagefixture', GOWORK='off',
                   GOPROXY='off', GOSUMDB='off', GOTOOLCHAIN='local', GOCACHE=str(self.root / 'go-list-cache'))
        rows = source_stages.decode_inventory(subprocess.check_output(
            source_stages.LIST_COMMAND, cwd=root, env=env, text=True))
        self.assertEqual(rows, [dict(import_path=source_stages.MODULE + '/' + name, has_tests=name == 'raceonly')
                                for name in ('base', 'customtag', 'raceonly')])
        plain = source_stages.decode_inventory(subprocess.check_output(
            [arg for arg in source_stages.LIST_COMMAND if arg != '-race'], cwd=root, env=env, text=True))
        self.assertEqual([row['import_path'] for row in plain],
                         [source_stages.MODULE + '/base', source_stages.MODULE + '/customtag'])
        self.assertEqual(Counter(name for names in source_stages.partition(rows).values() for name in names),
                         Counter(row['import_path'] for row in rows))

    def test_collector_rejects_different_inventory_context_flags_results_or_entry(self):
        self.enable()
        groups, data = self.evidence()
        index = next(key for key, value in data[3].items() if value['result']['group'].endswith('--race-1'))
        mutations = [lambda p: p['source_contract']['entry'].update(sha256='b' * 64),
                     lambda p: p.update(stage='normal'), lambda p: p.update(exit_code=17),
                     lambda p: p.update(schema=True),
                     lambda p: p['commands'][1]['command'].remove('-race'),
                     lambda p: p['commands'][-1]['command'].remove('-race'),
                     lambda p: p['race']['results'].pop(),
                     lambda p: p['race']['results'].append(p['race']['results'][0]),
                     lambda p: p['race']['results'][0].update(action='skip'),
                     lambda p: p['race']['context'].update(GOVERSION='go1.26.12'),
                     lambda p: p['race']['context'].update(workspace_sha256='f' * 64)]
        for mutate in mutations:
            bad = copy.deepcopy(data)
            mutate(bad[3][index]['result']['source_stage'])
            with self.assertRaises(ValueError):
                self.validate_evidence(bad, groups)
        bad = copy.deepcopy(data)
        del bad[3][index]['result']['source_stage']
        with self.assertRaises(ValueError):
            self.validate_evidence(bad, groups)
        bad = copy.deepcopy(data)
        race = bad[3][index]['result']['source_stage']['race']
        # This package remains in another partition, so this worker alone can
        # look consistent. The cross-worker complete-inventory check must fail.
        race['inventory'][-1]['has_tests'] = not race['inventory'][-1]['has_tests']
        with self.assertRaisesRegex(ValueError, 'different package inventories'):
            self.validate_evidence(bad, groups)

    def test_terminal_parser_distinguishes_individual_skip_and_no_test_package(self):
        rows = self.packages(3)
        rows[-1]['has_tests'] = False
        selected = [row['import_path'] for row in rows]
        events = [dict(Action='build-output', ImportPath='dependency.invalid/pkg', Output='warning\n'),
                  dict(Action='skip', Package=selected[0], Test='TestOptional'),
                  *[dict(Action='pass' if row['has_tests'] else 'skip', Package=row['import_path']) for row in rows]]
        path = self.root / 'events.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in events))
        self.assertEqual(len(source_stages.terminals(path, selected, rows)), 3)
        for bad in (events[:-1], events + [events[-1]],
                    events[:2] + [dict(Action='skip', Package=selected[0]), *events[3:]],
                    events + [dict(Action='pass', Package=source_stages.MODULE + '/unexpected')]):
            path.write_text(''.join(json.dumps(row) + '\n' for row in bad))
            with self.assertRaises(ValueError):
                source_stages.terminals(path, selected, rows)

    def test_real_stage_entry_failure_and_post_execution_mutation_stay_failure(self):
        entry = self.enable()
        output = self.root / 'stage-output/proof.json'
        for exit_code in (0, 23):
            env = {**os.environ, 'FIXTURE_STAGE_EXIT': str(exit_code)}
            if exit_code:
                with self.assertRaises(subprocess.CalledProcessError) as error:
                    source_stages.execute(self.plan, self.sources, 'normal', output, env)
                self.assertEqual(error.exception.returncode, exit_code)
            else:
                source_stages.execute(self.plan, self.sources, 'normal', output, env)
            proof = json.loads(output.read_text())
            self.assertEqual(proof['exit_code'], exit_code)
            self.assertEqual(proof['conclusion'], 'failure' if exit_code else 'success')
            self.assertEqual((entry.parent.parent / 'observed-stage').read_text(), 'normal\n')
        entry = self.enable('#!/bin/bash\n# kuasar-source-stages: orchestrator-source-v1\n'
                            'printf "# mutate\\n" >> scripts/ci-source-checks.sh\n')
        with self.assertRaisesRegex(ValueError, 'differs from the plan'):
            source_stages.execute(self.plan, self.sources, 'normal', output, os.environ)
        self.assertEqual(json.loads(output.read_text())['conclusion'], 'failure')

    def fake_go(self):
        binary = self.root / 'bin'
        binary.mkdir()
        executable = binary / 'go'
        executable.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['FIXTURE_COMMANDS'], 'a') as log: log.write(json.dumps(args) + '\\n')
rows = json.loads(pathlib.Path(os.environ['FIXTURE_PACKAGES']).read_text())
assert os.environ['CGO_ENABLED'] == '1'
if args[:2] == ['env', '-json']:
    assert args[2:] == ['GOVERSION', 'GOOS', 'GOARCH', 'CGO_ENABLED', 'GOFLAGS', 'GOWORK']
    print(json.dumps(dict(GOVERSION='go1.26.11', GOOS='linux', GOARCH='amd64', CGO_ENABLED='1', GOFLAGS='', GOWORK='off')))
elif args == ['list', '-race', '-json', './...']:
    for row in rows:
        print(json.dumps(dict(ImportPath=row['import_path'], TestGoFiles=['real_test.go'] if row['has_tests'] else [])))
elif args[:6] == ['test', '-json', '-race', '-p=1', '-count=1', '-timeout=5m']:
    assert args[6:] and len(set(args[6:])) == len(args[6:])
    flags={row['import_path']: row['has_tests'] for row in rows}
    for name in args[6:]: print(json.dumps(dict(Action='pass' if flags[name] else 'skip', Package=name)))
    raise SystemExit(int(os.environ.get('FIXTURE_GO_EXIT', '0')))
else:
    raise SystemExit('unexpected fixture argv: ' + repr(args))
''')
        executable.chmod(0o755)
        packages = self.root / 'packages.json'
        packages.write_text(json.dumps(self.packages()))
        return {**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'],
                'FIXTURE_PACKAGES': str(packages), 'FIXTURE_COMMANDS': str(self.root / 'commands.jsonl')}

    def test_four_real_fixture_workers_execute_literal_race_commands_and_full_union(self):
        self.enable()
        env = self.fake_go()
        proofs = {}
        for stage in source_stages.RACE_STAGES:
            output = self.root / stage / 'proof.json'
            proof = source_stages.execute(self.plan, self.sources, stage, output, env)
            source_stages.validate_proof(self.plan, stage, proof, directory=str(self.sources / 'orchestrator'))
            proofs[stage] = proof
        rows = [json.loads(line) for line in (self.root / 'commands.jsonl').read_text().splitlines()]
        tests = [row for row in rows if row[0] == 'test']
        self.assertEqual(len(tests), 4)
        self.assertEqual(Counter(name for command in tests for name in command[6:]),
                         Counter(row['import_path'] for row in self.packages()))
        self.assertEqual(sum(len(p['race']['results']) for p in proofs.values()), 102)

    def test_success_shaped_race_json_does_not_hide_real_nonzero_exit(self):
        self.enable()
        env = dict(self.fake_go(), FIXTURE_GO_EXIT='19')
        output = self.root / 'race-failure/proof.json'
        with self.assertRaises(subprocess.CalledProcessError) as error:
            source_stages.execute(self.plan, self.sources, 'race-1', output, env)
        self.assertEqual(error.exception.returncode, 19)
        proof = json.loads(output.read_text())
        self.assertEqual((proof['conclusion'], proof['exit_code']), ('failure', 19))
        self.assertTrue((output.parent / 'source-race-1.events.jsonl').is_file())
        with self.assertRaises(ValueError):
            source_stages.validate_proof(self.plan, 'race-1', proof)

    def test_real_worker_cli_preserves_original_exit_and_frozen_plan(self):
        self.enable()
        plan_path, output = self.root / 'plan.json', self.root / 'cli/proof.json'
        plan_path.write_text(json.dumps(self.plan))
        argv = [sys.executable, '-B', SUBJECT.__file__, '--source-stage', 'normal',
                '--plan', str(plan_path), '--sources', str(self.sources), '--output', str(output)]
        for exit_code in (0, 27):
            completed = subprocess.run(argv, env={**os.environ, 'FIXTURE_STAGE_EXIT': str(exit_code)},
                                       capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, exit_code, completed.stderr)
            proof = json.loads(output.read_text())
            self.assertEqual(proof['exit_code'], exit_code)
            self.assertEqual(proof['plan_id'], artifacts.identity(self.plan))

    def test_outer_executor_requires_fresh_proof_and_preserves_worker_failure(self):
        self.enable()
        group = 'owner-orchestrator--normal'
        proof_path = self.output.parent / 'source-stage.json'
        for mutation in ('missing', 'failure', 'success', 'stale'):
            proof_path.unlink(missing_ok=True)
            if mutation == 'stale':
                proof_path.write_text('{}')
            def child(command, *, cwd, env):
                self.assertEqual(command, source_stages.check('normal', self.sources)[1])
                if mutation != 'missing':
                    proof = self.proof('normal', directory=str(cwd))
                    if mutation == 'failure':
                        proof.update(conclusion='failure', exit_code=29)
                    proof_path.write_text(json.dumps(proof))
                return subprocess.CompletedProcess(command, 29 if mutation == 'failure' else 0)
            with self.subTest(mutation=mutation), \
                 patch.object(SUBJECT, 'verify_sources', side_effect=lambda p, s, r, e:
                              r.update(sources=SUBJECT.source_records(p)[1])), \
                 patch.object(SUBJECT, 'workspace', side_effect=lambda s, o, e: e), \
                 patch.object(SUBJECT.subprocess, 'run', side_effect=child) as run:
                if mutation == 'success':
                    SUBJECT.execute(self.plan, self.sources, self.output, materialized=True, group=group)
                else:
                    with self.assertRaises(ValueError):
                        SUBJECT.execute(self.plan, self.sources, self.output, materialized=True, group=group)
                result = json.loads(self.output.read_text())
                self.assertEqual(result['conclusion'], 'success' if mutation == 'success' else 'failure')
                if mutation == 'failure':
                    self.assertEqual(result['exit_code'], 29)
                    self.assertEqual(result['source_stage']['exit_code'], 29)
                if mutation == 'stale':
                    run.assert_not_called()

    def test_stage_artifact_group_round_trips_without_splitting_its_suffix(self):
        self.enable()
        group = 'owner-orchestrator--vet-and-helpers'
        name = source_results.artifact_name(123, 2, group)
        directory = self.root / 'artifact'
        for relative in ('source-system-evidence/output/source-result.json',
                         'source-system-evidence/receipt.json', 'source-system-evidence/instance.json'):
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}')
        actual = source_results.read_artifact(directory, [{'id': 1, 'name': name}], 1)
        self.assertEqual(actual, dict(result={}, receipt={}, instance={}))
        self.assertEqual(source_results.producer_group('ci / source-checks (' + group + ')'), group)


if __name__ == "__main__":
    unittest.main()
