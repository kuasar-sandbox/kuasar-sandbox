"""Grouped runtime delivery, complete readers and attempt invalidation contracts."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'ci/integration'), str(ROOT / 'release')]
import artifacts
import workbench_groups as groups
import test_workbench_binding as fixtures


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BindingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.plan = self.fixture.plan
        self.run = dict(id=123, run_attempt=2, head_sha=self.plan['framework_sha'],
                        event='workflow_dispatch', path='.github/workflows/aggregate-release.yml')
        self.jobs, self.available, self.results = [], [], []
        for index, group in enumerate(('A', 'B'), 1):
            self.jobs.append(dict(id=index, name=f'release-asset-validation / workbench-release (x86_64, {group})',
                run_id=123, run_attempt=1, runner_name=f'host-{index}', status='completed', conclusion='success',
                started_at='2026-10-14T01:00:00Z', completed_at='2026-10-14T02:00:00Z'))
            self.available.append(dict(id=index, name=groups.artifact_name(123, 1, group), expired=False,
                workflow_run=dict(id=123, head_sha=self.run['head_sha']), created_at='2026-10-14T01:59:00Z'))
            record = copy.deepcopy(self.fixture.native_full()['x86_64'])
            cases = groups.group_cases(self.plan['case_files'])[group]
            record.update(group=group, cases=cases, cpus=2, memory_gib=8, cleanup_exit_code=0,
                          instance_id=f'instance-{index}', container_id=f'container-{index}',
                          prepared_cases_sha256=artifacts.identity(artifacts.workbench_cases(self.plan['case_files'], 'x86_64', native_full=True)),
                          timings=[dict(case=case, exit_code=0, wall_seconds=.1) for case in cases])
            self.results.append(record)
        for index, name, artifact in [(11, 'stage', 'aggregate-stage-release-v1.2.3-123'),
                                     (12, 'release-asset-validation / resolve', 'integration-plan-123')]:
            self.jobs.append(dict(id=index, name=name, run_id=123, run_attempt=1, status='completed', conclusion='success',
                started_at='2026-10-14T00:00:00Z', completed_at='2026-10-14T00:59:00Z'))
            self.available.append(dict(id=index, name=artifact, expired=False, workflow_run=dict(id=123, head_sha=self.run['head_sha']),
                                       created_at='2026-10-14T00:30:00Z'))
        self.arm = self.fixture.artifact_only()['aarch64']
        self.jobs.append(dict(id=13, name='release-asset-validation / workbench-release (aarch64, all)', run_id=123,
            run_attempt=1, status='completed', conclusion='success', started_at='2026-10-14T01:00:00Z',
            completed_at='2026-10-14T01:30:00Z'))
        self.available.append(dict(id=13, name='integration-workbench-result-aarch64-all-123-1', expired=False,
            workflow_run=dict(id=123, head_sha=self.run['head_sha']), created_at='2026-10-14T01:29:00Z'))
        self.jobs.append(dict(id=21, name='release-asset-validation / results', run_id=123, run_attempt=2,
                              status='completed', conclusion='success', started_at='2026-10-14T02:00:01Z'))

    def aggregate(self):
        return groups.aggregate(self.plan, self.results, self.run, self.jobs, self.available, 21, self.arm)

    def test_disjoint_complete_inventory_order_obs_and_new_suite(self):
        expected = artifacts.workbench_cases(self.plan['case_files'], 'x86_64', native_full=True)
        selected = groups.group_cases(self.plan['case_files'])
        self.assertEqual(sorted(selected['A'] + selected['B']), expected)
        self.assertTrue(set(selected['A']).isdisjoint(selected['B']))
        for group, cases in selected.items():
            self.assertEqual(cases, [case for case in expected if case.split('.')[0] in groups.SUITES[group]])
        self.assertNotIn('storage.obs.sh', selected['B'])
        changed = copy.deepcopy(self.plan['case_files'])
        changed['platform'].append('new-suite.example.sh')
        with self.assertRaises(ValueError):
            groups.group_cases(changed)
        changed = copy.deepcopy(self.plan['case_files'])
        changed['platform'].append('basic.future.sh')
        self.assertIn('basic.future.sh', groups.group_cases(changed)['A'])

    def test_grouped_and_legacy_arm_readers_bind_complete_publication(self):
        result = self.aggregate()
        groups.verify_current(result, self.run, self.jobs)
        combined = self.fixture.artifact_only()
        combined['x86_64'] = result
        with patch.object(groups, 'api', return_value=self.run), patch.object(groups, 'pages', return_value=self.jobs):
            fixtures.binder.bind(self.fixture.root, self.plan, self.fixture.validation, combined)
        notes = (self.fixture.root / 'release-notes.md').read_text()
        binding = json.loads(notes.split('<!-- kuasar-integration-validation ')[1].split(' -->')[0])
        self.assertEqual(binding['workbench'], combined)
        binding['registry'] = fixtures.registry_binding(self.fixture.version, combined)
        artifacts.check_registry_binding(self.fixture.version, binding)
        self.assertLess(len(notes), fixtures.binder.publication_body.MAX_CHARACTERS)
        self.assertEqual(binding['workbench']['aarch64']['qualification_scope'], 'artifact-only')
        with patch.object(groups, 'api') as api:
            groups.verify_live(self.fixture.results['x86_64'])
            api.assert_not_called()

    def test_public_downloads_consume_full_arm_and_grouped_evidence(self):
        import test_workbench_downloads
        reader = test_workbench_downloads.DownloadTests()
        grouped = self.fixture.artifact_only()
        grouped['x86_64'] = self.aggregate()
        for arch in ('x86_64', 'aarch64'):
            for download in (False, True):
                with self.subTest(arch=arch, download=download):
                    expected = reader.select(arch=arch, download=download, workbench=self.fixture.results)
                    observed = reader.select(arch=arch, download=download, workbench=grouped)
                    self.assertEqual(observed[0], 0)
                    self.assertEqual(observed, expected)

    def test_registry_publisher_checks_grouped_attempts_before_transport(self):
        import os
        import workbench_registry
        grouped = self.fixture.artifact_only()
        grouped['x86_64'] = self.aggregate()
        with patch.object(groups, 'api', return_value=self.run), patch.object(groups, 'pages', return_value=self.jobs):
            fixtures.binder.bind(self.fixture.root, self.plan, self.fixture.validation, grouped)
        for stale in (False, True):
            jobs = copy.deepcopy(self.jobs)
            if stale:
                jobs.append(dict(jobs[0], id=501, run_attempt=3))
            with self.subTest(stale=stale), patch.object(groups, 'api', return_value=dict(self.run, run_attempt=3)), \
                 patch.object(groups, 'pages', return_value=jobs), \
                 patch.object(workbench_registry.workbench_assets, 'validate',
                    side_effect=lambda assets, version, arch, revision, **kwargs: self.fixture.receipts[arch]), \
                 patch.dict(os.environ, GH_TOKEN='unit-test-only', GITHUB_ACTOR='unit-test'), \
                 patch.object(workbench_registry, 'inspect_raw', side_effect=RuntimeError('mock registry transport')) as transport:
                if stale:
                    with self.assertRaisesRegex(ValueError, 'producer rerun'):
                        workbench_registry.publish(self.fixture.root, self.fixture.version, self.fixture.sha)
                    transport.assert_not_called()
                else:
                    with self.assertRaisesRegex(RuntimeError, 'mock registry transport'):
                        workbench_registry.publish(self.fixture.root, self.fixture.version, self.fixture.sha)
                    transport.assert_called_once()

    def test_registry_validation_only_consumes_groups_without_transport_or_writes(self):
        import workbench_registry
        grouped = self.fixture.artifact_only()
        grouped['x86_64'] = self.aggregate()
        with patch.object(groups, 'api', return_value=self.run), patch.object(groups, 'pages', return_value=self.jobs):
            fixtures.binder.bind(self.fixture.root, self.plan, self.fixture.validation, grouped)
            notes = self.fixture.root / 'release-notes.md'
            before = notes.read_bytes()
            with patch.object(workbench_registry.workbench_assets, 'validate',
                    side_effect=lambda assets, version, arch, revision, **kwargs: self.fixture.receipts[arch]), \
                 patch.object(workbench_registry, 'inspect_raw') as transport, \
                 patch.object(workbench_registry.subprocess, 'run') as command:
                receipts, _, binding, _ = workbench_registry.validate_binding(
                    self.fixture.root, self.fixture.version, self.fixture.sha)
                self.assertEqual(set(receipts), {'x86_64', 'aarch64'})
                self.assertEqual(binding['workbench'], grouped)
                transport.assert_not_called()
                command.assert_not_called()
            self.assertEqual(notes.read_bytes(), before)

    def test_missing_duplicate_cancelled_and_mismatched_group_results(self):
        mutations = [lambda: self.results.pop(), lambda: self.results.append(copy.deepcopy(self.results[0])),
                     lambda: self.results[0].update(conclusion='cancelled'),
                     lambda: self.results[0].update(cases=[]), lambda: self.results[0].update(timings=[]),
                     lambda: self.results[0].update(plan_id='c' * 64), lambda: self.results[0].update(input_assets={}),
                     lambda: self.results[0].update(prepared_cases_sha256='f' * 64),
                     lambda: self.results[0].update(cpus=1), lambda: self.results[0].update(memory_gib=4),
                     lambda: self.results[0].update(cleanup_exit_code=1),
                     lambda: self.results[0].update(offline=False), lambda: self.results[0].update(empty_private_daemon=False),
                     lambda: self.results[0].update(preflight={}), lambda: self.results[0].update(isolation={}),
                     lambda: self.results[0].update(qualification_scope='system'),
                     lambda: self.results[1].update(instance_id=self.results[0]['instance_id']),
                     lambda: self.results[1].update(container_id=self.results[0]['container_id']),
                     lambda: self.jobs[1].update(runner_name=self.jobs[0]['runner_name']),
                     lambda: self.jobs[0].update(conclusion='failure'), lambda: self.jobs[0].update(conclusion='cancelled'),
                     lambda: self.available[0].update(expired=True), lambda: self.available[0].update(name='wrong'),
                     lambda: self.available.append(copy.deepcopy(self.available[0]))]
        original = copy.deepcopy((self.results, self.jobs, self.available))
        for index, mutate in enumerate(mutations):
            self.results, self.jobs, self.available = copy.deepcopy(original)
            mutate()
            with self.subTest(mutation=index), self.assertRaises(ValueError):
                self.aggregate()

    def test_producer_input_and_validator_reruns_invalidate_old_validation(self):
        record = self.aggregate()
        for index in range(len(self.jobs)):
            for conclusion in ('success', 'failure', 'cancelled'):
                jobs = copy.deepcopy(self.jobs)
                jobs.append(dict(jobs[index], id=500 + index, run_attempt=3, conclusion=conclusion))
                with self.subTest(job=index, conclusion=conclusion), self.assertRaises(ValueError):
                    groups.verify_current(record, dict(self.run, run_attempt=3), jobs)
        # Re-running A and validation can legally retain unchanged successful B.
        self.jobs.append(dict(self.jobs[0], id=31, run_attempt=2))
        self.available.append(dict(self.available[0], id=31, name=groups.artifact_name(123, 2, 'A')))
        current = self.aggregate()
        self.assertEqual([row['producer']['run_attempt'] for row in current['runtime_groups']], [2, 1])
        groups.verify_current(current, self.run, self.jobs)
        groups.recovery_order(self.jobs)
        self.jobs.append(dict(self.jobs[1], id=32, run_attempt=3, completed_at='2026-10-14T03:00:00Z'))
        with self.assertRaises(ValueError):
            groups.recovery_order(self.jobs)

    def test_publication_cannot_use_stale_or_foreign_run(self):
        record = self.aggregate()
        for key, value in [('id', 456), ('head_sha', 'f' * 40), ('event', 'pull_request'), ('path', '.github/workflows/other.yml')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                groups.verify_current(record, dict(self.run, **{key:value}), self.jobs)
        bad = copy.deepcopy(record)
        bad['authorization']['head_sha'] = 'e' * 40
        with self.assertRaises(ValueError):
            groups.check_record(self.fixture.version, self.fixture.sha, bad, arch='x86_64',
                                expected_assets=record['input_assets'], case_files=self.plan['case_files'])

    def test_grouped_evidence_is_not_relabelled_as_legacy_full_or_arm(self):
        record = self.aggregate()
        for scope in ('system', 'native-full', 'artifact-only', ''):
            combined = copy.deepcopy(self.fixture.results)
            combined['x86_64'] = dict(record, qualification_scope=scope)
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                artifacts.check_workbench_results(self.fixture.version, self.fixture.sha, combined,
                    expected_assets=record['input_assets'], case_files=self.plan['case_files'])
        with self.assertRaises(ValueError):
            groups.check_record(self.fixture.version, self.fixture.sha, record, arch='aarch64',
                                expected_assets=record['input_assets'], case_files=self.plan['case_files'])


if __name__ == '__main__':
    unittest.main()
