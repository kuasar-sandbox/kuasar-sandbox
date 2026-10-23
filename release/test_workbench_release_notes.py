"""Narrative adaptation preserves exact evidence and the existing body readers."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publication_body as body
import release_notes as notes
import test_workbench_binding


class NarrativeTests(unittest.TestCase):
    def setUp(self):
        self.version = 'release-v0.1.7'
        self.revision = '921c7618e8d981e63ed6d4a2a2a0d5eea246e640'
        self.path = notes.NOTES / (self.version + '.md')
        self.raw = self.path.read_text()
        self.identity = json.loads(self.raw.splitlines()[0][len(notes.HEADER):-4])
        self.units = self.identity['components']

    def narrative(self, text='old generated notes\n'):
        return notes.Narrative(text, self.version, self.revision, self.units)

    def test_reviewed_summary_is_exact_source_bound_and_satisfies_old_contract(self):
        result = self.narrative()
        self.assertEqual(result.text, self.raw.partition('\n')[2])
        notes.validate(result.text, self.version, self.units)
        self.assertIn('Accelerator #183', result.text)
        limitations = result.text.split('## Known limitations\n', 1)[1].split('\n## Security', 1)[0]
        self.assertIn('Checkpoint publication and restore/cold-start paths that materialize a selected disk version can panic if materialization encounters a read or packing error, including insufficient temporary-storage space.', limitations)
        self.assertIn('The panic masks the original error and can leave intermediate files behind', limitations)
        self.assertIn('[Sandboxer #337](https://github.com/kuasar-sandbox/sandboxer/issues/337)', limitations)
        self.assertIn('the fix in [#338](https://github.com/kuasar-sandbox/sandboxer/pull/338) is not included.', limitations)
        self.assertIn('planned Workbench cases do not establish', result.text)
        self.assertIn('snapshot/export --checkpoint ID', result.text)
        self.assertNotRegex(result.text, r'(?m)^- `[0-9a-f]{12}`')
        for version, revision, units in ((self.version, '0' * 40, self.units),
                (self.version + '-preview.20261023.1', self.revision, self.units),
                (self.version, self.revision, dict(self.units, sandboxer='v0.1.8'))):
            with self.subTest(version=version, revision=revision, units=units), self.assertRaisesRegex(ValueError, 'differs'):
                notes.Narrative('old', version, revision, units, self.path)

    def test_invalid_manual_structure_or_reserved_evidence_is_rejected(self):
        for text in (self.raw.replace('## Known limitations', '## Caveats'),
                     self.raw + body.MARKER + '{} -->',
                     self.raw + '\n### sandboxer\n'):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'notes.md'
                path.write_text(text)
                with self.assertRaisesRegex(ValueError, 'must retain'):
                    notes.Narrative('old', self.version, self.revision, self.units, path)

    def test_generated_commit_rows_compact_only_with_comparison_and_preserve_prose(self):
        row = '- `aaaaaaaaaaaa` subject ([commit](https://github.com/kuasar-sandbox/sandboxer/commit/' + 'a' * 40 + '))\n'
        intro = '## Component updates\n\n### sandboxer\n\nSelected version: `v1` → `v2`.\n\n'
        comparison = '[Full comparison](https://github.com/kuasar-sandbox/sandboxer/compare/v1...v2)\n'
        prose = 'Required upgrade caveat, with unicode →.\n'
        self.assertEqual(notes.compact_commits(intro + row + prose), intro + row + prose)
        self.assertEqual(notes.compact_commits(intro + row + prose + comparison), intro + prose + comparison)
        # Non-generated bullets and unknown prose are never clipped to length.
        self.assertEqual(notes.compact_commits(intro + '- User-authored warning\n' + comparison),
                         intro + '- User-authored warning\n' + comparison)
        mismatch = intro + row.replace('/sandboxer/commit/', '/accelerator/commit/') + comparison
        self.assertEqual(notes.compact_commits(mismatch), mismatch)

    def test_commit_compaction_requires_unique_selection_and_exact_comparison_range(self):
        row = '- `aaaaaaaaaaaa` subject ([commit](https://github.com/kuasar-sandbox/sandboxer/commit/' + 'a' * 40 + '))\n'
        selected = 'Selected version: `v0.1.6` → `v0.1.7`.\n'
        comparison = '[Full comparison](https://github.com/kuasar-sandbox/sandboxer/compare/v0.1.6...v0.1.7)\n'
        for selection, link in (
                (selected, comparison.replace('v0.1.6...v0.1.7', 'v0.1.1...v0.1.2')),
                ('', comparison), (selected * 2, comparison),
                (selected, comparison * 2)):
            original = '### sandboxer\n' + selection + row + link
            with self.subTest(selection=selection, link=link):
                self.assertEqual(notes.compact_commits(original), original)

    def binding(self):
        return {'aggregate_sha': self.revision, 'validation_plan': {'cases': ['full case evidence']},
                'workbench': {arch: {'image_id': 'sha256:' + '1' * 64, 'sha256': '2' * 64}
                              for arch in body.WORKBENCH_ARCHES}}

    def test_exact_budget_and_proof_only_overflow_adapt_prefix_not_evidence(self):
        binding = self.binding()
        original = copy.deepcopy(binding)
        prefix = 'Full reviewed prose\n' * 100
        baseline = body.binding_text(prefix, binding, '\n', reserve_registry_version=self.version)
        # Without future proof the original text fits, but reserve must drive fit.
        calls = []
        def adapt(text, available):
            calls.append((text, available))
            return text[:1]  # Test callback only; production never slices prose.
        with patch.object(body, 'MAX_CHARACTERS', len(baseline)):
            result = body.binding_text(prefix, binding, '\n', reserve_registry_version=self.version, fit_notes=adapt)
        self.assertEqual(len(calls), 1)
        self.assertLess(calls[0][1], len(prefix))
        self.assertEqual(json.loads(result.split(body.MARKER)[1].split(' -->')[0]), original)
        self.assertEqual(binding, original)
        self.assertNotIn('registry', binding)
        # Equality is accepted, one more character invokes adaptation.
        plain = body.binding_text(prefix, binding, '\n')
        with patch.object(body, 'MAX_CHARACTERS', len(plain)), patch.object(self.narrative(), 'fit') as fit:
            self.assertEqual(body.binding_text(prefix, binding, '\n', fit_notes=fit), plain)
            fit.assert_not_called()

    def test_evidence_conflicts_are_rejected_before_narrative_callback(self):
        binding = self.binding()
        binding['case_files'] = {'a': [True]}
        binding['validation_plan']['case_files'] = {'a': [1]}
        with patch.object(self.narrative(), 'fit') as fit, self.assertRaisesRegex(ValueError, 'conflicting evidence'):
            body.binding_text('x' * 125000, binding, '\n', fit_notes=fit)
        fit.assert_not_called()

    def test_long_reviewed_prose_links_to_exact_committed_bytes_without_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'release/notes' / self.path.name
            path.parent.mkdir(parents=True)
            path.write_text(self.raw)
            for arguments in (['init', '-q'], ['add', '.'],
                ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture']):
                subprocess.run(['git', '-C', directory, *arguments], check=True, capture_output=True)
            with patch.object(notes, 'ROOT', root):
                url = notes.immutable_notes_url(path)
                self.assertNotIn('/main/', url)
                narrative = notes.Narrative('old', self.version, self.revision, self.units, path)
                minimum = notes.linked_notes(self.version, self.units, url) + '\n'
                adjusted = narrative.fit(narrative.text + '\n', len(minimum))
                self.assertEqual(adjusted, minimum)
                notes.validate(adjusted, self.version, self.units)
                self.assertEqual(path.read_text(), self.raw)
                path.write_text(self.raw + '\nUncommitted change\n')
                with self.assertRaisesRegex(ValueError, 'uncommitted'):
                    narrative.fit(narrative.text + '\n', len(minimum))

    def test_irreducible_evidence_fails_before_any_notes_change(self):
        binding = self.binding()
        binding['validation_plan']['large'] = 'x' * 125000
        original = copy.deepcopy(binding)
        narrative = self.narrative()
        with patch.object(notes, 'immutable_notes_url', return_value='https://example.invalid/exact-notes'):
            with self.assertRaisesRegex(ValueError, r'actual=\d+, budget=125000, blocks=.*validation_plan'):
                body.binding_text(narrative.text, binding, '\n', reserve_registry_version=self.version, fit_notes=narrative.fit)
        self.assertEqual(binding, original)

    def test_generator_cli_keeps_normal_text_and_adapts_large_commit_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'notes.md'
            command = [sys.executable, '-B', str(notes.ROOT / 'release/release_notes.py'), str(path)]
            path.write_text('Short notes\n')
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(path.read_text(), 'Short notes\n')
            row = '- `aaaaaaaaaaaa` title ([commit](https://github.com/kuasar-sandbox/accelerator/commit/' + 'a' * 40 + '))\n'
            text = '### accelerator\nSelected version: `v1` → `v2`.\n' + row * 1000 + '[Full comparison](https://github.com/kuasar-sandbox/accelerator/compare/v1...v2)\n'
            self.assertGreater(len(text), body.MAX_CHARACTERS)
            path.write_text(text)
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(path.read_text(), notes.compact_commits(text))
            path.write_text('Unknown prose\n' * 10000)
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('budget=125000', result.stderr)
            self.assertEqual(path.read_text(), 'Unknown prose\n' * 10000)


class ReviewedBindingTests(unittest.TestCase):
    setUp = test_workbench_binding.BindingTests.setUp

    def test_reviewed_notes_replace_only_prefix_and_preserve_all_bound_bytes(self):
        binder = test_workbench_binding.binder
        units = {unit: row['version'] for unit, row in self.plan['baseline']['units'].items()}
        identity = dict(version=self.version, aggregate_sha=self.sha, components=units)
        prose = notes.linked_notes(self.version, units, 'https://example.invalid/reviewed')
        override = self.root / 'reviewed.md'
        override.write_text(notes.HEADER + json.dumps(identity) + ' -->\n' + prose)
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file() and path.name != 'release-notes.md'}
        binder.bind(self.root, self.plan, self.validation, self.results, notes_override=override)
        result = (self.root / 'release-notes.md').read_text()
        self.assertTrue(result.startswith(prose + '\n' + body.MARKER))
        restored = json.loads(result.split(body.MARKER)[1].split(' -->')[0])
        self.assertEqual(restored['validation_plan'], self.plan)
        self.assertEqual(restored['architectures'], self.validation['architectures'])
        self.assertEqual(restored['workbench'], self.results)
        self.assertEqual({path: path.read_bytes() for path in before}, before)


if __name__ == '__main__':
    unittest.main()
