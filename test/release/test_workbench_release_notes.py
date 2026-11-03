"""Narrative adaptation preserves exact evidence and the existing body readers."""
import copy
import json
import re
from urllib.parse import urljoin
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'release'))
import publication_body as body
import release_notes as notes
import test_workbench_binding


class NarrativeTests(unittest.TestCase):
    def setUp(self):
        self.version = 'release-v9.8.7'
        self.units = {u: (u + '-' if u in ('runtime', 'vmlinux') else '') + 'v1.2.3' for u in notes.selection.UNITS}
        self.manifest = 'version: ' + self.version + '\ncomponents:\n' + ''.join(
            '  ' + u + ': ' + t + '\n' for u, t in self.units.items())
        self.block = 'notes:\n  body: |\n    ### Changed #12\n\n    Two lines.\n  upgrade_notice: |-\n    请同步升级。\n'
        self.text = '# Kuasar Sandbox ' + self.version + '\n\n## Highlights\n\nGenerated facts.\n'
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.git('init', '-q')
        (self.root / 'releases').mkdir()
        self.path = self.root / 'releases/release.yaml'
        self.revision = self.commit(self.manifest + self.block)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), '-c', 'user.name=Fixture',
            '-c', 'user.email=test@example.invalid', *args], text=True).strip()

    def commit(self, text):
        self.path.write_text(text)
        self.git('add', 'releases')
        self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def narrative(self):
        fields, url = notes.publication_notes(self.root, self.version, self.revision, self.units, self.root)
        return notes.Narrative(self.text, fields, url)

    def test_admitted_bytes_are_default_across_head_and_worktree_changes(self):
        self.commit(self.manifest + self.block.replace('Two lines.', 'New notes.'))
        self.path.write_text('uncommitted')
        result = self.narrative()
        self.assertIn('### Changed #12\n\nTwo lines.', result.text)
        self.assertIn('请同步升级。', notes.inline_notice(result.text))
        self.assertIn(self.revision + '/releases/release.yaml#L', result.url)
        self.assertNotIn('New notes.', result.text)

    def test_correction_is_bound_to_sha_and_whole_selection(self):
        correction = self.block.replace('Two lines.', 'Corrected.').replace(
            'notes:\n', f'notes:\n  aggregate_sha: {self.revision}\n')
        corrected_sha = self.commit(self.manifest + correction)
        self.assertIn('Corrected.', self.narrative().text)
        self.assertIn(corrected_sha, self.narrative().url)
        for text in (self.manifest + correction.replace(self.revision, '0' * 40),
                     self.manifest.replace('components:', 'delivery: workbench-v1\ncomponents:') + correction,
                     self.manifest.replace('sandboxer: v1.2.3', 'sandboxer: v2.0.0') + correction):
            self.commit(text)
            with self.assertRaisesRegex(ValueError, 'differs|changes'):
                self.narrative()

    def test_preview_is_independent_and_absent_notes_keep_generated_text(self):
        self.path = self.path.with_name('daily-preview.yaml')
        preview = 'preview.20990101'
        sha = self.commit(self.manifest + 'preview_version: ' + preview + '\n')
        fields, url = notes.publication_notes(self.root, self.version + '-' + preview, sha, self.units, self.root)
        self.assertEqual(notes.Narrative(self.text, fields, url).text, self.text)
        for field in ('body', 'upgrade_notice'):
            sha = self.commit(self.manifest + 'preview_version: ' + preview +
                              '\nnotes:\n  ' + field + ': |\n    Preview only.\n')
            fields, _ = notes.publication_notes(self.root, self.version + '-' + preview, sha, self.units, self.root)
            self.assertEqual(fields, {field: 'Preview only.\n'})

    def test_reject_malformed_fields_and_reserved_markers(self):
        for block in ('notes: invalid', 'notes:\n  unknown: yes', 'notes:\n  body: |',
                      self.block + '  body: |\n    duplicate', self.block * 2,
                      self.block.replace('Two lines.', body.MARKER), self.block.replace('Two lines.', notes.NOTICE_END)):
            with self.subTest(block=block), self.assertRaises(notes.selection.ManifestError):
                notes.selection.parse_manifest(self.manifest + block, 'manifest', False)

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

    def test_budget_links_body_to_its_exact_lines_and_retains_notice_and_evidence(self):
        record = self.binding()
        original = copy.deepcopy(record)
        narrative = self.narrative()
        narrative = notes.Narrative(self.text, dict(narrative.fields, body='Long body. ' * 2000), narrative.url)
        with patch.object(body, 'MAX_CHARACTERS', 2500):
            result = body.binding_text(narrative.text + '\n', record, '\n',
                reserve_registry_version=self.version, fit_notes=narrative.fit)
        self.assertIn(narrative.url, result)
        self.assertLess(result.index(narrative.notice), result.index(narrative.url))
        self.assertIn('Generated facts.', result)
        self.assertEqual(json.loads(result.split(body.MARKER)[1].split(' -->')[0]), original)
        self.assertEqual(original, record)

    def test_minimum_delivery_summary_retains_actual_comparison_and_notice(self):
        url = 'https://example.invalid/committed-manifest#L12-L18'
        compare = 'https://github.com/kuasar-sandbox/sandboxer/compare/v1...v2'
        generated = self.text + 'Generated detail.\n' * 2000 + '\n### sandboxer\n[Full comparison](' + compare + ')\n'
        fields = {'body': 'Long prose. ' * 2000, 'upgrade_notice': 'Upgrade both components.'}
        narrative = notes.Narrative(generated, fields, url, version=self.version, units=self.units)
        adjusted = narrative.fit(narrative.text, 5000)
        self.assertLessEqual(len(adjusted), 5000)
        self.assertEqual(notes.inline_notice(adjusted), narrative.notice)
        self.assertIn(compare, adjusted)
        self.assertIn(url, adjusted)
        notes.validate(adjusted, self.version, self.units)

    def test_last_summary_keeps_warning_links_selection_and_evidence_at_exact_budget(self):
        url = 'https://example.invalid/immutable#L11-L34'
        comparison = 'https://github.com/kuasar-sandbox/sandboxer/compare/v1...v2'
        generated = self.text + 'Generated detail.\n' * 2000 + '\n### sandboxer\n[Full comparison](' + comparison + ')\n'
        fields = {'body': 'Authored prose. ' * 2000, 'upgrade_notice': 'Mandatory warning. 请保留。\n' * 120}
        narrative = notes.Narrative(generated, fields, url, version=self.version, units=self.units)
        minimum = notes.compact_delivery_summary(self.version, self.units, url, narrative.notice, generated)
        self.assertLess(len(minimum), len(notes.delivery_summary(self.version, self.units, url, narrative.notice, generated)))
        self.assertEqual(narrative.fit(narrative.text, len(minimum)), minimum)
        self.assertEqual(narrative.fit(narrative.text, len(minimum) - 1), narrative.text)
        self.assertEqual(notes.inline_notice(minimum), narrative.notice)
        self.assertIn(comparison, [urljoin('https://github.com/kuasar-sandbox/kuasar-sandbox/releases/tag/' + self.version, target)
                                  for target in re.findall(r'\]\(([^)]+)\)', minimum)])
        self.assertIn(url, minimum)
        self.assertIn('/blob/' + self.version + '/docs', minimum)
        self.assertIn('ARM Workbench artifact-only ≠ system tests.', minimum)
        notes.validate(minimum, self.version, self.units)
        record = self.binding()
        original = copy.deepcopy(record)
        baseline = body.binding_text(minimum, record, '\n')
        with patch.object(body, 'MAX_CHARACTERS', len(baseline)):
            adjusted = body.binding_text(narrative.text, record, '\n', fit_notes=narrative.fit)
        self.assertEqual(adjusted, baseline)
        self.assertEqual(json.loads(adjusted.split(body.MARKER)[1].split(' -->')[0]), original)
        self.assertEqual(record, original)
        with patch.object(body, 'MAX_CHARACTERS', len(baseline) - 1), self.assertRaisesRegex(ValueError, 'must not be truncated'):
            body.binding_text(narrative.text, record, '\n', fit_notes=narrative.fit)

    def test_last_summary_keeps_preview_label_and_no_notice(self):
        version = self.version + '-preview.20990101'
        summary = notes.compact_delivery_summary(version, self.units, 'https://example.invalid/notes', '', self.text)
        self.assertIn('Preview.', summary)
        self.assertNotIn('Stable.', summary)
        self.assertEqual(notes.inline_notice(summary), '')
        notes.validate(summary, version, self.units)

    def test_richer_delivery_summary_still_wins_when_it_fits(self):
        generated = self.text + 'Generated detail.\n' * 2000
        fields = {'body': 'Long prose. ' * 2000, 'upgrade_notice': 'Full warning.'}
        narrative = notes.Narrative(generated, fields, 'https://example.invalid/notes', version=self.version, units=self.units)
        richer = notes.delivery_summary(self.version, self.units, narrative.url, narrative.notice, generated) + '\n'
        self.assertEqual(narrative.fit(narrative.text, len(richer)), richer)

    def test_last_summary_does_not_hide_malformed_marked_warning(self):
        for notice in (notes.NOTICE_START + '\n## Warning\nMissing end.',
                       notes.NOTICE_END, notes.NOTICE_START + '\nnot a heading\n' + notes.NOTICE_END):
            with self.subTest(notice=notice), self.assertRaisesRegex(ValueError, 'release notice'):
                notes.compact_delivery_summary(self.version, self.units, 'https://example.invalid/notes', notice, self.text)

    def test_last_summary_removes_only_duplicate_generated_heading(self):
        fields = {'upgrade_notice': '## Authored warning\n\nKeep every word. 请保留。\n'}
        narrative = notes.Narrative(self.text, fields, 'https://example.invalid/notes')
        summary = notes.compact_delivery_summary(self.version, self.units, narrative.url, narrative.notice, self.text)
        expected = notes.NOTICE_START + '\n' + fields['upgrade_notice'] + notes.NOTICE_END
        self.assertEqual(notes.inline_notice(summary), expected)
        self.assertEqual(len(narrative.notice) - len(expected), len('## Important upgrade notice\n\n'))
        self.assertTrue(summary.startswith('# Kuasar Sandbox ' + self.version + '\n' + expected))
        # Do not deduplicate arbitrary authored titles or warnings without H2s.
        for warning in ('No heading.\n', '# Authored H1\n', '### Authored H3\n'):
            narrative = notes.Narrative(self.text, {'upgrade_notice': warning})
            summary = notes.compact_delivery_summary(self.version, self.units, 'notes', narrative.notice, self.text)
            self.assertEqual(notes.inline_notice(summary), narrative.notice)

    def test_last_summary_keeps_exact_github_targets_and_private_reporting(self):
        base = 'https://github.com/kuasar-sandbox/kuasar-sandbox/releases/tag/' + self.version
        manifest = 'https://github.com/kuasar-sandbox/kuasar-sandbox/blob/' + self.revision + '/releases/release.yaml#L11-L34'
        comparison = 'https://github.com/kuasar-sandbox/sandboxer/compare/v1...v2'
        generated = self.text + '\n### sandboxer\n[Full comparison](' + comparison + ')\n'
        summary = notes.compact_delivery_summary(self.version, self.units, manifest, '', generated)
        targets = [urljoin(base, target) for target in re.findall(r'\]\(([^)]+)\)', summary)]
        self.assertEqual(targets, [manifest.split('#', 1)[0],
            'https://github.com/kuasar-sandbox/kuasar-sandbox/blob/' + self.version + '/docs/quickstart.md',
            'https://github.com/kuasar-sandbox/kuasar-sandbox/security/advisories/new', comparison])
        self.assertIn('## Production deployment\nSee Quick Start.', summary)
        self.assertIn('Verify SHA256SUMS.', summary)
        self.assertIn('## Security\n[Private]', summary)
        for url in ('https://github.com.evil.invalid/notes', 'https://example.invalid/notes'):
            summary = notes.compact_delivery_summary(self.version, self.units, url, '', self.text)
            self.assertIn('[↗](' + url + ')', summary)

    def test_irreducible_notice_or_evidence_rejects_publication(self):
        for long_notice in (True, False):
            record = self.binding()
            fields = {'body': 'Long body. ' * 2000, 'upgrade_notice': 'Warning! ' * (2000 if long_notice else 1)}
            if not long_notice:
                record['validation_plan']['evidence'] = 'x' * 4000
            narrative = notes.Narrative(self.text, fields, 'https://example.invalid/immutable')
            with patch.object(body, 'MAX_CHARACTERS', 2500), self.assertRaisesRegex(ValueError, 'must not be truncated'):
                body.binding_text(narrative.text, record, '\n', reserve_registry_version=self.version, fit_notes=narrative.fit)

    def test_generated_commit_compaction_cannot_change_any_marked_notice_bytes(self):
        section = ('### sandboxer\nSelected version: `v1` → `v2`.\n'
            '- `aaaaaaaaaaaa` title ([commit](https://github.com/kuasar-sandbox/sandboxer/commit/' + 'a' * 40 + '))\n'
            '[Full comparison](https://github.com/kuasar-sandbox/sandboxer/compare/v1...v2)\n')
        notice = notes.NOTICE_START + '\n## Important upgrade notice\n' + section + notes.NOTICE_END
        original = '# Kuasar Sandbox release-v0.1.10\n\n' + notice + '\n' + section
        compact = notes.compact_commits(original)
        self.assertEqual(notes.inline_notice(compact), notice)
        self.assertLess(len(compact), len(original))

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
        generated = f'# Kuasar Sandbox {self.version}\n\n## Highlights\n\nAsset selection.\n'
        generated += '\n'.join('## ' + h for h in notes.HEADINGS if h != 'Highlights') + '\n'
        generated += '\n'.join(f'| `{u}` | `{tag}` |' for u, tag in units.items()) + '\n'
        generated += '\n'.join('### ' + u for u in units) + '\n'
        (self.root / 'release-notes.md').write_text(generated)
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file() and path.name != 'release-notes.md'}
        fields = {'body': 'Reviewed change.', 'upgrade_notice': 'Upgrade together.'}
        with patch.object(notes, 'publication_notes', return_value=(fields, 'fixed-url')) as read:
            binder.bind(self.root, self.plan, self.validation, self.results, platform=self.root)
        read.assert_called_once_with(self.root, self.version, self.sha, units, notes.ROOT)
        result = (self.root / 'release-notes.md').read_text()
        self.assertIn('Upgrade together.', notes.inline_notice(result))
        self.assertIn('Reviewed change.', result)
        restored = json.loads(result.split(body.MARKER)[1].split(' -->')[0])
        self.assertEqual(restored['validation_plan'], self.plan)
        self.assertEqual(restored['architectures'], self.validation['architectures'])
        self.assertEqual(restored['workbench'], self.results)
        self.assertEqual({path: path.read_bytes() for path in before}, before)


if __name__ == '__main__':
    unittest.main()
