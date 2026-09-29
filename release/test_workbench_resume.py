"""Publication retry preserves a successful stage and never reruns validation."""
import copy
import unittest
from unittest.mock import patch

import preview_coordinator as subject
import selection

VERSION = 'release-v1.2.3-preview.20260929'
SHA = 'a' * 40


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.run = {'id': 10, 'display_title': subject.aggregate_run_title(VERSION, SHA),
                    'created_at': '2026-09-29T01:00:00Z', 'status': 'completed', 'conclusion': 'failure', 'run_attempt': 1}
        self.jobs = [{'id': index, 'name': name, 'conclusion': 'success', 'status': 'completed'}
                     for index, name in enumerate(('prepare', 'stage', 'release-asset-validation / results'), 1)]
        self.jobs.append({'id': 20, 'name': 'publish', 'status': 'completed', 'conclusion': 'failure'})
        self.artifacts = [{'name': f'aggregate-stage-{VERSION}-10', 'expired': False}]

    def resume(self):
        with patch.object(subject, 'aggregate_runs', return_value=[self.run]), patch.object(
                subject, 'paginated', side_effect=lambda endpoint, field: self.jobs if field == 'jobs' else self.artifacts), patch.object(subject, 'gh') as gh:
            result = subject.resume_workbench_publish(VERSION, SHA)
            return result, gh.call_args_list

    def test_only_failed_publication_is_retried(self):
        result, calls = self.resume()
        self.assertFalse(result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args, ('api', '--method', 'POST',
            f'repos/{subject.PLATFORM_REPOSITORY}/actions/jobs/20/rerun', '--silent'))

    def test_second_publish_retry_keeps_prior_successful_prerequisites(self):
        self.run['run_attempt'] = 2
        self.jobs.insert(0, dict(self.jobs[-1], id=25))
        result, calls = self.resume()
        self.assertFalse(result)
        self.assertIn('/actions/jobs/25/rerun', calls[0].args[-2])
        self.jobs[0]['conclusion'] = 'success'
        with self.assertRaisesRegex(RuntimeError, 'no failed publish job'):
            self.resume()

    def test_expired_or_ambiguous_stage_and_failed_gate_prevent_resume(self):
        original = copy.deepcopy(self.artifacts)
        for artifacts in ([], [dict(original[0], expired=True)], original * 2):
            self.artifacts = artifacts
            with self.assertRaisesRegex(RuntimeError, 'original workbench stage is unavailable'):
                self.resume()
        self.artifacts = original
        self.jobs[2]['conclusion'] = 'failure'
        with self.assertRaisesRegex(RuntimeError, 'successful original prerequisite'):
            self.resume()

    def test_successful_publisher_is_never_rerun_and_budget_is_bounded(self):
        self.jobs[-1]['conclusion'] = 'success'
        with self.assertRaisesRegex(RuntimeError, 'no failed publish job'):
            self.resume()
        self.run['run_attempt'] = 3
        with self.assertRaisesRegex(RuntimeError, 'three attempts'):
            self.resume()

    def test_active_publication_is_not_restarted(self):
        self.run['status'] = 'in_progress'
        self.assertEqual(self.resume(), (False, []))

    def test_partial_new_contract_enters_resume_before_preview_cleanup(self):
        partial = subject.ReleaseStatus(None, SHA, False)
        with patch.object(subject, 'platform_release', return_value=partial), patch.object(
                subject, 'platform_asset_names', return_value={selection.workbench_archive(VERSION, 'x86_64')}), patch.object(
                subject, 'resume_workbench_publish', return_value=False) as resume, patch.object(subject, 'dispatch_platform_cleanup') as cleanup:
            self.assertFalse(subject.ensure_aggregate(VERSION, SHA))
            resume.assert_called_once_with(VERSION, SHA)
            cleanup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
