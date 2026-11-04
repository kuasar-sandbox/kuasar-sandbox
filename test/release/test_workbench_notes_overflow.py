"""Publication compaction requires opt-in, complete evidence and committed prose."""
import base64
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'release'))
import release_notes as notes
import publication_body as body
import preview_coordinator as coordinator
import test_workbench_binding


class OverflowTests(unittest.TestCase):
    def setUp(self):
        self.version = 'release-v1.2.3'
        self.snapshot = '0' * 40
        self.url = 'https://github.com/kuasar-sandbox/kuasar-sandbox/blob/' + self.snapshot + '/releases/release.yaml#L1-L99'
        self.row = '- Human-readable title ([#42](https://github.com/kuasar-sandbox/sandboxer/pull/42), [commit aaaaaaaaaaaa](https://github.com/kuasar-sandbox/sandboxer/commit/' + 'a' * 40 + ')).\n'
        self.intro = '### sandboxer\nSelected version: `v1.2.2` → `v1.2.3`.\n'
        self.link = '[Full comparison](https://github.com/kuasar-sandbox/sandboxer/compare/' + 'b' * 40 + '...' + 'a' * 40 + ')\n'
        self.prose = 'Do not lose this handwritten warning.  \n'
        self.full = self.intro + self.row * 10 + self.prose + self.link
        self.compact = self.intro + self.prose + self.link
        self.fields = {'mode': 'fixed', 'overflow': 'compact', 'body': self.full}
        self.manifest = 'version: ' + self.version + '\ncomponents:\n' + ''.join(
            f'  {unit}: ' + (unit + '-' if unit in ('runtime', 'vmlinux') else '') + 'v1.2.3\n' for unit in notes.UNITS)
        self.manifest += 'notes: # keep\n  mode: fixed\n  overflow: compact # explicit opt in\n  body: | # literal\n' + ''.join('    ' + line + '\n' for line in self.full[:-1].split('\n'))

    def test_exact_budget_never_rewrites_or_persists(self):
        persist = Mock()
        narrative = notes.Narrative('', self.fields, persist=persist)
        self.assertEqual(narrative.fit(self.full, len(self.full)), self.full)
        persist.assert_not_called()

    def test_default_manual_and_explicit_manual_preserve_fixed_and_auto(self):
        for mode in ('fixed', 'auto'):
            for overflow in ({}, {'overflow': 'manual'}):
                persist = Mock()
                fields = dict(body=self.full, mode=mode, **overflow)
                narrative = notes.Narrative('', fields, persist=persist)
                self.assertEqual(narrative.fit(self.full, 1), self.full)
                persist.assert_not_called()

    def test_compact_changes_only_generated_rows_and_requires_committed_readback(self):
        persist = Mock(return_value=self.compact)
        narrative = notes.Narrative('', self.fields, persist=persist)
        self.assertEqual(narrative.fit(self.full + '\n', len(self.compact) + 1), self.compact + '\n')
        persist.assert_called_once_with(self.fields, self.compact)
        with self.assertRaisesRegex(ValueError, 'permission'):
            notes.Narrative('', self.fields).fit(self.full, len(self.compact))
        persist.return_value = 'unconfirmed'
        with self.assertRaisesRegex(ValueError, 'readback'):
            narrative.fit(self.full, len(self.compact))

    def test_irreducible_prose_or_evidence_does_not_commit(self):
        persist = Mock()
        narrative = notes.Narrative('', self.fields, persist=persist)
        for limit in (-1, len(self.compact) - 1):
            with self.assertRaisesRegex(ValueError, 'must not be truncated'):
                narrative.fit(self.full, limit)
        persist.assert_not_called()

    def test_wrong_commit_or_pr_repository_and_mismatched_sha_are_preserved(self):
        for row in (self.row.replace('/sandboxer/pull/', '/accelerator/pull/'),
                    self.row.replace('/sandboxer/commit/', '/accelerator/commit/'),
                    self.row.replace('commit aaaaaaaaaaaa', 'commit bbbbbbbbbbbb'),
                    self.row.replace('pull/42', 'pull/43')):
            original = self.intro + row + self.link
            self.assertEqual(notes.compact_commits(original), original)

    def test_manifest_replacement_preserves_all_other_fields_and_comments(self):
        revised = notes.replace_manifest_body(self.manifest, 'releases/release.yaml', self.compact)
        self.assertEqual(notes.selection.read_simple_yaml(revised, 'manifest')['notes'], dict(self.fields, body=self.compact))
        self.assertIn('notes: # keep', revised)
        self.assertIn('overflow: compact # explicit opt in', revised)
        self.assertIn('body: | # literal', revised)
        for original in (self.manifest.replace('handwritten', 'hand\u2028written'), self.manifest.replace('\n', '\r\n')):
            compact = notes.compact_commits(notes.selection.read_simple_yaml(original, 'manifest')['notes']['body'])
            revised = notes.replace_manifest_body(original, 'manifest', compact)
            self.assertEqual(notes.selection.read_simple_yaml(revised, 'manifest')['notes']['body'], compact)

    # Other suites load the coordinator under the same module name. Pin this
    # test's mocked module for the lazy production import, then restore it.
    @patch.dict(sys.modules, {'preview_coordinator': coordinator})
    def test_persistence_cas_and_verified_remote_body(self):
        head, sha = '1' * 40, '2' * 40
        revised = notes.replace_manifest_body(self.manifest, 'releases/release.yaml', self.compact)
        def api(endpoint):
            text = self.manifest if endpoint.endswith((head, self.snapshot)) else revised
            return {'content': base64.b64encode(text.encode()).decode()}
        with patch.object(coordinator, 'branch_sha', side_effect=[head, sha]), patch.object(coordinator, 'api', side_effect=api), patch.object(coordinator, 'commit_manifest', return_value=sha) as commit:
            self.assertEqual(notes.persist_compacted_notes(self.fields, self.compact, self.version, 'main', self.url), self.compact)
            commit.assert_called_once_with('releases/release.yaml', revised, self.version, 'main', head)

    @patch.dict(sys.modules, {'preview_coordinator': coordinator})
    def test_stale_manifest_and_failed_or_uncertain_commit_stop_without_retry(self):
        head = '1' * 40
        for changed in (self.manifest.replace(self.version, 'release-v1.2.4', 1),
                        self.manifest.replace('sandboxer: v1.2.3', 'sandboxer: v1.2.4'),
                        'previous_version: release-v1.2.2\n' + self.manifest,
                        'delivery: workbench-v1\n' + self.manifest,
                        self.manifest.replace('handwritten', 'edited')):
            with patch.object(coordinator, 'branch_sha', return_value=head), patch.object(coordinator, 'api', side_effect=lambda endpoint: {'content': base64.b64encode((self.manifest if endpoint.endswith(self.snapshot) else changed).encode()).decode()}), patch.object(coordinator, 'commit_manifest') as commit:
                with self.assertRaisesRegex(ValueError, 'advanced|notes changed'):
                    notes.persist_compacted_notes(self.fields, self.compact, self.version, 'main', self.url)
                commit.assert_not_called()
        with patch.object(coordinator, 'branch_sha', return_value=head), patch.object(coordinator, 'api', return_value={'content': base64.b64encode(self.manifest.encode()).decode()}), patch.object(coordinator, 'commit_manifest', side_effect=RuntimeError('denied')) as commit:
            with self.assertRaisesRegex(RuntimeError, 'denied'):
                notes.persist_compacted_notes(self.fields, self.compact, self.version, 'main', self.url)
            self.assertEqual(commit.call_count, 1)

    def test_complete_evidence_is_unchanged_after_opt_in_compaction(self):
        evidence = {'validation_plan': {'cases': ['all cases'], 'source_records': {'sha': 'a' * 40}}}
        original = copy.deepcopy(evidence)
        persist = Mock(return_value=self.compact)
        narrative = notes.Narrative('', self.fields, persist=persist)
        limit = len(body.binding_text(self.compact, evidence, '\n'))
        with patch.object(body, 'MAX_CHARACTERS', limit):
            result = body.binding_text(self.full, evidence, '\n', fit_notes=narrative.fit)
        self.assertEqual(result, body.binding_text(self.compact, original, '\n'))
        self.assertEqual(evidence, original)


