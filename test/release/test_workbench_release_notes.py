"""Narrative adaptation preserves exact evidence and the existing body readers."""
import copy
import json
from pathlib import Path
import shutil
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

    def test_complete_modes_use_exact_body_without_generated_template(self):
        for mode in ('auto', 'fixed'):
            for chomping, ending in (('|', '\n'), ('|-', '')):
                with self.subTest(mode=mode, chomping=chomping):
                    block = f'notes:\n  mode: {mode}\n  body: {chomping}\n    自定义说明。  \n\n    No required headings.\n'
                    fields = notes.selection.read_simple_yaml(self.manifest + block, 'manifest')['notes']
                    expected = '自定义说明。  \n\nNo required headings.' + ending
                    narrative = notes.Narrative(self.text, fields, 'immutable-url')
                    self.assertEqual(narrative.text, expected)
                    self.assertEqual(narrative.fit(expected, 1), expected)

    def test_legacy_stage_bridge_is_tied_to_exact_immutable_source(self):
        original = self.revision
        self.assertTrue(notes.legacy_admitted_source(self.root, original))
        (self.root / 'release').mkdir()
        (self.root / 'release/prepare_notes.py').write_text('# preparation contract\n')
        self.git('add', 'release/prepare_notes.py')
        self.git('commit', '-qm', 'introduce preparation')
        self.assertFalse(notes.legacy_admitted_source(self.root, self.git('rev-parse', 'HEAD')))
        self.assertTrue(notes.legacy_admitted_source(self.root, original))
        with self.assertRaises(subprocess.CalledProcessError):
            notes.legacy_admitted_source(self.root, 'f' * 40)

    def test_strict_validator_rejects_new_source_without_complete_notes(self):
        (self.root / 'release').mkdir()
        (self.root / 'release/prepare_notes.py').write_text('# preparation contract\n')
        self.git('add', 'release/prepare_notes.py')
        self.git('commit', '-qm', 'introduce preparation')
        output = self.root / 'notes.md'
        output.write_text(self.text)
        with self.assertRaisesRegex(ValueError, 'publication requires complete manifest notes'):
            notes.validate_file(self.root, self.version, output)

    def test_windows_literal_line_endings_match_committed_and_local_readers(self):
        text = self.manifest + 'notes:\n  mode: fixed\n  body: |\n    First.  \n\n    Last.\n'
        self.assertEqual(notes.selection.read_simple_yaml(text, 'LF'),
                         notes.selection.read_simple_yaml(text.replace('\n', '\r\n'), 'CRLF'))

    def test_literal_eof_unicode_separators_and_blank_indentation(self):
        for ending in ('', '\n'):
            for style in ('|', '|-'):
                literal = 'Keep\u2028this\u0085text.  '
                block = f'notes:\n  mode: fixed\n  body: {style}\n    {literal}{ending}'
                expected = literal + ('\n' if style == '|' and ending else '')
                self.assertEqual(notes.selection.read_simple_yaml(self.manifest + block, 'fixture')['notes']['body'], expected)
        for invalid in ('\t\n', '\u00a0\n', '  \t\n'):
            block = 'notes:\n  mode: fixed\n  body: |\n    First.\n' + invalid + '    Last.\n'
            with self.subTest(invalid=repr(invalid)), self.assertRaises(ValueError):
                notes.selection.read_simple_yaml(self.manifest + block, 'fixture')

    def test_complete_schema_rejects_legacy_notice_until_explicit_migration(self):
        for mode in ('auto', 'fixed'):
            with self.assertRaisesRegex(ValueError, 'migrate notes.upgrade_notice into notes.body'):
                notes.selection.read_simple_yaml(self.manifest + self.block.replace(
                    'notes:\n', f'notes:\n  mode: {mode}\n'), 'manifest')
        for block in ('notes:\n  mode: invalid\n  body: |\n    Text',
                      'notes:\n  mode: fixed\n  aggregate_sha: ' + 'a' * 40,
                      'notes:\n  mode: fixed\n  body: |-\n    ',
                      'notes:\n  mode: fixed\n  body: |\n    ' + body.MARKER):
            with self.subTest(block=block), self.assertRaises(ValueError):
                notes.selection.read_simple_yaml(self.manifest + block, 'manifest')

    def test_complete_notes_correction_preserves_frozen_manifest_bridge(self):
        correction = (f'notes:\n  mode: fixed\n  aggregate_sha: {self.revision}\n'
                      '  body: |-\n    Full replacement including upgrade instructions.')
        corrected = self.commit(self.manifest + correction)
        narrative = self.narrative()
        self.assertEqual(narrative.text, 'Full replacement including upgrade instructions.')
        self.assertIn(corrected, narrative.url)
        for text in (self.manifest + correction.replace(self.revision, '0' * 40),
                     self.manifest.replace('components:', 'delivery: workbench-v1\ncomponents:') + correction,
                     self.manifest.replace('sandboxer: v1.2.3', 'sandboxer: v2.0.0') + correction,
                     self.manifest.replace(self.version, 'release-v9.8.8') + correction):
            self.commit(text)
            with self.assertRaisesRegex(ValueError, 'differs|changes'):
                self.narrative()

    def test_complete_notes_warn_only_on_generated_stale_references(self):
        old = 'release-v0.1.0'
        text = (f'# Kuasar Sandbox {old}\n| `sandboxer` | `v0.1.0` |\n'
                f'[Quick Start](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/{old}/docs/quickstart.md)')
        warnings = notes.stale_warnings(text, self.version, self.units)
        self.assertEqual(len(warnings), 3)
        self.assertEqual(notes.stale_warnings('Upgrade from release-v0.1.0 and sandboxer v0.1.0.',
                                             self.version, self.units), [])
        historical = ('[historical docs](https://github.com/kuasar-sandbox/kuasar-sandbox/'
                      f'blob/{old}/docs/quickstart.md)')
        self.assertEqual(notes.stale_warnings(historical, self.version, self.units), [])
        deployment = ('[Deployment](https://github.com/kuasar-sandbox/kuasar-sandbox/'
                      f'blob/{old}/docs/deployment.md)')
        self.assertEqual(notes.stale_warnings(deployment, self.version, self.units),
                         ['documentation link names a different aggregate version'])
        fields = {'mode': 'fixed', 'body': text}
        self.assertEqual(notes.Narrative(self.text, fields, version=self.version, units=self.units).text, text)

    def test_export_and_validate_cli_read_committed_complete_body(self):
        self.commit(self.manifest + 'notes:\n  mode: fixed\n  body: |-\n    Exact complete prose.  \n')
        self.path.write_text('uncommitted invalid manifest')
        output = self.root / 'notes.md'
        command = [sys.executable, '-B', str(notes.ROOT / 'release/release_notes.py')]
        subprocess.run(command + ['export', str(self.root), self.version, str(output)], check=True, capture_output=True)
        self.assertEqual(output.read_text(), 'Exact complete prose.  ')
        subprocess.run(command + ['validate', str(self.root), self.version, str(output)], check=True, capture_output=True)
        output.write_text('Not in the manifest')
        result = subprocess.run(command + ['validate', str(self.root), self.version, str(output)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('differs from complete manifest', result.stderr)

    def test_export_rejects_legacy_and_oversize_without_writing(self):
        output = self.root / 'notes.md'
        output.write_text('unchanged')
        with self.assertRaisesRegex(ValueError, 'prepare complete'):
            notes.export_notes(self.root, self.version, output)
        self.commit(self.manifest + f'notes:\n  mode: fixed\n  aggregate_sha: {self.revision}\n  body: |-\n    Notes.')
        with self.assertRaisesRegex(ValueError, 'only for publisher corrections'):
            notes.export_notes(self.root, self.version, output)
        self.commit(self.manifest + 'notes:\n  mode: fixed\n  body: |-\n    ' + 'x' * (body.MAX_CHARACTERS + 1))
        with self.assertRaisesRegex(ValueError, 'must not be truncated'):
            notes.export_notes(self.root, self.version, output)
        self.assertEqual(output.read_text(), 'unchanged')

    def test_bound_validate_accepts_only_exact_corrected_manifest_prefix(self):
        # Frozen platform remains on its legacy source; publisher carries the
        # explicit notes-only correction with its original aggregate SHA.
        frozen = self.root / 'frozen'
        subprocess.run(['git', 'clone', '-q', str(self.root), str(frozen)], check=True)
        self.commit(self.manifest + f'notes:\n  mode: fixed\n  aggregate_sha: {self.revision}\n'
                    '  body: |-\n    Corrected full human prefix.')
        output = self.root / 'notes.md'
        suffix = '\n' + body.MARKER + '{} -->\n'
        output.write_text('Corrected full human prefix.' + suffix)
        publisher_release = self.root / 'release'
        publisher_release.mkdir()
        for module in ('release_notes.py', 'selection.py', 'publication_body.py'):
            shutil.copyfile(notes.ROOT / 'release' / module, publisher_release / module)
        command = [sys.executable, '-B', str(publisher_release / 'release_notes.py'),
                   'validate', str(frozen), self.version, str(output)]
        subprocess.run(command, check=True, capture_output=True)
        self.assertEqual(subprocess.check_output(['git', '-C', str(frozen), 'rev-parse', 'HEAD'],
                                                text=True).strip(), self.revision)
        with patch.object(notes, 'ROOT', self.root):
            notes.validate_file(frozen, self.version, output)
            output.write_text('Different human prose' + suffix)
            with self.assertRaisesRegex(ValueError, 'human prefix differs'):
                notes.validate_file(frozen, self.version, output)
        failed = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('human prefix differs', failed.stderr)
        output.write_text('Corrected full human prefix.' + suffix)
        corrected_text = self.path.read_text()
        self.commit(corrected_text.replace(self.revision, '0' * 40))
        failed = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('notes.aggregate_sha differs', failed.stderr)
        self.commit(corrected_text.replace('sandboxer: v1.2.3', 'sandboxer: v2.0.0'))
        failed = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('changes the admitted manifest', failed.stderr)

    def test_selection_fingerprint_is_optional_non_authoritative_metadata(self):
        for mode in ('auto', 'fixed'):
            block = f'notes:\n  mode: {mode}\n  selection_sha256: ' + '0' * 64 + '\n  body: |-\n    Exact body.'
            fields = notes.selection.read_simple_yaml(self.manifest + block, 'manifest')['notes']
            self.assertEqual(fields['selection_sha256'], '0' * 64)
            self.assertEqual(notes.Narrative(self.text, fields).text, 'Exact body.')
            self.commit(self.manifest + block)
            output = self.root / 'exported.md'
            if mode == 'auto':
                with self.assertRaisesRegex(ValueError, 'do not describe the current selection'):
                    notes.export_notes(self.root, self.version, output)
                self.assertFalse(output.exists())
                # Historical bound bodies remain legitimate independently of
                # optional generation bookkeeping, never test provenance.
                output.write_text('Exact body.\n' + body.MARKER + '{} -->\n')
                with patch.object(notes, 'ROOT', self.root):
                    notes.validate_file(self.root, self.version, output)
                output.unlink()
            else:
                notes.export_notes(self.root, self.version, output)
                self.assertEqual(output.read_text(), 'Exact body.')
                notes.validate_file(self.root, self.version, output)
        for digest in ('a' * 63, 'a' * 65, 'A' * 64, 'g' * 64):
            block = 'notes:\n  mode: auto\n  selection_sha256: ' + digest + '\n  body: |-\n    Text.'
            with self.subTest(digest=digest), self.assertRaisesRegex(ValueError, '64 lowercase hex'):
                notes.selection.read_simple_yaml(self.manifest + block, 'manifest')
        block = 'notes:\n  selection_sha256: ' + 'a' * 64 + '\n  body: |-\n    Text.'
        with self.assertRaisesRegex(ValueError, 'requires notes.mode'):
            notes.selection.read_simple_yaml(self.manifest + block, 'manifest')

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

    def test_complete_notes_replace_full_prefix_without_changing_machine_evidence(self):
        binder = test_workbench_binding.binder
        # A prior generated version heading is intentionally retained: fixed
        # notes survive selection changes, with a diagnostic rather than failure.
        human = '# Kuasar Sandbox release-v0.1.0\n\nManually reviewed instructions.  \n'
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with patch.object(notes, 'publication_notes', return_value=({'mode': 'fixed', 'body': human}, 'url')):
            binder.bind(self.root, self.plan, self.validation, self.results, platform=self.root)
        result = (self.root / 'release-notes.md').read_text()
        self.assertEqual(result.split(body.MARKER)[0], human + '\n')
        restored = json.loads(result.split(body.MARKER)[1].split(' -->')[0])
        self.assertEqual(restored['validation_plan'], self.plan)
        self.assertEqual(restored['source_records'], self.plan['source_records'])
        self.assertEqual(restored['aggregate_sha'], self.sha)
        self.assertEqual(restored['architectures'], self.validation['architectures'])
        self.assertEqual(restored['workbench'], self.results)
        for path, contents in before.items():
            if path.name != 'release-notes.md':
                self.assertEqual(path.read_bytes(), contents)

    def test_complete_notes_budget_failure_does_not_fallback_or_write(self):
        binder = test_workbench_binding.binder
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        human = 'Manually reviewed instructions.\n' * 5000
        for mode in ('auto', 'fixed'):
            with patch.object(notes, 'publication_notes', return_value=({'mode': mode, 'body': human}, 'url')):
                with patch.object(notes, 'compact_commits') as compact, self.assertRaisesRegex(ValueError, 'must not be truncated'):
                    binder.bind(self.root, self.plan, self.validation, self.results, platform=self.root)
                compact.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_complete_notes_do_not_bypass_machine_asset_checks(self):
        binder = test_workbench_binding.binder
        target = next((self.root / 'assets').iterdir())
        target.write_text('tampered')
        with patch.object(notes, 'publication_notes', return_value=({'mode': 'fixed', 'body': 'Reviewed.'}, 'url')):
            with self.assertRaisesRegex(ValueError, 'publisher bytes differ'):
                binder.bind(self.root, self.plan, self.validation, self.results, platform=self.root)
        self.assertEqual((self.root / 'release-notes.md').read_text(), 'Original notes\n')

    def test_prefeature_stage_keeps_its_original_legacy_contract(self):
        binder = test_workbench_binding.binder
        with patch.object(notes, 'publication_notes', return_value=({}, None)), \
             patch.object(notes, 'legacy_admitted_source', return_value=True) as legacy:
            binder.bind(self.root, self.plan, self.validation, self.results, platform=self.root)
        legacy.assert_called_once_with(self.root, self.sha)
        result = (self.root / 'release-notes.md').read_text()
        self.assertEqual(result.split(body.MARKER)[0], 'Original notes\n\n')
        restored = json.loads(result.split(body.MARKER)[1].split(' -->')[0])
        self.assertEqual(restored['validation_plan'], self.plan)
        self.assertEqual(restored['workbench'], self.results)

    def test_publication_rejects_unprepared_legacy_instead_of_generated_fallback(self):
        binder = test_workbench_binding.binder
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        for fields in ({}, {'body': 'Old highlight.'}, {'upgrade_notice': 'Old notice.'}):
            with self.subTest(fields=fields), patch.object(notes, 'publication_notes', return_value=(fields, 'url')), \
                 patch.object(notes, 'legacy_admitted_source', return_value=False):
                with self.assertRaisesRegex(ValueError, 'publication requires complete manifest notes'):
                    binder.bind(self.root, self.plan, self.validation, self.results, platform=self.root)
            self.assertEqual({path: path.read_bytes() for path in before}, before)


if __name__ == '__main__':
    unittest.main()
