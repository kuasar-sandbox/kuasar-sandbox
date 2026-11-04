"""Pre-freeze generation preserves authored prose and never invents evidence."""
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'release'))
import prepare_notes as prepare
import release_notes
import selection


class PrepareNotesTests(unittest.TestCase):
    def setUp(self):
        self.relative = 'releases/daily-preview.yaml'
        self.root = prepare.ROOT
        self.units = {unit: (unit + '-' if unit in ('runtime', 'vmlinux') else '') + 'v1.2.3'
                      for unit in selection.UNITS}
        self.selected = ('version: release-v2.0.0\nprevious_version: release-v1.0.0\n'
                         'preview_version: preview.20990101\ndelivery: workbench-v1\ncomponents:\n' +
                         ''.join(f'  {unit}: {tag}\n' for unit, tag in self.units.items()))
        self.generated = '# Kuasar Sandbox release-v2.0.0-preview.20990101\n\n## Highlights\n\nGenerated.\n'

    def with_notes(self, text=None, mode='auto', digest=False, correction=False):
        text = self.selected if text is None else text
        block = 'notes: # keep this comment\n  mode: ' + mode + '\n'
        if digest:
            block += '  selection_sha256: ' + ('a' * 64 if digest is True else digest) + '\n'
        if correction:
            block += '  aggregate_sha: ' + 'a' * 40 + '\n'
        return text + block + '  body: |- # preserve formatting\n    Human edit.  \n\n    Second paragraph.\n'

    def fields(self, text):
        return selection.read_simple_yaml(text, self.relative)['notes']

    def changes(self):
        return [self.selected.replace('release-v2.0.0', 'release-v2.0.1'),
                self.selected.replace('preview.20990101', 'preview.20990102'),
                self.selected.replace('sandboxer: v1.2.3', 'sandboxer: v1.2.4'),
                self.selected.replace('release-v1.0.0', 'release-v1.0.1'),
                self.selected.replace('delivery: workbench-v1\n', '')]

    def test_same_auto_selection_preserves_hand_edits_and_raw_notes_block(self):
        current = self.with_notes()
        with patch.object(prepare, 'render') as render:
            self.assertEqual(prepare.prepare(current, current, self.relative, self.root), current)
        render.assert_not_called()

    def test_new_auto_tuple_regenerates_each_selection_dimension(self):
        current = self.with_notes()
        for selected in self.changes():
            with self.subTest(selected=selected), patch.object(prepare, 'render', return_value=self.generated) as render:
                result = prepare.prepare(current, selected, self.relative, self.root)
                render.assert_called_once_with(self.root, selected, self.relative, None, source_shas=None)
                self.assertEqual(self.fields(result)['body'], self.generated)
                self.assertNotIn('selection_sha256', self.fields(result))
                self.assertNotIn('selection_sha256:', result)
                prepare.require_prepared(result, self.relative)

    def test_canonical_format_and_nonnotes_order_do_not_regenerate(self):
        current = self.with_notes()
        lines = self.selected.splitlines(keepends=True)
        selected = '# independent comment\n' + ''.join(lines[1:4] + lines[:1] + lines[4:5] + list(reversed(lines[5:])))
        with patch.object(prepare, 'render') as render:
            result = prepare.prepare(current, selected, self.relative, self.root)
        render.assert_not_called()
        self.assertEqual(selection.split_notes(result, self.relative)[1], selection.split_notes(current, self.relative)[1])
        self.assertTrue(result.startswith(selected))

    def test_fixed_preserves_body_for_any_selection_ignoring_legacy_metadata(self):
        for digest in (False, '0' * 64):
            current = self.with_notes(mode='fixed', digest=digest)
            for selected in [self.selected, *self.changes()]:
                with self.subTest(digest=digest, selected=selected), patch.object(prepare, 'render') as render:
                    result = prepare.prepare(current, selected, self.relative, self.root)
                render.assert_not_called()
                self.assertEqual(self.fields(result), self.fields(current))
                prepare.require_prepared(result, self.relative)

    def test_mode_switches_obey_current_mode_and_selection_change(self):
        # Changing the mode alone does not change the selected result.
        fixed_to_auto = self.with_notes(mode='fixed').replace('mode: fixed', 'mode: auto')
        auto_to_fixed = self.with_notes().replace('mode: auto', 'mode: fixed')
        for current in (fixed_to_auto, auto_to_fixed):
            with patch.object(prepare, 'render') as render:
                result = prepare.prepare(current, current, self.relative, self.root)
            render.assert_not_called()
            self.assertEqual(result, current)
        changed = self.changes()[0]
        with patch.object(prepare, 'render', return_value=self.generated) as render:
            prepare.prepare(fixed_to_auto, changed, self.relative, self.root)
        render.assert_called_once()
        with patch.object(prepare, 'render') as render:
            result = prepare.prepare(auto_to_fixed, changed, self.relative, self.root)
        render.assert_not_called()
        self.assertEqual(self.fields(result)['body'], self.fields(auto_to_fixed)['body'])

    def test_auto_legacy_fingerprint_is_ignored_and_removed_without_generation(self):
        for digest in (False, True, 'obsolete-value'):
            current = self.with_notes(digest=digest)
            with patch.object(prepare, 'render') as render:
                result = prepare.prepare(current, current, self.relative, self.root)
            render.assert_not_called()
            self.assertEqual(self.fields(result)['body'], self.fields(current)['body'])
            self.assertNotIn('selection_sha256:', result)

    def test_pending_working_notes_are_used_and_fixed_mode_is_respected(self):
        current = self.with_notes()
        for mode in ('auto', 'fixed'):
            selected = self.with_notes(self.changes()[0], mode=mode).replace('Human edit.', 'Pending manual edit.')
            with patch.object(prepare, 'render', return_value=self.generated) as render:
                result = prepare.prepare(current, selected, self.relative, self.root)
            if mode == 'auto':
                render.assert_called_once()
                self.assertEqual(self.fields(result)['body'], self.generated)
            else:
                render.assert_not_called()
                self.assertIn('Pending manual edit.', self.fields(result)['body'])
        selected = current.replace('Human edit.', 'Pending manual edit.')
        with patch.object(prepare, 'render') as render:
            self.assertEqual(prepare.prepare(current, selected, self.relative, self.root), selected)
        render.assert_not_called()

    def test_cli_updates_selection_and_notes_together_then_preserves_committed_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / self.relative
            path.parent.mkdir()
            def git(*args):
                return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
            git('init', '-q')
            path.write_text(self.with_notes())
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'initial')
            path.write_text(self.with_notes(self.changes()[0]))
            with patch.object(sys, 'argv', ['prepare_notes.py', str(root), 'daily-preview']), patch.object(prepare, 'render', return_value=self.generated) as render:
                prepare.main()
            render.assert_called_once()
            self.assertEqual(self.fields(path.read_text())['body'], self.generated)
            path.write_text(path.read_text().replace('Generated.', 'Reviewed by hand.'))
            pending = path.read_text()
            with patch.object(sys, 'argv', ['prepare_notes.py', str(root), 'daily-preview']), patch.object(prepare, 'render') as render:
                with self.assertRaises(SystemExit):
                    prepare.main()
            render.assert_not_called()
            self.assertEqual(path.read_text(), pending)
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'selected with reviewed notes')
            expected = path.read_text()
            with patch.object(sys, 'argv', ['prepare_notes.py', str(root), 'daily-preview']), patch.object(prepare, 'render') as render:
                prepare.main()
            render.assert_not_called()
            self.assertEqual(path.read_text(), expected)

    def test_obsolete_sha_metadata_has_no_effect_on_preparation(self):
        for mode in ('auto', 'fixed'):
            current = self.with_notes(mode=mode, correction=True)
            for selected in (self.selected, self.changes()[0]):
                with self.subTest(mode=mode, selected=selected), patch.object(prepare, 'render', return_value=self.generated):
                    result = prepare.prepare(current, selected, self.relative, self.root)
                fields = self.fields(result)
                self.assertNotIn('aggregate_sha', fields)
                expected = self.generated if mode == 'auto' and selected != self.selected else self.fields(current)['body']
                self.assertEqual(fields['body'], expected)
                prepare.require_prepared(result, self.relative)

    def test_legacy_migration_preserves_body_and_notice_without_reserved_wrappers(self):
        current = self.selected + ('notes:\n  body: |-\n    ### Human highlight\n\n    Keep this wording.\n'
                                   '  upgrade_notice: |-\n    Upgrade both parts.\n\n    请保留此说明。\n')
        with patch.object(prepare, 'render', return_value=self.generated):
            result = prepare.prepare(current, current, self.relative, self.root)
        fields = self.fields(result)
        self.assertEqual(fields['mode'], 'auto')
        self.assertIn('### Human highlight\n\nKeep this wording.', fields['body'])
        self.assertIn('Upgrade both parts.\n\n请保留此说明。', fields['body'])
        self.assertNotIn('upgrade_notice', fields)
        self.assertNotIn(release_notes.NOTICE_START, fields['body'])
        self.assertNotIn(release_notes.NOTICE_END, fields['body'])
        prepare.require_prepared(result, self.relative)

    def test_preparation_availability_does_not_require_mode_fingerprint_or_sha(self):
        for text in (self.with_notes(digest=False), self.with_notes(digest='0' * 64),
                     self.with_notes(correction=True), self.with_notes(mode='fixed', correction=True),
                     self.selected + 'notes:\n  body: |-\n    Human text.'):
            with self.subTest(text=text):
                prepare.require_prepared(text, self.relative)
        prepare.require_prepared(self.selected + 'notes:\n  body: |-\n    ' + 'x' * 125001, self.relative)
        for text in (self.selected, self.selected + 'notes:\n  body: |-\n    '):
            with self.subTest(text=text[:100]), self.assertRaises(ValueError):
                prepare.require_prepared(text, self.relative)

    def test_real_renderer_persists_source_pinned_inventory_before_future_tags(self):
        previous = dict(self.units, sandboxer='v1.2.2', runtime='runtime-v1.2.2', vmlinux='vmlinux-v1.2.2')
        source_shas = {unit: 'b' * 40 for unit in self.units}
        requests = []
        def pages(endpoint):
            requests.append(endpoint)
            if '/git/ref/tags/' in endpoint:
                self.assertTrue(endpoint.endswith('v1.2.2'))
                return [{'object': {'type': 'commit', 'sha': 'a' * 40}}]
            if '/compare/' in endpoint:
                return [{'status': 'ahead', 'total_commits': 1,
                         'base_commit': {'sha': 'a' * 40},
                         'commits': [{'sha': 'b' * 40, 'commit': {'message': 'Improve selected component (#12)'}}]}]
            return [{'sha': 'b' * 40, 'files': [{'filename': 'native-deps/deps/vmlinux/test.config'}]}]
        with tempfile.TemporaryDirectory() as directory:
            trap = Path(directory) / 'gh'
            called = Path(directory) / 'called'
            trap.write_text('#!/bin/sh\ntouch "' + str(called) + '"\nexit 99\n')
            trap.chmod(0o755)
            with patch.dict(os.environ, {'PATH': directory + os.pathsep + os.environ['PATH']}), \
                 patch.object(prepare.change_inventory.Inventory, 'pages', side_effect=pages):
                result = prepare.prepare(self.selected, self.selected, self.relative, self.root,
                                         previous_units=previous, source_shas=source_shas)
            self.assertFalse(called.exists())
            path = Path(directory) / 'manifest.yaml'
            path.write_text(result)
            body = self.fields(path.read_text())['body']
        version = 'release-v2.0.0-preview.20990101'
        release_notes.validate(body, version, self.units)
        self.assertIn('https://github.com/kuasar-sandbox/sandboxer/compare/' + 'a' * 40 + '...' + 'b' * 40, body)
        self.assertIn('https://github.com/kuasar-sandbox/guest-runtime/pull/12', body)
        self.assertIn('Selected version unchanged: `v1.2.3`.', body)
        self.assertIn('Improve selected component', body)
        self.assertIn('/commit/' + 'b' * 40, body)
        self.assertIn('Kernel release inputs:', body)
        prepare.require_prepared(result, self.relative)

    def test_length_waits_for_publication_and_no_compression_policy_exists(self):
        current = self.with_notes()
        with patch.object(prepare, 'render', return_value='x' * 125001):
            result = prepare.prepare(current, self.changes()[0], self.relative, self.root)
        self.assertEqual(self.fields(result)['body'], 'x' * 125001 + '\n')
        prepare.require_prepared(result, self.relative)
        for policy in ('manual', 'compact', 'truncate'):
            with self.assertRaisesRegex(ValueError, 'invalid notes field'):
                self.fields(self.with_notes().replace('  mode: auto\n', '  mode: auto\n  overflow: ' + policy + '\n'))


if __name__ == '__main__':
    unittest.main()
