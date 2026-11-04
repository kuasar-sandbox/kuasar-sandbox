"""Selection transactions freeze complete prose before any release dispatch."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'release'))
import formal_coordinator as formal
import prepare_notes
import preview_coordinator as coordinator
import selection

SHA = 'a' * 40
NEXT = 'b' * 40
VERSION = 'release-v1.2.3'


def manifest(channel='release', notes=True):
    value = 'version: ' + VERSION + '\n'
    if channel == 'daily-preview':
        value += 'preview_version: preview.20990101\n'
    value += 'components:\n' + ''.join(f'  {unit}: {unit + "-" if unit in ("runtime", "vmlinux") else ""}v1.2.3\n'
                                     for unit in selection.UNITS)
    return value + ('notes:\n  mode: fixed\n  body: |-\n    Human notes.  \n' if notes else '')


def remote(text):
    return {'content': base64.b64encode(text.encode()).decode(), 'sha': 'blob'}


class SelectionTransactionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'releases').mkdir()

    def persist(self, channel, text):
        return coordinator.persist_manifest(text, VERSION + ('-preview.20990101' if channel == 'daily-preview' else ''),
            channel=channel, root=self.root, source_ref='main', source_sha=SHA)

    def test_both_channels_generate_and_persist_full_body_before_freeze(self):
        for channel in ('release', 'daily-preview'):
            current = manifest(channel, notes=False)
            path = self.root / f'releases/{channel}.yaml'
            path.write_text(current)
            def write(relative, prepared, aggregate, ref, sha):
                self.assertEqual((relative, ref, sha), (f'releases/{channel}.yaml', 'main', SHA))
                fields = selection.read_simple_yaml(prepared, relative)['notes']
                self.assertEqual(fields['mode'], 'auto')
                self.assertIn(f'[`releases/{channel}.yaml`]', fields['body'])
                self.assertIn(f'/blob/{aggregate}/releases/{channel}.yaml', fields['body'])
                self.assertIn('verify GitHub asset sizes and SHA-256 digests', fields['body'])
                self.assertNotIn('selection_sha256', fields)
                self.assertNotIn('one tested platform combination', fields['body'])
                prepare_notes.require_prepared(prepared, relative)
                self.assertEqual(path.read_text(), current)
                return NEXT
            with patch.object(coordinator, 'branch_sha', side_effect=[SHA, NEXT]), \
                 patch.object(coordinator, 'api', return_value=remote(current)), \
                 patch.object(coordinator, 'commit_manifest', side_effect=write) as commit:
                self.assertEqual(self.persist(channel, current), NEXT)
                commit.assert_called_once()
            self.assertIn('mode: auto', path.read_text())

    def test_generation_failure_never_writes_local_or_remote_manifest(self):
        current = manifest(notes=False)
        path = self.root / 'releases/release.yaml'
        path.write_text(current)
        with patch.object(coordinator, 'branch_sha', return_value=SHA), \
             patch.object(coordinator, 'api', return_value=remote(current)), \
             patch.object(prepare_notes, 'render', side_effect=ValueError('generation failed')), \
             patch.object(coordinator, 'commit_manifest') as commit:
            with self.assertRaisesRegex(ValueError, 'generation failed'):
                self.persist('release', current)
            commit.assert_not_called()
        self.assertEqual(path.read_text(), current)

    def test_lost_commit_reply_stops_without_retry_and_fresh_rescan_deduplicates(self):
        for applied in (False, True):
            current = manifest()
            selected = current.replace('release-v1.2.3', 'release-v1.2.4')
            path = self.root / 'releases/release.yaml'
            path.write_text(current)
            with patch.object(coordinator, 'branch_sha', return_value=SHA), \
                 patch.object(coordinator, 'api', return_value=remote(current)), \
                 patch.object(coordinator, 'commit_manifest', side_effect=RuntimeError('lost response')) as write:
                with self.assertRaisesRegex(RuntimeError, 'lost response'):
                    self.persist('release', selected)
                write.assert_called_once()
            self.assertEqual(path.read_text(), current)
            # A new invocation must obtain the remote head and checkout anew.
            head, fresh = (NEXT, selected) if applied else (SHA, current)
            path.write_text(fresh)
            with patch.object(coordinator, 'branch_sha', side_effect=[head, NEXT]), \
                 patch.object(coordinator, 'api', return_value=remote(fresh)), \
                 patch.object(coordinator, 'commit_manifest', return_value=NEXT) as write:
                value = coordinator.persist_manifest(selected, 'release-v1.2.4', channel='release',
                    root=self.root, source_ref='main', source_sha=head)
                self.assertEqual(value, NEXT)
                self.assertEqual(write.call_count, 0 if applied else 1)

    def test_concurrent_manual_edit_and_unrelated_branch_motion_stop_dispatch(self):
        current = manifest()
        path = self.root / 'releases/release.yaml'
        path.write_text(current)
        for heads, content, desired in (([NEXT], current, current),
                ([SHA], current.replace('Human', 'Concurrent'), current),
                ([SHA, NEXT], current, current),
                ([SHA, 'c' * 40], current, current.replace('release-v1.2.3', 'release-v1.2.4'))):
            with patch.object(coordinator, 'branch_sha', side_effect=heads), \
                 patch.object(coordinator, 'api', return_value=remote(content)), \
                 patch.object(coordinator, 'commit_manifest', return_value=NEXT):
                with self.assertRaises(coordinator.Pending):
                    self.persist('release', desired)
            self.assertEqual(path.read_text(), current)

    def test_preparation_requires_body_availability_even_without_source_helper(self):
        self.assertFalse((self.root / 'release/prepare_notes.py').exists())
        for channel in ('release', 'daily-preview'):
            with self.assertRaisesRegex(ValueError, 'body|text'):
                prepare_notes.require_prepared(manifest(channel, notes=False), f'releases/{channel}.yaml')


class StableRetryTests(unittest.TestCase):
    def test_completed_release_is_noop_regardless_of_obsolete_notes_metadata(self):
        original = manifest()
        correction = original.replace('  mode: fixed\n', '  mode: fixed\n  aggregate_sha: ' + SHA + '\n')
        status = coordinator.ReleaseStatus({}, SHA, True)
        with patch.object(coordinator, 'platform_release', return_value=status), \
             patch.object(coordinator, 'api', return_value=remote(original)), \
             patch.object(coordinator, 'persist_manifest') as persist:
            self.assertEqual(formal.existing_publication(VERSION, correction, NEXT), formal.PublicationState.COMPLETE)
            persist.assert_not_called()
            with self.assertRaisesRegex(coordinator.Deferred, 'another selection'):
                formal.existing_publication(VERSION, correction.replace('sandboxer: v1.2.3', 'sandboxer: v1.2.4'), NEXT)

    def test_active_or_partial_release_cannot_rewrite_source(self):
        absent = coordinator.ReleaseStatus(None, None, False)
        partial = coordinator.ReleaseStatus({'draft': True}, SHA, False)
        for status, active in ((absent, {'html_url': 'run-url'}), (partial, None)):
            with patch.object(coordinator, 'platform_release', return_value=status), \
                 patch.object(coordinator, 'active_aggregate_run', return_value=active), \
                 patch.object(coordinator, 'persist_manifest') as persist:
                with self.assertRaises(coordinator.Deferred):
                    formal.existing_publication(VERSION, manifest(), NEXT)
                persist.assert_not_called()

    def test_failed_prepare_or_validation_does_not_lock_unpublished_version(self):
        absent = coordinator.ReleaseStatus(None, None, False)
        for conclusion in ('failure', 'cancelled'):
            history = [{'id': 12, 'display_title': f'Aggregate {VERSION} @{SHA}', 'conclusion': conclusion}]
            with patch.object(coordinator, 'platform_release', return_value=absent), \
                 patch.object(coordinator, 'active_aggregate_run', return_value=None), \
                 patch.object(coordinator, 'aggregate_runs', return_value=history), \
                 patch.object(formal, 'validated_stage', return_value=False):
                self.assertEqual(formal.existing_publication(VERSION, manifest(notes=False), NEXT), formal.PublicationState.NEW)

    def test_retained_stage_requires_latest_successful_gates_and_unexpired_artifact(self):
        state = {'id': 12}
        jobs = [{'id': i, 'name': name, 'status': 'completed', 'conclusion': 'success'}
                for i, name in enumerate(('prepare', 'stage', 'release-asset-validation / results'), 1)]
        artifact = {'name': f'aggregate-stage-{VERSION}-12', 'expired': False}
        for changed_jobs, artifacts, expected in ((jobs, [artifact], True),
                (jobs + [dict(jobs[-1], id=20, conclusion='failure')], [artifact], False),
                (jobs[:-1], [artifact], False), (jobs, [dict(artifact, expired=True)], False),
                (jobs, [], False)):
            def pages(endpoint, key):
                return changed_jobs if key == 'jobs' else artifacts
            with patch.object(coordinator, 'paginated', side_effect=pages):
                self.assertEqual(formal.validated_stage(state, VERSION), expected)

    def test_same_source_validated_run_resumes_original_publisher_before_preparation(self):
        for text in (manifest(notes=False), manifest().replace('  mode: fixed\n',
                '  mode: fixed\n  aggregate_sha: ' + SHA + '\n')):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'releases').mkdir()
                (root / 'releases/release.yaml').write_text(text)
                (root / 'releases/daily-preview.yaml').write_text(manifest('daily-preview'))
                with patch.object(formal, 'ROOT', root), \
                     patch.dict(formal.os.environ, dict(RELEASE_VERSION=VERSION, PLATFORM_REF='main', PLATFORM_SHA=SHA)), \
                     patch.object(coordinator, 'run', side_effect=[Mock(stdout=SHA), Mock(stdout=text)]), \
                     patch.object(coordinator, 'branch_sha', return_value=SHA), \
                     patch.object(coordinator, 'platform_release', return_value=coordinator.ReleaseStatus(None, None, False)), \
                     patch.object(coordinator, 'active_aggregate_run', return_value=None), \
                     patch.object(coordinator, 'aggregate_runs', return_value=[{'display_title': f'Aggregate {VERSION} @{SHA}'}]), \
                     patch.object(formal, 'validated_stage', return_value=True), \
                     patch.object(coordinator, 'persist_manifest') as persist, \
                     patch.object(coordinator, 'RepositoryState') as state, \
                     patch.object(coordinator, 'has_arm_archive', return_value=True), \
                     patch.object(formal, 'wait_for_convergence') as wait:
                    state.return_value.status.return_value = coordinator.ReleaseStatus({}, SHA, True)
                    formal.main()
                    persist.assert_not_called()
                    wait.assert_called_once()
                    self.assertEqual(wait.call_args.args[1:], (VERSION, SHA))

    def test_mixed_retained_sources_reject_resume_in_either_history_order(self):
        absent = coordinator.ReleaseStatus(None, None, False)
        for sources in ((SHA, NEXT), (NEXT, SHA)):
            history = [{'display_title': f'Aggregate {VERSION} @{source}'} for source in sources]
            with self.subTest(sources=sources), \
                 patch.object(coordinator, 'platform_release', return_value=absent), \
                 patch.object(coordinator, 'active_aggregate_run', return_value=None), \
                 patch.object(coordinator, 'aggregate_runs', return_value=history), \
                 patch.object(formal, 'validated_stage', return_value=True):
                with self.assertRaisesRegex(coordinator.Deferred, 'earlier validated source'):
                    formal.existing_publication(VERSION, manifest(notes=False), SHA)

    def test_failed_original_stage_without_release_is_not_replaced(self):
        absent = coordinator.ReleaseStatus(None, None, False)
        history = [{'display_title': f'Aggregate {VERSION} @{SHA}'}]
        with patch.object(coordinator, 'platform_release', return_value=absent), \
             patch.object(coordinator, 'active_aggregate_run', return_value=None), \
             patch.object(coordinator, 'aggregate_runs', return_value=history), \
             patch.object(formal, 'validated_stage', return_value=True):
            with self.assertRaisesRegex(coordinator.Deferred, 'earlier validated source'):
                formal.existing_publication(VERSION, manifest(), NEXT)
            for text in (manifest(), manifest(notes=False)):
                self.assertEqual(formal.existing_publication(VERSION, text, SHA), formal.PublicationState.RESUME)


if __name__ == '__main__':
    unittest.main()
