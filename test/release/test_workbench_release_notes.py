"""Human prose is independent of product admission and preserves exact evidence."""
import copy
import json
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
        fields, url = notes.publication_notes(self.root, self.version)
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

    def test_legacy_source_age_is_not_a_publication_gate(self):
        self.assertFalse(hasattr(notes, 'legacy_admitted_source'))
        output = self.root / 'notes.md'
        output.write_text('Human prose without a generated title or component table.')
        notes.validate_file(self.root / 'nonexistent-checkout', self.version, output)

    def test_validator_checks_only_text_availability_marker_count_and_size(self):
        output = self.root / 'notes.md'
        for text in ('', '  \n', body.MARKER + '{} -->\n' + body.MARKER + '{} -->',
                     'x' * (body.MAX_CHARACTERS + 1)):
            output.write_text(text)
            with self.subTest(text=text[:50]), self.assertRaises(ValueError):
                notes.validate_file(self.root, self.version, output)
        for text in ('Arbitrary human notes.', 'Changed prose\n' + body.MARKER + '{} -->\n'):
            output.write_text(text)
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

    def test_latest_matching_notes_ignore_sha_and_nonnotes_changes(self):
        for pointer in ('a' * 40, '0' * 40, 'obsolete-not-a-sha'):
            correction = (f'notes:\n  mode: fixed\n  aggregate_sha: {pointer}\n'
                          '  body: |-\n    Full replacement including upgrade instructions.')
            changed = self.manifest.replace('sandboxer: v1.2.3', 'sandboxer: v2.0.0')
            changed = changed.replace('components:', 'delivery: workbench-v1\ncomponents:')
            corrected = self.commit(changed + correction)
            narrative = self.narrative()
            self.assertEqual(narrative.text, 'Full replacement including upgrade instructions.')
            self.assertNotIn('aggregate_sha', narrative.fields)
            self.assertIn(corrected, narrative.url)

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
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_export_accepts_legacy_body_and_notice_verbatim(self):
        output = self.root / 'notes.md'
        notes.export_notes(self.root, self.version, output)
        self.assertEqual(output.read_text(), '请同步升级。\n\n### Changed #12\n\nTwo lines.\n')
        self.commit(self.manifest + f'notes:\n  aggregate_sha: stale\n  body: |-\n    Exact prose.  ')
        notes.export_notes(self.root, self.version, output)
        self.assertEqual(output.read_text(), 'Exact prose.  ')

    def test_export_missing_body_and_oversize_fail_without_writing(self):
        output = self.root / 'notes.md'
        output.write_text('unchanged')
        for block in ('', 'notes:\n  upgrade_notice: |-\n    Warning only.'):
            self.commit(self.manifest + block)
            with self.assertRaisesRegex(ValueError, 'missing|body|text'):
                notes.export_notes(self.root, self.version, output)
        self.commit(self.manifest + 'notes:\n  mode: fixed\n  body: |-\n    ' + 'x' * (body.MAX_CHARACTERS + 1))
        with self.assertRaisesRegex(ValueError, 'must not be truncated'):
            notes.export_notes(self.root, self.version, output)
        self.assertEqual(output.read_text(), 'unchanged')

    def test_bound_validation_does_not_compare_prefix_source_or_metadata(self):
        output = self.root / 'notes.md'
        suffix = '\n' + body.MARKER + '{} -->\n'
        for prefix in ('Different human prose', '# Kuasar Sandbox release-v0.0.1\nOld component wording.'):
            output.write_text(prefix + suffix)
            with patch.object(notes, 'publication_notes', side_effect=AssertionError('no manifest gate')):
                notes.validate_file(self.root / 'missing-source', self.version, output)

    def test_selection_fingerprint_is_optional_non_authoritative_metadata(self):
        for mode in ('auto', 'fixed'):
            block = f'notes:\n  mode: {mode}\n  selection_sha256: ' + '0' * 64 + '\n  body: |-\n    Exact body.'
            fields = notes.selection.read_simple_yaml(self.manifest + block, 'manifest')['notes']
            self.assertEqual(fields['selection_sha256'], '0' * 64)
            self.assertEqual(notes.Narrative(self.text, fields).text, 'Exact body.')
            self.commit(self.manifest + block)
            output = self.root / 'exported.md'
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

    def test_latest_committed_notes_win_over_old_source_and_worktree(self):
        latest = self.commit(self.manifest + self.block.replace('Two lines.', 'New notes.'))
        self.path.write_text('uncommitted')
        fields, url = notes.publication_notes(self.root, self.version)
        self.assertEqual(fields['body'], '### Changed #12\n\nNew notes.\n')
        self.assertIn(latest + '/releases/release.yaml#L', url)

    def test_historical_routing_uses_newest_matching_first_parent_snapshot(self):
        for channel, suffix in (('release', ''), ('daily-preview', '-preview.20990101')):
            self.path = self.root / 'releases' / (channel + '.yaml')
            selected = self.manifest + ('preview_version: preview.20990101\n' if suffix else '')
            self.commit(selected + 'notes:\n  body: |-\n    Original.')
            newest = self.commit(selected + 'notes:\n  body: |-\n    Newest matching prose.  ')
            moved = (selected.replace('preview.20990101', 'preview.20990102') if suffix
                     else selected.replace(self.version, 'release-v9.8.8'))
            self.commit(moved + 'notes:\n  body: |-\n    Another release.')
            fields, url = notes.publication_notes(self.root, self.version + suffix)
            self.assertEqual(fields['body'], 'Newest matching prose.  ')
            self.assertIn(newest + '/releases/' + channel + '.yaml#L', url)
            self.assertEqual(notes.publication_notes(self.root, 'release-v0.0.0' + suffix), ({}, None))

    def test_latest_matching_without_notes_does_not_borrow_older_or_fallback_prose(self):
        for channel, suffix in (('release', ''), ('daily-preview', '-preview.20990101')):
            self.path = self.root / 'releases' / (channel + '.yaml')
            selected = self.manifest + ('preview_version: preview.20990101\n' if suffix else '')
            if suffix:
                self.commit(selected + self.block)
            fallback = self.root / (channel + '-fallback')
            subprocess.run(['git', 'clone', '-q', str(self.root), str(fallback)], check=True)
            self.commit(selected)
            self.commit(selected.replace(self.version, 'release-v9.8.8') + self.block)
            self.assertEqual(notes.publication_notes(self.root, self.version + suffix, fallback), ({}, None))

    def test_missing_publisher_version_uses_optional_platform_history(self):
        fallback = self.root / 'fallback'
        subprocess.run(['git', 'clone', '-q', str(self.root), str(fallback)], check=True)
        self.git('checkout', '--orphan', 'publisher')
        self.git('rm', '-rf', 'releases')
        (self.root / 'releases').mkdir()
        self.commit(self.manifest.replace(self.version, 'release-v10.0.0') + self.block)
        fields, url = notes.publication_notes(self.root, self.version, fallback)
        self.assertEqual(fields['body'], '### Changed #12\n\nTwo lines.\n')
        self.assertIn(self.revision, url)
        self.assertEqual(notes.publication_notes(self.root, self.version), ({}, None))

    def test_merged_side_branch_notes_do_not_override_first_parent_history(self):
        original = self.revision
        self.git('branch', 'side')
        self.commit(self.manifest.replace(self.version, 'release-v10.0.0') + self.block)
        self.git('checkout', '-q', 'side')
        self.commit(self.manifest + self.block.replace('Two lines.', 'Side branch prose.'))
        self.git('checkout', '-q', '-')
        self.git('merge', '-q', '--no-ff', '-s', 'ours', 'side', '-m', 'merge side without prose')
        fields, url = notes.publication_notes(self.root, self.version)
        self.assertEqual(fields['body'], '### Changed #12\n\nTwo lines.\n')
        self.assertIn(original, url)

    def test_preview_is_independent_and_absent_notes_keep_generated_text(self):
        self.path = self.path.with_name('daily-preview.yaml')
        preview = 'preview.20990101'
        sha = self.commit(self.manifest + 'preview_version: ' + preview + '\n')
        fields, url = notes.publication_notes(self.root, self.version + '-' + preview)
        self.assertEqual(notes.Narrative(self.text, fields, url).text, self.text)
        for field in ('body', 'upgrade_notice'):
            sha = self.commit(self.manifest + 'preview_version: ' + preview +
                              '\nnotes:\n  ' + field + ': |\n    Preview only.\n')
            fields, _ = notes.publication_notes(self.root, self.version + '-' + preview)
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

    def test_authored_body_and_notice_never_shrink_to_a_manifest_link(self):
        record = self.binding()
        original = copy.deepcopy(record)
        fields = {'body': 'Long body. ' * 2000, 'upgrade_notice': '请同步升级。  '}
        narrative = notes.Narrative(self.text, fields, 'https://example.invalid/immutable')
        expected = fields['upgrade_notice'] + '\n\n' + fields['body']
        self.assertEqual(narrative.text, expected)
        self.assertEqual(narrative.fit(expected, 2500), expected)
        with patch.object(body, 'MAX_CHARACTERS', 2500), self.assertRaisesRegex(ValueError, 'must not be truncated'):
            body.binding_text(narrative.text + '\n', record, '\n',
                reserve_registry_version=self.version, fit_notes=narrative.fit)
        self.assertEqual(record, original)

    def test_notice_only_keeps_warning_and_stage_text_without_title_contract(self):
        warning = 'Upgrade both components.  \n请保留。'
        staged = 'Arbitrary staged prose without a generated title.  \n'
        narrative = notes.Narrative(staged, {'upgrade_notice': warning}, 'url')
        expected = warning + '\n\n' + staged
        self.assertEqual(narrative.text, expected)
        self.assertEqual(narrative.fit(expected, 1), expected)

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

    def test_no_fields_staged_fallback_never_compacts_or_replaces_prefix(self):
        row = '- `aaaaaaaaaaaa` title ([commit](https://github.com/kuasar-sandbox/sandboxer/commit/' + 'a' * 40 + '))\n'
        staged = ('### sandboxer\nSelected version: `v1` → `v2`.\n' + row * 100 +
                  '[Full comparison](https://github.com/kuasar-sandbox/sandboxer/compare/v1...v2)\n')
        narrative = notes.Narrative(staged, {}, None)
        self.assertEqual(narrative.text, staged)
        with patch.object(notes, 'compact_commits', side_effect=AssertionError('never rewrite staged prose')):
            self.assertEqual(narrative.fit(staged, 1), staged)
            with patch.object(body, 'MAX_CHARACTERS', 2500), self.assertRaisesRegex(ValueError, 'must not be truncated'):
                body.binding_text(staged, self.binding(), '\n', fit_notes=narrative.fit)

    def test_compatibility_cli_checks_size_without_rewriting_any_text(self):
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
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('must not be truncated', result.stderr)
            self.assertEqual(path.read_text(), text)
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

    def test_arbitrary_human_text_never_bypasses_machine_corruption(self):
        binder = test_workbench_binding.binder
        for corruption in ('validation', 'source-records', 'workbench'):
            validation, results = copy.deepcopy(self.validation), copy.deepcopy(self.results)
            source = self.root / 'source-records.json'
            original = source.read_text()
            if corruption == 'validation':
                validation['architectures']['x86_64']['plan_id'] = '0' * 64
            elif corruption == 'source-records':
                source.write_text('{}')
            else:
                results['x86_64']['image_id'] = 'sha256:' + '0' * 64
            try:
                with self.subTest(corruption=corruption), patch.object(notes, 'publication_notes',
                        return_value=({'body': 'Any human text.'}, 'url')):
                    with self.assertRaises(ValueError):
                        binder.bind(self.root, self.plan, validation, results, platform=self.root)
                self.assertEqual((self.root / 'release-notes.md').read_text(), 'Original notes\n')
            finally:
                source.write_text(original)

    def test_missing_manifest_notes_preserve_stage_text_without_source_age_gate(self):
        binder = test_workbench_binding.binder
        with patch.object(notes, 'publication_notes', return_value=({}, None)) as selected:
            binder.bind(self.root, self.plan, self.validation, self.results, platform=self.root)
        selected.assert_called_once_with(notes.ROOT, self.version, self.root)
        result = (self.root / 'release-notes.md').read_text()
        self.assertEqual(result.split(body.MARKER)[0], 'Original notes\n\n')
        restored = json.loads(result.split(body.MARKER)[1].split(' -->')[0])
        self.assertEqual(restored['validation_plan'], self.plan)
        self.assertEqual(restored['workbench'], self.results)

    def test_missing_mode_or_stale_bookkeeping_does_not_gate_binding(self):
        binder = test_workbench_binding.binder
        for fields in ({'body': 'Full human text.'},
                       {'mode': 'auto', 'selection_sha256': '0' * 64, 'body': 'Full human text.'}):
            (self.root / 'release-notes.md').write_text('Original notes\n')
            with self.subTest(fields=fields), patch.object(notes, 'publication_notes', return_value=(fields, 'url')):
                binder.bind(self.root, self.plan, self.validation, self.results, platform=self.root)
            result = (self.root / 'release-notes.md').read_text()
            self.assertEqual(result.split(body.MARKER)[0], 'Full human text.\n')
            self.assertEqual(json.loads(result.split(body.MARKER)[1].split(' -->')[0])['validation_plan'], self.plan)


if __name__ == '__main__':
    unittest.main()
