"""Selection transactions freeze complete prose before any release dispatch."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'release'))
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


if __name__ == '__main__':
    unittest.main()
