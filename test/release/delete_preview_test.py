import base64
import copy
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'release'))
import delete_preview as subject

TAG = 'release-v1.2.3-preview.20260901.1'
SHA = 'a' * 40


class DeletePreviewTests(unittest.TestCase):
    def setUp(self):
        units = {unit: (unit + '-' if unit in ('runtime', 'vmlinux') else '') + 'v1.2.3'
                 for unit in subject.coordinator.selection.UNITS}
        self.snapshot = subject.gc.CanonicalSnapshot(SHA, units, 'workbench-v1')
        names = subject.coordinator.platform_asset_names_for_components(TAG, units, delivery='workbench-v1')
        self.release = dict(id=11, tag_name=TAG, target_commitish=SHA, draft=False, prerelease=True,
                            body='Human release notes.', assets=[dict(id=i, name=name, size=100,
                            digest='sha256:' + 'b' * 64, state='uploaded') for i, name in enumerate(sorted(names), 1)])
        self.ref = {'object': {'sha': SHA, 'type': 'commit'}}
        self.calls, self.failures = [], []
        def api(endpoint):
            if '/git/ref/tags/' in endpoint:
                return copy.deepcopy(self.ref)
            if endpoint.endswith('/releases/11'):
                return copy.deepcopy(self.release)
            self.fail('unexpected API read: ' + endpoint)
        def listing(endpoint):
            self.assertTrue(endpoint.endswith('/releases?per_page=100'))
            return [copy.deepcopy(self.release)] if self.release is not None else []
        def delete(*args, **kwargs):
            self.assertEqual(args[:3], ('api', '--method', 'DELETE'))
            endpoint = args[3]
            self.calls.append(endpoint)
            error = self.failures.pop(0) if self.failures else None
            if error and '(HTTP 404)' not in error:
                return subprocess.CompletedProcess(args, 1, '', error)
            if '/releases/11' in endpoint:
                self.release = None
            elif '/git/refs/tags/' in endpoint:
                self.ref = None
            else:
                self.fail('unexpected mutation: ' + endpoint)
            return subprocess.CompletedProcess(args, 1 if error else 0, '', error or '')
        def registry(*args, **kwargs):
            self.calls.append('registry')
            return {'deleted': [1], 'retained_shared': []}
        self.mocks = {}
        for owner, name, options in [
                (subject.coordinator, 'api_optional', dict(side_effect=api)),
                (subject.coordinator, 'paginated', dict(side_effect=listing)),
                (subject.coordinator, 'gh', dict(side_effect=delete)),
                (subject.coordinator, 'aggregate_runs', dict(return_value=[])),
                (subject, 'read_snapshot', dict(return_value=self.snapshot)),
                (subject, 'canonical_gc_snapshot', dict(return_value=self.snapshot)),
                (subject, 'admitted_tagless_snapshot', dict(return_value=self.snapshot)),
                (subject.workbench_gc, 'collect', dict(side_effect=registry))]:
            patcher = patch.object(owner, name, **options)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def test_complete_new_or_historical_release_is_not_incomplete(self):
        for historical in (False, True):
            if historical: self.release['assets'].append({'name': 'SHA256SUMS', 'digest': None})
            with self.subTest(historical=historical), self.assertRaisesRegex(ValueError, 'complete aggregate'):
                subject.collect(TAG, SHA, 'incomplete')
            self.assertFalse(self.calls)

    def test_registry_guard_precedes_release_and_tag_deletion(self):
        self.assertEqual(subject.collect(TAG, SHA, 'gc')['deleted'], ['release', 'tag'])
        self.assertEqual(self.calls[0], 'registry')
        self.assertTrue(self.calls[1].endswith('/releases/11'))
        self.assertIn('/git/refs/tags/', self.calls[2])

    def test_node_infra_and_workbench_registry_checks_both_precede_github_deletion(self):
        snapshot = subject.gc.CanonicalSnapshot(SHA, self.snapshot.components, 'node-infra-v1')
        self.mocks['canonical_gc_snapshot'].return_value = snapshot
        result = subject.collect(TAG, SHA, 'gc')
        self.assertEqual(self.calls[:2], ['registry', 'registry'])
        self.assertEqual(set(result['registry']), {'workbench', 'node-infra'})
        self.assertEqual(self.mocks['collect'].call_args_list[-1].kwargs, {'image': 'node-infra'})

    def test_node_infra_registry_failure_retains_release_and_tag(self):
        snapshot = subject.gc.CanonicalSnapshot(SHA, self.snapshot.components, 'node-infra-v1')
        self.mocks['canonical_gc_snapshot'].return_value = snapshot
        self.mocks['collect'].side_effect = [{'deleted': [], 'retained_shared': []}, ValueError('node-infra uncertain')]
        with self.assertRaisesRegex(ValueError, 'node-infra uncertain'):
            subject.collect(TAG, SHA, 'gc')
        self.assertIsNotNone(self.release)
        self.assertIsNotNone(self.ref)
        self.assertFalse(self.calls)

    def test_registry_failure_retains_github_objects(self):
        self.mocks['collect'].side_effect = ValueError('uncertain registry ownership')
        with self.assertRaisesRegex(ValueError, 'uncertain registry'):
            subject.collect(TAG, SHA, 'gc')
        self.assertFalse(self.calls)

    def test_tagless_gc_uses_canonical_source_without_current_head_requirement(self):
        self.ref = None
        self.assertEqual(subject.collect(TAG, SHA, 'gc')['deleted'], ['release'])
        self.mocks['canonical_gc_snapshot'].assert_called_once()
        self.mocks['admitted_tagless_snapshot'].assert_not_called()

    def test_already_absent_objects_succeed_without_source_or_history_lookup(self):
        self.ref = self.release = None
        self.assertEqual(subject.collect(TAG, SHA, 'gc')['status'], 'already absent')
        self.mocks['canonical_gc_snapshot'].assert_not_called()
        self.assertFalse(self.calls)

    def test_tagless_source_mismatch_retains_objects(self):
        self.ref = None
        self.release['target_commitish'] = 'b' * 40
        with self.assertRaisesRegex(ValueError, 'source identity'):
            subject.collect(TAG, SHA, 'gc')
        self.assertFalse(self.calls)

    def test_invalid_native_asset_metadata_is_not_deletion_authority(self):
        for key, value in [('digest', None), ('size', -1), ('id', 0), ('state', None), ('state', 'unknown')]:
            original = self.release['assets'][0][key]
            self.release['assets'][0][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(subject.coordinator.Deferred, 'retain for inspection'):
                subject.collect(TAG, SHA, 'gc')
            self.release['assets'][0][key] = original
        self.assertFalse(self.calls)

    def test_registry_work_does_not_allow_deletion_from_stale_github_state(self):
        original = copy.deepcopy(self.release)
        for change in ('stable', 'renamed', 'moved-tag'):
            self.release = copy.deepcopy(original)
            self.ref['object']['sha'] = SHA
            self.calls.clear()
            def registry(*args):
                self.calls.append('registry')
                if change == 'stable': self.release['prerelease'] = False
                elif change == 'renamed': self.release['tag_name'] = 'different-version'
                else: self.ref['object']['sha'] = 'b' * 40
            self.mocks['collect'].side_effect = registry
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'changed before deletion'):
                subject.collect(TAG, SHA, 'gc')
            self.assertEqual(self.calls, ['registry'])

    def test_download_counts_and_asset_order_do_not_block_gc(self):
        def registry(*args):
            self.calls.append('registry')
            self.release['assets'].reverse()
            self.release['assets'][0]['download_count'] = 10
        self.mocks['collect'].side_effect = registry
        self.assertEqual(subject.collect(TAG, SHA, 'gc')['deleted'], ['release', 'tag'])

    def test_changed_asset_identity_after_registry_work_retains_release(self):
        original = copy.deepcopy(self.release)
        for field, value in [('id', 99), ('name', 'renamed'), ('size', 99),
                             ('digest', 'sha256:'+'f'*64), ('state', 'starter')]:
            self.release = copy.deepcopy(original); self.calls.clear()
            def registry(*args):
                self.calls.append('registry')
                self.release['assets'][0][field] = value
            self.mocks['collect'].side_effect = registry
            with self.assertRaisesRegex(ValueError, 'Release changed'):
                subject.collect(TAG, SHA, 'gc')
            self.assertEqual(self.calls, ['registry'])

    def test_incomplete_new_release_can_be_removed_after_ownership_checks(self):
        self.release['assets'].pop()
        self.assertEqual(subject.collect(TAG, SHA, 'incomplete')['deleted'], ['release', 'tag'])

    def test_active_publication_prevents_deletion(self):
        self.mocks['aggregate_runs'].return_value = [{'status': 'in_progress'}]
        with self.assertRaisesRegex(ValueError, 'still active'): subject.collect(TAG, SHA, 'gc')
        self.assertFalse(self.calls)

    def test_partial_delete_retry_keeps_source_and_converges(self):
        self.failures = [None, 'gh: Forbidden (HTTP 403)']
        with self.assertRaisesRegex(ValueError, 'Forbidden'): subject.collect(TAG, SHA, 'gc')
        self.assertIsNone(self.release)
        self.assertIsNotNone(self.ref)
        self.assertEqual(subject.collect(TAG, SHA, 'gc')['deleted'], ['tag'])

    def test_confirmed_delete_404_is_idempotent(self):
        self.failures = ['gh: Not Found (HTTP 404)', 'gh: Not Found (HTTP 404)']
        self.assertEqual(subject.collect(TAG, SHA, 'gc')['deleted'], ['release', 'tag'])

    def test_ref_read_network_failure_is_not_absence(self):
        self.mocks['api_optional'].side_effect = RuntimeError('network failure')
        with self.assertRaisesRegex(RuntimeError, 'network'): subject.collect(TAG, SHA, 'gc')
        self.assertFalse(self.calls)

    def test_stable_or_changed_tag_is_never_deleted(self):
        self.ref['object']['sha'] = 'b' * 40
        with self.assertRaisesRegex(ValueError, 'tag source'): subject.collect(TAG, SHA, 'gc')
        self.ref['object']['sha'] = SHA
        self.release['prerelease'] = False
        with self.assertRaisesRegex(ValueError, 'non-preview'): subject.collect(TAG, SHA, 'gc')
        self.assertFalse(self.calls)


if __name__ == '__main__':
    unittest.main()
