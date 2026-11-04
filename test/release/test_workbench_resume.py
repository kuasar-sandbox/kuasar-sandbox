"""Recover original accepted bytes; do not query Actions for completed releases."""
import copy
import unittest
from unittest.mock import patch, Mock

import preview_coordinator as subject

VERSION = 'release-v1.2.3-preview.20260929'
SHA = 'a' * 40


class ResumeTests(unittest.TestCase):
    def setUp(self):
        subject._publication_resumes.clear()
        self.run = dict(id=10, display_title=subject.aggregate_run_title(VERSION, SHA),
                        created_at='2026-09-29T01:00:00Z', status='completed', conclusion='failure', run_attempt=99)
        self.jobs = [dict(id=index, name=name, conclusion='success', status='completed')
                     for index, name in enumerate(('prepare', 'stage', 'release-asset-validation / results',
                         'release-asset-validation / workbench-release (x86_64, all)',
                         'release-asset-validation / workbench-release (aarch64, all)'), 1)]
        self.jobs.append(dict(id=20, name='publish', status='completed', conclusion='failure'))
        self.artifacts = [dict(name=name, expired=False) for name in
                          (f'aggregate-stage-{VERSION}-10', 'integration-validation-10',
                           'integration-workbench-result-x86_64-10', 'integration-workbench-result-aarch64-10')]
        self.status = subject.ReleaseStatus(None, None, False)

    def pages(self, endpoint, field):
        return self.jobs if field == 'jobs' else self.artifacts

    def resume(self, runs=None):
        with patch.object(subject, 'aggregate_runs', return_value=runs if runs is not None else [self.run]), \
             patch.object(subject, 'platform_release', return_value=self.status), \
             patch.object(subject, 'active_delete_run', return_value=None), \
             patch.object(subject, 'paginated', side_effect=self.pages), patch.object(subject, 'gh') as gh:
            result = subject.ensure_aggregate(VERSION, SHA)
            return result, gh.call_args_list

    def test_exact_recovery_scan_is_bounded_before_job_queries(self):
        with patch.object(subject, 'paginated', side_effect=AssertionError('unbounded job query')):
            with self.assertRaisesRegex(subject.Deferred, 'too many exact publication runs'):
                subject.resume_publish(subject.PLATFORM_REPOSITORY, 'platform', VERSION, [self.run] * 21)

    def test_no_release_or_tag_still_resumes_original_publish_only(self):
        result, calls = self.resume()
        self.assertFalse(result)
        self.assertEqual([call.args for call in calls], [('api', '--method', 'POST',
            f'repos/{subject.PLATFORM_REPOSITORY}/actions/jobs/20/rerun', '--silent')])

    def test_partial_tagged_and_tagless_release_preserve_original_stage(self):
        for status in (subject.ReleaseStatus(None, SHA, False),
                       subject.ReleaseStatus(dict(target_commitish=SHA), None, False)):
            self.status = status
            self.assertIn('/actions/jobs/20/rerun', self.resume()[1][0].args[-2])

    def test_successful_publisher_with_failed_cleanup_never_rebuilds_aggregate(self):
        self.jobs[-1]['conclusion'] = 'success'
        self.jobs.append(dict(id=21, name='cleanup', status='completed', conclusion='failure'))
        with self.assertRaisesRegex(subject.Pending, 'successful publisher'):
            self.resume()

    def test_second_publish_retry_keeps_prior_successful_prerequisites(self):
        self.run['run_attempt'] = 100
        self.jobs.insert(0, dict(self.jobs[-1], id=25))
        self.assertIn('/actions/jobs/25/rerun', self.resume()[1][0].args[-2])

    def test_every_required_artifact_must_be_unique_and_unexpired(self):
        original = copy.deepcopy(self.artifacts)
        for index in range(len(original)):
            for mutation in ('missing', 'expired', 'duplicate'):
                with self.subTest(artifact=index, mutation=mutation):
                    self.artifacts = copy.deepcopy(original)
                    if mutation == 'missing': self.artifacts.pop(index)
                    elif mutation == 'expired': self.artifacts[index]['expired'] = True
                    else: self.artifacts.append(copy.deepcopy(self.artifacts[index]))
                    with self.assertRaisesRegex(subject.Deferred, 'artifact is missing or expired'):
                        self.resume()

    def test_every_actual_gate_must_succeed(self):
        for job in self.jobs[:-1]:
            with self.subTest(job=job['name']):
                job['conclusion'] = 'failure'
                with self.assertRaisesRegex(subject.Deferred, 'without successful'):
                    self.resume()
                job['conclusion'] = 'success'

    def test_latest_rerun_cannot_hide_a_failed_gate(self):
        self.jobs.append(dict(self.jobs[1], id=99, conclusion='failure'))
        with self.assertRaisesRegex(subject.Deferred, 'successful stage'):
            self.resume()

    def test_successful_release_never_queries_actions(self):
        self.status = subject.ReleaseStatus(dict(tag_name=VERSION), SHA, True)
        with patch.object(subject, 'platform_release', return_value=self.status), \
             patch.object(subject, 'aggregate_runs', side_effect=AssertionError('historical Actions query')), \
             patch.object(subject, 'paginated', side_effect=AssertionError('historical job query')):
            self.assertTrue(subject.ensure_aggregate(VERSION, SHA))

    def test_historical_attempts_do_not_lock_future_invocations(self):
        for _ in range(3): self.assertFalse(self.resume()[0])
        with self.assertRaisesRegex(RuntimeError, 'three attempts in this invocation'):
            self.resume()
        subject._publication_resumes.clear()
        self.assertFalse(self.resume()[0])

    def test_active_publication_is_not_restarted(self):
        self.run['status'] = 'in_progress'
        self.assertEqual(self.resume(), (False, []))

    def test_cleanup_prevents_restore(self):
        with patch.object(subject, 'platform_release', return_value=self.status), \
             patch.object(subject, 'active_delete_run', return_value=dict(id=99)), \
             patch.object(subject, 'aggregate_runs') as runs:
            with self.assertRaisesRegex(subject.Pending, 'cleanup is active'):
                subject.ensure_aggregate(VERSION, SHA)
            runs.assert_not_called()

    def test_newer_cancelled_prepare_does_not_hide_original_stage(self):
        newer = dict(self.run, id=11, created_at='2026-09-29T02:00:00Z', conclusion='cancelled')
        original = self.pages
        with patch.object(self, 'pages', side_effect=lambda endpoint, field:
                          [] if '/runs/11/' in endpoint else original(endpoint, field)):
            self.assertIn('/actions/jobs/20/rerun', self.resume([newer, self.run])[1][0].args[-2])

    def test_multiple_accepted_stages_are_not_chosen_arbitrarily(self):
        newer = dict(self.run, id=11, created_at='2026-09-29T02:00:00Z')
        original = self.pages
        with patch.object(self, 'pages', side_effect=lambda endpoint, field:
                [dict(row, name=row['name'].replace('-10', '-11')) for row in self.artifacts]
                if '/runs/11/' in endpoint and field == 'artifacts' else original(endpoint, field)):
            with self.assertRaisesRegex(subject.Deferred, 'multiple accepted stages'):
                self.resume([newer, self.run])

    def test_foreign_source_partial_is_retained_without_actions_or_cleanup(self):
        self.status = subject.ReleaseStatus(None, 'b' * 40, False)
        with patch.object(subject, 'dispatch_platform_cleanup', side_effect=AssertionError('cleanup')):
            with self.assertRaisesRegex(subject.Deferred, 'another or ambiguous source'):
                self.resume()

    def test_no_accepted_stage_does_not_delete_partial_bytes(self):
        self.status = subject.ReleaseStatus(None, SHA, False)
        self.jobs[-1]['conclusion'] = 'skipped'
        with self.assertRaisesRegex(subject.Deferred, 'no recoverable stage'):
            self.resume()

    def component(self, unit_name='sandboxer'):
        unit = subject.UNIT_BY_NAME[unit_name]
        tag = ('runtime-' if unit_name == 'runtime' else 'vmlinux-' if unit_name == 'vmlinux' else '') + 'v1.2.3-preview.20260929'
        plan = subject.Plan(unit, tag, 'main', SHA, tag, tag, 'publish')
        plans = {unit.name: plan}
        for name in unit.dependencies:
            plans[name] = subject.Plan(subject.UNIT_BY_NAME[name], 'v1.0.0', 'main', 'b' * 40,
                                       'v1.0.0', 'v1.0.0', 'reuse')
        self.run['display_title'] = subject.release_run_title(plan, plans)
        self.jobs = [dict(id=index, name=name, status='completed', conclusion='success')
                     for index, name in enumerate(('preflight', 'build (x86_64, ubuntu-24.04)',
                                                    'build (aarch64, ubuntu-24.04-arm)'), 1)]
        self.jobs.append(dict(id=20, name='publish', status='completed', conclusion='failure'))
        prefix = unit_name + '-release' if unit_name in {'runtime', 'vmlinux'} else 'component-release'
        self.artifacts = [dict(name=artifact, expired=False) for artifact in ('workbench-selection-10',
                          f'{prefix}-{tag}-x86_64-10', f'{prefix}-{tag}-aarch64-10')]
        return plan, plans

    def resume_component(self, plan, plans, status):
        state = Mock()
        state.status.return_value = status
        with patch.object(subject, 'RepositoryState', return_value=state), \
             patch.object(subject, 'active_release_run', return_value=None), \
             patch.object(subject, 'active_delete_run', return_value=None), \
             patch.object(subject, 'release_runs', return_value=[self.run]), \
             patch.object(subject, 'paginated', side_effect=self.pages), patch.object(subject, 'gh') as gh:
            result = subject.ensure_unit(plan, plans, VERSION, 'c' * 40)
            return result, gh.call_args_list

    def test_component_stage_recovers_both_before_and_after_release_creation(self):
        plan, plans = self.component()
        for status in (subject.ReleaseStatus(None, None, False), subject.ReleaseStatus(None, SHA, False)):
            self.assertIn('/actions/jobs/20/rerun', self.resume_component(plan, plans, status)[1][0].args[-2])

    def test_runtime_and_vmlinux_recover_their_actual_workflow_artifact_names(self):
        for name in ('runtime', 'vmlinux'):
            subject._publication_resumes.clear()
            plan, plans = self.component(name)
            with self.subTest(unit=name):
                self.assertIn('/actions/jobs/20/rerun', self.resume_component(
                    plan, plans, subject.ReleaseStatus(None, SHA, False))[1][0].args[-2])
                self.artifacts[1]['name'] = self.artifacts[1]['name'].replace(name + '-release', 'component-release')
                with self.assertRaisesRegex(subject.Deferred, 'artifact is missing'):
                    self.resume_component(plan, plans, subject.ReleaseStatus(None, SHA, False))

    def test_successful_component_publisher_with_failed_cleanup_never_rebuilds(self):
        plan, plans = self.component()
        self.jobs[-1]['conclusion'] = 'success'
        self.jobs.append(dict(id=21, name='cleanup', status='completed', conclusion='failure'))
        with self.assertRaisesRegex(subject.Pending, 'successful publisher'):
            self.resume_component(plan, plans, subject.ReleaseStatus(None, None, False))

    def test_component_selection_and_both_archives_are_required(self):
        plan, plans = self.component()
        original = copy.deepcopy(self.artifacts)
        for index in range(len(original)):
            self.artifacts = original[:index] + original[index + 1:]
            with self.assertRaisesRegex(subject.Deferred, 'artifact is missing or expired'):
                self.resume_component(plan, plans, subject.ReleaseStatus(None, None, False))

    def test_component_dependency_conflict_never_reuses_or_deletes_stage(self):
        plan, plans = self.component()
        self.run['display_title'] += ',another-dependency'
        with self.assertRaisesRegex(subject.Deferred, 'no recoverable stage'):
            self.resume_component(plan, plans, subject.ReleaseStatus(None, SHA, False))

    def test_configured_partial_reaches_restore_without_cleanup_or_new_selection(self):
        plan, plans = self.component()
        state = Mock()
        state.status.return_value = subject.ReleaseStatus(None, SHA, False)
        with patch.object(subject, 'RepositoryState', return_value=state), \
             patch.object(subject, 'active_release_run', return_value=None), \
             patch.object(subject, 'active_delete_run', return_value=None), \
             patch.object(subject, 'branch_sha', return_value='b' * 40), \
             patch.object(subject, 'clone_repository', side_effect=AssertionError('new selection')), \
             patch.object(subject, 'dispatch_cleanup', side_effect=AssertionError('cleanup')):
            actual = subject.make_plan(plan.unit, plan.selected, '20260929', None)
            self.assertEqual((actual.selected, actual.source_sha, actual.action), (plan.selected, SHA, 'publish'))

    def test_partial_component_keeps_all_configured_dependencies_before_reselection(self):
        plan, plans = self.component()
        configured = {name: item.selected for name, item in plans.items()}
        statuses = {name: subject.ReleaseStatus(None, item.source_sha, name != plan.unit.name)
                    for name, item in plans.items()}
        def repository(unit):
            state = Mock()
            state.status.return_value = statuses[unit.name]
            return state
        calls = []
        def ensure(item, original, aggregate, aggregate_sha):
            calls.append(item.unit.name)
            self.assertEqual({name: candidate.selected for name, candidate in original.items()}, configured)
            return item.unit.name != plan.unit.name
        with patch.object(subject, 'RepositoryState', side_effect=repository), \
             patch.object(subject, 'ensure_unit', side_effect=ensure), \
             patch.object(subject, 'branch_sha', side_effect=AssertionError('advanced dependency main')):
            with self.assertRaisesRegex(subject.Pending, 'before selecting new component versions'):
                subject.recover_configured_components(configured, VERSION, 'f' * 40)
        self.assertEqual(calls, ['accelerator', 'connector', 'sandboxer'])

    def test_missing_configured_dependency_preserves_partial_component_and_channel(self):
        plan, plans = self.component()
        def repository(unit):
            state = Mock()
            state.status.return_value = subject.ReleaseStatus(None, SHA if unit.name == 'sandboxer' else None, False)
            return state
        with patch.object(subject, 'RepositoryState', side_effect=repository), patch.object(subject, 'ensure_unit') as ensure:
            with self.assertRaisesRegex(subject.Deferred, 'retain the channel'):
                subject.recover_configured_components({name: item.selected for name, item in plans.items()}, VERSION, SHA)
            ensure.assert_not_called()

    def test_dependency_rebuild_partial_keeps_matching_stage_or_defers_conflict(self):
        plan, plans = self.component()
        state = Mock()
        with patch.object(subject.preview_selection, 'preview_candidate', return_value=plan.selected), \
             patch.object(subject, 'dispatch_cleanup', side_effect=AssertionError('cleanup')):
            state.status.return_value = subject.ReleaseStatus(None, SHA, False)
            self.assertEqual(subject.force_dependency_preview(plan, '20260929', state).action, 'publish')
            state.status.return_value = subject.ReleaseStatus(None, 'b' * 40, False)
            with self.assertRaisesRegex(subject.Deferred, 'conflicts'):
                subject.force_dependency_preview(plan, '20260929', state)


if __name__ == '__main__':
    unittest.main()