class BindingOverflowTests(unittest.TestCase):
    setUp = test_workbench_binding.BindingTests.setUp

    def test_full_budget_compacts_and_persists_before_bound_output(self):
        fixture = OverflowTests()
        fixture.setUp()
        binder = test_workbench_binding.binder
        fields = fixture.fields
        original = (self.root / 'release-notes.md').read_text()
        calls = []
        def persist(current, compacted, version, source_ref, source_url):
            self.assertEqual((self.root / 'release-notes.md').read_text(), original)
            self.assertEqual((current, compacted, version, source_ref),
                             (fields, fixture.compact, self.version, 'main'))
            self.assertEqual(source_url, 'url')
            calls.append(compacted)
            return compacted
        with patch.object(notes, 'publication_notes', return_value=(dict(fields, body=fixture.compact), 'url')):
            binder.bind(self.root, self.plan, self.validation, self.results, platform=self.root)
        baseline = (self.root / 'release-notes.md').read_text()
        (self.root / 'release-notes.md').write_text(original)
        # Extra room includes future registry evidence but not the long rows.
        with patch.object(notes, 'publication_notes', return_value=(fields, 'url')), patch.object(notes, 'persist_compacted_notes', side_effect=persist), patch.object(body, 'MAX_CHARACTERS', len(baseline) + 1100):
            binder.bind(self.root, self.plan, self.validation, self.results, platform=self.root,
                        source_ref='main', allow_notes_write=True)
        self.assertEqual(calls, [fixture.compact])
        result = (self.root / 'release-notes.md').read_text()
        self.assertEqual(result.split(body.MARKER)[0], baseline.split(body.MARKER)[0])
        actual = json.loads(result.split(body.MARKER)[1].split(' -->')[0])
        expected = json.loads(baseline.split(body.MARKER)[1].split(' -->')[0])
        if 'case_files' not in actual:
            self.assertEqual(expected.pop('case_files'), actual['validation_plan']['case_files'])
        self.assertEqual(actual, expected)

    def test_validation_only_compaction_cannot_write_manifest(self):
        fixture = OverflowTests()
        fixture.setUp()
        binder = test_workbench_binding.binder
        original = (self.root / 'release-notes.md').read_text()
        with patch.object(notes, 'publication_notes', return_value=(fixture.fields, 'url')), patch.object(notes, 'persist_compacted_notes') as persist, patch.object(body, 'MAX_CHARACTERS', 1):
            with self.assertRaises(ValueError):
                binder.bind(self.root, self.plan, self.validation, self.results, platform=self.root,
                            source_ref='main')
        persist.assert_not_called()
        self.assertEqual((self.root / 'release-notes.md').read_text(), original)
