#!/usr/bin/env python3
"""Real documentation assembly and tar consumption with private source fixtures."""
from contextlib import redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('source_documentation',
                                            Path(__file__).with_name('check-source-documentation.py'))
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)


class RequiredTests(unittest.TestCase):
    def plan(self, owner, paths):
        return {'mode': 'source', 'source_scope': {'schema': 1, 'sources': [],
                                                  'changes': {owner: sorted(paths)}}}

    def test_all_owner_metadata_and_platform_delivery_implementations_require_real_assembly(self):
        for owner in subject.OWNERS:
            for path in subject.GUIDE_METADATA:
                with self.subTest(owner=owner, path=path):
                    self.assertTrue(subject.required(self.plan(owner, [path])))
        for path in subject.PLATFORM_DELIVERY:
            with self.subTest(path=path):
                self.assertTrue(subject.required(self.plan('platform', [path])))
        self.assertFalse(subject.required(self.plan('connector', ['docs/topic.md'])))
        self.assertFalse(subject.required(self.plan('platform', ['test/demo/test_example.py'])))
        self.assertFalse(subject.required({'mode': 'source'}))

    def test_malformed_scope_cannot_silently_skip_a_required_check(self):
        valid = self.plan('platform', ['release/guide-migrations.json'])
        for mutation in (
            lambda value: value.update(mode='exact-assets'),
            lambda value: value['source_scope'].update(schema=True),
            lambda value: value['source_scope'].update(changes={'unknown': []}),
            lambda value: value['source_scope'].update(changes={'platform': ['x', 'x']}),
            lambda value: value['source_scope'].update(changes={'platform': [None]}),
        ):
            value = copy.deepcopy(valid)
            mutation(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                subject.required(value)


class FencedCodeTests(unittest.TestCase):
    def test_fence_contract_retains_delimiters_info_whitespace_and_literal_nested_runs(self):
        first = ('   ````yaml title="literal"\nvalue: 256  \n```\n~~~\n'
                 '````still-body\n   ````` \t\n')
        second = '~~~sh\necho "[literal](not-a-link.md)"\n~~~~\n'
        text = '[Before](before.md)\n\n' + first + '\n[Between](between.md)\n' + second
        self.assertEqual(subject.fenced_code(text), [first, second])
        changed_prose = text.replace('[Before](before.md)', '[After](after.md)').replace(
            '[Between](between.md)', 'Historical release navigation notice.')
        self.assertEqual(subject.fenced_code(changed_prose), [first, second])
        self.assertEqual(subject.fenced_code(text.replace('\n', '\r\n')), [first, second])
        self.assertNotEqual(subject.fenced_code(text.replace('yaml title=', 'json title=')), [first, second])
        self.assertNotEqual(subject.fenced_code(text.replace('value: 256  ', 'value: 256')), [first, second])

    def test_unclosed_fences_keep_the_remaining_literal_content(self):
        block = '```python\nprint("unclosed, still literal")\n'
        self.assertEqual(subject.fenced_code('Outside prose\n' + block), [block])


@unittest.skipUnless(shutil.which('git'), 'Git is required for exact source fixtures')
class SelectedDocumentationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sources = self.root / 'sources'
        self.sources.mkdir()
        self.output = self.root / 'documentation-result.json'
        self.marker = self.root / 'installation-was-executed'
        self.plan = {'mode': 'source', 'test_revisions': {},
                     'source_scope': {'schema': 1, 'sources': [],
                                      'changes': {'platform': ['release/inputs/guide-inputs.yaml']}}}
        for owner in subject.OWNERS:
            source = self.sources / owner
            (source / 'docs').mkdir(parents=True)
            (source / 'release').mkdir()
            for suffix in ('', '_zh'):
                (source / f'README{suffix}.md').write_text(
                    '[English](README.md) | [简体中文](README_zh.md)\n\n# ' + owner + '\n\n'
                    f'[Topic](docs/topic{suffix}.md)\n')
                (source / f'docs/topic{suffix}.md').write_text(
                    '[English](topic.md) | [简体中文](topic_zh.md)\n\n# Topic\n\n'
                    f'[Repository](../README{suffix}.md)\n' +
                    ('\n```yaml\nlimit: 256\nreference: "[literal](not-a-link.md)"\n```\n'
                     if owner == 'connector' else ''))
            (source / 'LICENSE').write_bytes(b'Fixture legal text must remain byte-identical.\n')
            (source / 'release/guide-inputs.txt').write_text('README.md\nREADME_zh.md\ndocs/topic.md\ndocs/topic_zh.md\n')
            self.git(owner, 'init', '-q')
        platform = self.sources / 'platform'
        for path in ('test/e2e/assemble_docs.py', 'test/e2e/package_inputs.py',
                     'ci/integration/check-published-download.py'):
            destination = platform / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, destination)
        # The selected source's checker must not be used to validate its output.
        (platform / 'ci/check_docs.py').write_text('def check_file(*args): return []\n')
        guides = ['README.md', 'README_zh.md', 'docs/topic.md', 'docs/topic_zh.md']
        for suffix in ('', '_zh'):
            path = f'docs/installation{suffix}.md'
            guides.append(path)
            (platform / path).write_text(
                '[English](installation.md) | [简体中文](installation_zh.md)\n\n# Installation\n\n'
                '```bash\n' + f'touch {self.marker}\n' + '```\n\n'
                '```bash\ndocker run fixture-that-must-not-run\n```\n\n'
                '```bash\ncurl https://invalid.invalid/not-a-download\n```\n')
        declaration = platform / 'release/inputs/guide-inputs.yaml'
        declaration.parent.mkdir()
        declaration.write_text(json.dumps(guides))
        (platform / 'README.md').write_text((platform / 'README.md').read_text() +
            '\n[Connector topic](https://github.com/kuasar-sandbox/connector/blob/main/docs/topic.md#topic)\n')
        for owner in subject.OWNERS:
            self.commit(owner)

    def git(self, owner, *args):
        command = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                   '-c', 'user.name=Documentation Fixture', '-c', 'user.email=fixture@example.invalid',
                   '-C', str(self.sources / owner), *args]
        return subprocess.check_output(command, stderr=subprocess.PIPE).decode().strip()

    def commit(self, owner):
        self.git(owner, 'add', '--', '.')
        self.git(owner, 'commit', '-qm', 'Documentation source fixture')
        sha = self.git(owner, 'rev-parse', 'HEAD')
        self.plan['test_revisions'][owner] = {
            'repository': 'kuasar-sandbox/' + ('kuasar-sandbox' if owner == 'platform' else owner),
            'sha': sha, 'role': 'candidate' if owner == 'platform' else 'baseline'}

    def execute(self, *, success=True):
        original = subprocess.run
        calls = []
        def only_git_reads(command, *args, **kwargs):
            calls.append(command)
            self.assertIsInstance(command, list)
            self.assertEqual(command[0], 'git', 'documentation checking must not install or download')
            self.assertIn(command[3], ('rev-parse', 'diff', 'ls-tree'))
            return original(command, *args, **kwargs)
        with patch.object(subprocess, 'run', side_effect=only_git_reads), redirect_stdout(io.StringIO()):
            if success:
                result = subject.check(self.plan, self.sources, self.output)
            else:
                with self.assertRaises((Exception, SystemExit)):
                    subject.check(self.plan, self.sources, self.output)
                result = json.loads(self.output.read_text())
        self.assertFalse(self.marker.exists())
        self.assertEqual(result, json.loads(self.output.read_text()))
        self.assertEqual(result['conclusion'], 'success' if success else 'failure')
        return result

    def append_assembler(self, code):
        path = self.sources / 'platform/test/e2e/assemble_docs.py'
        path.write_text(path.read_text() + '\n' + code + '\n')
        self.commit('platform')

    def test_real_archive_preserves_sources_legal_bytes_and_both_installation_scripts(self):
        result = self.execute()
        self.assertEqual(set(result['sources']), set(subject.OWNERS))
        self.assertEqual({owner: row['sha'] for owner, row in result['sources'].items()},
                         {owner: row['sha'] for owner, row in self.plan['test_revisions'].items()})
        self.assertEqual(result['plan_id'], hashlib.sha256(subject.canonical(self.plan)).hexdigest())
        self.assertEqual(result['archive']['members'], len(result['files']))
        self.assertEqual(result['markdown_files'], 26)
        self.assertEqual(result['fenced_code_blocks'], 8)
        self.assertEqual(result['findings'], [])
        self.assertTrue(result['installation_languages_match'])
        for language in ('en', 'zh'):
            self.assertEqual(result['installation'][language]['blocks'], 3)
            self.assertTrue(result['installation'][language]['matches_selected_source'])
            self.assertFalse(result['installation'][language]['commands_executed'])
        for row in result['files']:
            if row['source'].endswith('.md'):
                self.assertTrue(row['fenced_code_preserved'])
                self.assertEqual(row['source_fenced_code_sha256'], row['package_fenced_code_sha256'])
                if row['owner'] == 'connector' and row['source'] == 'docs/topic.md':
                    # The real assembler relocates navigation outside the literal block.
                    self.assertNotEqual(row['source_sha256'], row['package_sha256'])
                    self.assertEqual(row['fenced_code_blocks'], 1)
            else:
                self.assertEqual(row['source_sha256'], row['package_sha256'])

    def test_old_download_paths_work_with_the_selected_legacy_consumer(self):
        platform = self.sources / 'platform'
        for suffix in ('', '_zh'):
            old = platform / f'docs/installation{suffix}.md'
            new = platform / f'docs/download{suffix}.md'
            new.write_text(old.read_text().replace('installation.md', 'download.md').replace(
                'installation_zh.md', 'download_zh.md'))
            old.unlink()
        declaration = platform / 'release/inputs/guide-inputs.yaml'
        paths = json.loads(declaration.read_text())
        declaration.unlink()
        (platform / 'release/guide-inputs.txt').write_text('\n'.join(path.replace('installation', 'download')
                                                                    for path in paths) + '\n')
        consumer = platform / 'ci/integration/check-published-download.py'
        consumer.write_text(consumer.read_text().replace("('installation', 'download')", "('download',)"))
        self.commit('platform')
        result = self.execute()
        self.assertEqual(result['installation']['en']['source'], 'docs/download.md')
        self.assertEqual(result['installation']['zh']['source'], 'docs/download_zh.md')

    def test_component_metadata_change_still_validates_all_selected_sources(self):
        self.plan['source_scope']['changes'] = {'connector': ['release/guide-inputs.txt']}
        source = self.sources / 'connector'
        (source / 'docs/order').mkdir()
        (source / 'docs/order.md').write_text('# Order\n')
        (source / 'docs/order/page.md').write_text('# Nested\n')
        declaration = source / 'release/guide-inputs.txt'
        declaration.write_text(declaration.read_text() + 'docs/order.md\ndocs/order/page.md\n')
        self.commit('connector')
        result = self.execute()
        self.assertIn('guide/connector/order/page.md', [row['destination'] for row in result['files']])
        self.assertEqual(set(result['sources']), set(subject.OWNERS))

    def test_command_corruption_by_assembler_is_detected_before_any_execution(self):
        self.append_assembler('''_original_assemble = assemble
def assemble(output, roots, refs, kernel=None):
    _original_assemble(output, roots, refs, kernel)
    path = output / 'guide/installation.md'
    path.write_text(path.read_text().replace('docker run fixture', 'docker run altered'))
''')
        result = self.execute(success=False)
        self.assertIn('fenced code changed', result['error'])
        self.assertIn('guide/installation.md', result['error'])

    def test_non_installation_fenced_body_and_language_cannot_be_changed(self):
        assembler = self.sources / 'platform/test/e2e/assemble_docs.py'
        original = assembler.read_text()
        for before, after in (('limit: 256', 'limit: 512'), ('```yaml', '```json')):
            with self.subTest(before=before, after=after):
                assembler.write_text(original)
                self.append_assembler('''_original_assemble = assemble
def assemble(output, roots, refs, kernel=None):
    _original_assemble(output, roots, refs, kernel)
    path = output / 'guide/connector/topic.md'
    path.write_text(path.read_text().replace(''' + repr(before) + ', ' + repr(after) + '''))
''')
                result = self.execute(success=False)
                self.assertIn('fenced code changed', result['error'])
                self.assertIn('guide/connector/topic.md', result['error'])
                row = next(row for row in result['files'] if row['destination'] == 'guide/connector/topic.md')
                self.assertFalse(row['fenced_code_preserved'])
                self.assertNotEqual(row['source_fenced_code_sha256'], row['package_fenced_code_sha256'])

    def test_fenced_source_snapshot_precedes_candidate_assembly(self):
        self.append_assembler('''_original_assemble = assemble
def assemble(output, roots, refs, kernel=None):
    path = roots['connector'] / 'docs/topic.md'
    path.write_text(path.read_text().replace('limit: 256', 'limit: 512'))
    _original_assemble(output, roots, refs, kernel)
''')
        result = self.execute(success=False)
        self.assertIn('fenced code changed', result['error'])
        self.assertIn('guide/connector/topic.md', result['error'])

    def test_selected_archive_consumer_cannot_rewrite_installation_commands(self):
        consumer = self.sources / 'platform/ci/integration/check-published-download.py'
        consumer.write_text(consumer.read_text() + '''
_original_guide_script = guide_script
def guide_script(archive, language):
    return _original_guide_script(archive, language).replace('docker run fixture', 'docker run altered')
''')
        self.commit('platform')
        result = self.execute(success=False)
        self.assertIn('installation commands differ', result['error'])
        self.assertTrue(all(row['fenced_code_preserved'] for row in result['files']
                            if row['source'].endswith('.md')))

    def test_trusted_checker_rejects_broken_assembled_navigation(self):
        self.append_assembler('''_original_assemble = assemble
def assemble(output, roots, refs, kernel=None):
    _original_assemble(output, roots, refs, kernel)
    path = output / 'guide/README.md'
    path.write_text(path.read_text() + '\\n[Broken](missing-output.md)\\n')
''')
        result = self.execute(success=False)
        self.assertIn('navigation checks failed', result['error'])
        self.assertTrue(any('missing-output.md' in finding for finding in result['findings']))

    def test_extra_output_file_is_not_accepted_as_a_selected_input(self):
        self.append_assembler('''_original_assemble = assemble
def assemble(output, roots, refs, kernel=None):
    _original_assemble(output, roots, refs, kernel)
    (output / 'guide/unselected.md').write_text('# Unselected\\n')
''')
        result = self.execute(success=False)
        self.assertIn('inventory differs', result['error'])

    def test_wrong_head_dirty_inputs_and_missing_owner_fail_with_evidence(self):
        self.plan['test_revisions']['connector']['sha'] = '0' * 40
        result = self.execute(success=False)
        self.assertIn('HEAD mismatch', result['error'])
        self.plan['test_revisions']['connector']['sha'] = self.git('connector', 'rev-parse', 'HEAD')
        path = self.sources / 'connector/README.md'
        path.write_text(path.read_text() + '\nUncommitted input\n')
        result = self.execute(success=False)
        self.assertIn('diff', result['error'])
        self.commit('connector')
        shutil.rmtree(self.sources / 'guest-runtime')
        result = self.execute(success=False)
        self.assertIn('missing private documentation owner', result['error'])

    def test_selected_input_cannot_be_an_untracked_file(self):
        source = self.sources / 'connector'
        declaration = source / 'release/guide-inputs.txt'
        declaration.write_text('README.md\nREADME_zh.md\ndocs/\n')
        self.commit('connector')
        (source / 'docs/untracked.md').write_text('# Untracked\n')
        result = self.execute(success=False)
        self.assertIn('differs from selected Git blob', result['error'])

    def test_failed_cli_returns_nonzero_and_persists_failure_record(self):
        self.plan['test_revisions']['platform']['sha'] = '0' * 40
        plan = self.root / 'plan.json'
        plan.write_text(json.dumps(self.plan))
        result = subprocess.run([sys.executable, '-B', str(Path(subject.__file__)),
                                 '--plan', str(plan), '--sources', str(self.sources), '--output', str(self.output)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.output.read_text())['conclusion'], 'failure')
        self.assertFalse(self.marker.exists())


if __name__ == '__main__':
    unittest.main()
