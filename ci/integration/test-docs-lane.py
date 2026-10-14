#!/usr/bin/env python3
"""Exact metadata classification, shared document semantics and fail-closed evidence."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import docs_lane as subject

ROOT = Path(__file__).resolve().parents[2]
REPO = 'kuasar-sandbox/kuasar-sandbox'


def entry(text='', mode='100644', kind='blob'):
    raw = text.encode('utf-8') if isinstance(text, str) else text
    return dict(mode=mode, type=kind, sha=hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest())


def tree(entries=None, truncated=False):
    return dict(sha='a' * 40, entries=entries or {}, truncated=truncated)


class DocsLaneTests(unittest.TestCase):
    def test_all_six_repositories_and_explicit_whitelist(self):
        for repo in subject.REPOSITORIES:
            for path in ('README.md', 'README_zh.md', 'test/e2e/README.md', 'test/e2e/README_zh.md',
                         'docs/topic.md', 'docs/nested/topic_zh.md', 'test/e2e/guides/topic.md'):
                with self.subTest(repo=repo, path=path):
                    self.assertEqual(subject.classify(repo, tree(), tree({path: entry()})), [path])
            for path in subject.PLATFORM:
                self.assertEqual(subject.allowed(repo, path), repo == REPO)

    def test_mixed_unknown_policy_workflow_modes_and_links_fall_back(self):
        for path in ('src/code.go', 'docs/example.sh', 'docs/image.svg', '.github/workflows/ci.yml',
                     'ci/check_docs.py', 'ci/integration/docs_lane.py', 'notes.md', 'test/README.md',
                     'docs/topic.MD', '../docs/topic.md'):
            with self.subTest(path=path):
                self.assertIsNone(subject.classify(REPO, tree(), tree({'README.md': entry(), path: entry()})))
        self.assertIsNone(subject.classify('unknown/repository', tree(), tree({'README.md': entry()})))
        for mode, kind in (('100755', 'blob'), ('120000', 'blob'), ('160000', 'commit')):
            for old, new in ((None, entry(mode=mode, kind=kind)), (entry(), entry(mode=mode, kind=kind)),
                             (entry(mode=mode, kind=kind), None), (entry(mode=mode, kind=kind), entry())):
                with self.subTest(mode=mode, old=old, new=new):
                    self.assertIsNone(subject.classify(REPO, tree({'README.md': old} if old else {}),
                                                       tree({'README.md': new} if new else {})))

    def test_full_tree_comparison_has_no_pr_file_count_limit(self):
        before = tree({f'docs/{n}.md': entry('old') for n in range(3500)})
        after = tree({f'docs/{n}.md': entry('new') for n in range(3500)})
        self.assertEqual(len(subject.classify(REPO, before, after)), 3500)
        after['entries']['z-last.go'] = entry('code')
        self.assertIsNone(subject.classify(REPO, before, after))
        for item in (before, after):
            item['truncated'] = True
            self.assertIsNone(subject.classify(REPO, before, after))

    def test_missing_exact_tree_fails_and_empty_diff_is_not_evidence(self):
        with self.assertRaises(ValueError):
            subject.classify(REPO, dict(sha=None), tree())
        self.assertEqual(subject.classify(REPO, tree(), tree()), [])
        for response in ({}, dict(sha='b' * 40, tree=dict(sha='a' * 40))):
            with patch.object(subject, 'api', return_value=response):
                with self.assertRaises(ValueError):
                    subject.tree(REPO, 'a' * 40)
        with patch.object(subject, 'api', side_effect=[dict(sha='a' * 40, tree=dict(sha='b' * 40)), {}]):
            with self.assertRaises(ValueError):
                subject.tree(REPO, 'a' * 40)

    def check(self, files, changed):
        entries = {name: entry(text) for name, text in files.items()}
        contents = {entries[name]['sha']: text.encode('utf-8') if isinstance(text, str) else text for name, text in files.items()}
        with patch.object(subject, 'blob', side_effect=lambda repo, identity: contents[identity]):
            return subject.check_tree(REPO, tree(entries), changed)

    def test_english_only_and_reciprocal_pair_share_existing_semantics(self):
        self.assertEqual(self.check({'docs/one.md': '# English\n'}, ['docs/one.md'])['findings'], [])
        pair = {'docs/one.md': '[English](one.md) | [简体中文](one_zh.md)\n# English\n',
                'docs/one_zh.md': '[English](one.md) | [简体中文](one_zh.md)\n# 中文\n'}
        self.assertEqual(self.check(pair, ['docs/one.md'])['findings'], [])
        pair['docs/one.md'] = '# English\n'
        self.assertTrue(self.check(pair, ['docs/one.md'])['findings'])
        self.assertTrue(self.check({'docs/one_zh.md': '# 中文\n'}, ['docs/one_zh.md'])['findings'])
        self.assertTrue(self.check({'docs/one.md': '# 中文\n'}, ['docs/one.md'])['findings'])

    def test_rename_delete_and_heading_changes_check_unchanged_reverse_references(self):
        for files, changed in [
            ({'README.md': '[guide](docs/old.md)\n', 'docs/new.md': '# Guide\n'}, ['docs/old.md', 'docs/new.md']),
            ({'README.md': '[guide][ref]\n[ref]: docs/old.md\n'}, ['docs/old.md']),
            ({'README.md': '[guide](docs/guide.md#before)\n', 'docs/guide.md': '# After\n'}, ['docs/guide.md'])]:
            with self.subTest(changed=changed):
                result = self.check(files, changed)
                self.assertIn('README.md', result['checked_files'])
                self.assertTrue(any('README.md:' in finding for finding in result['findings']))
        self.assertEqual(self.check({'README.md': '[guide](docs/new.md#after)\n', 'docs/new.md': '# After\n'},
                                    ['docs/old.md', 'docs/new.md'])['findings'], [])

    def test_local_files_anchors_utf8_and_unrelated_debt(self):
        self.assertEqual(self.check({'docs/a.md': '[source](../src/tool.py)\n# One\n[anchor](#one)\n',
                                    'src/tool.py': 'code'}, ['docs/a.md'])['findings'], [])
        self.assertTrue(self.check({'docs/a.md': '[missing](no.md)\n'}, ['docs/a.md'])['findings'])
        with self.assertRaises(UnicodeError):
            self.check({'docs/a.md': b'\xff'}, ['docs/a.md'])
        self.assertEqual(self.check({'docs/a.md': '# Valid\n', 'docs/unrelated.md': '[missing](no.md)\n'},
                                   ['docs/a.md'])['findings'], [])

    def test_all_companions_must_classify_and_all_identities_are_read(self):
        records = [dict(repository=repo, base_sha='b' * 40, head_sha='c' * 40, candidate_sha='d' * 40)
                   for repo in sorted(subject.REPOSITORIES)]
        def get_tree(repo, commit):
            return tree({} if commit == 'b' * 40 else {'README.md': entry()})
        with patch.dict(os.environ, GITHUB_RUN_ID='1', GITHUB_RUN_ATTEMPT='1'), \
             patch.object(subject, 'tree', side_effect=get_tree) as reader, \
             patch.object(subject, 'check_tree', return_value=dict(checked_files=['README.md'], findings=[])):
            result = subject.qualify(records, 'e' * 40)
            self.assertEqual(result['lane'], 'docs')
            self.assertEqual(reader.call_count, 18)
            subject.verify(result, records, 'e' * 40)
        def mixed(repo, commit):
            if repo == records[-1]['repository'] and commit != 'b' * 40:
                return tree({'main.go': entry()})
            return get_tree(repo, commit)
        with patch.dict(os.environ, GITHUB_RUN_ID='1', GITHUB_RUN_ATTEMPT='1'), \
             patch.object(subject, 'tree', side_effect=mixed), patch.object(subject, 'check_tree') as checker:
            self.assertEqual(subject.qualify(records, 'e' * 40)['lane'], 'full')
            checker.assert_not_called()

    def test_missing_failed_cancelled_empty_and_stale_results_fail(self):
        records = [dict(repository=REPO, base_sha='b' * 40, head_sha='c' * 40, candidate_sha='d' * 40)]
        valid = dict(schema=1, lane='docs', conclusion='success', sources=records, policy_sha='e' * 40,
                     run_id='1', run_attempt='2', repositories=[dict(repository=REPO, base_tree='a' * 40,
                     candidate_tree='b' * 40, head_tree='c' * 40, changed=['README.md'], checked_files=['README.md'], findings=[])])
        with patch.dict(os.environ, GITHUB_RUN_ID='1', GITHUB_RUN_ATTEMPT='2'):
            subject.verify(valid, records, 'e' * 40)
            for key, values in [('lane', ('full', '')), ('conclusion', ('failure', 'cancelled', 'skipped', '')),
                                ('sources', ([],)), ('repositories', ([],)), ('run_id', ('2', '')),
                                ('run_attempt', ('1', '')), ('policy_sha', ('f' * 40, ''))]:
                for value in values:
                    with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                        subject.verify(dict(valid, **{key: value}), records, 'e' * 40)
            for key, value in [('changed', []), ('changed', ['main.go']), ('findings', ['broken']), ('head_tree', '')]:
                changed = copy.deepcopy(valid); changed['repositories'][0][key] = value
                with self.subTest(key=key), self.assertRaises(ValueError):
                    subject.verify(changed, records, 'e' * 40)

    def test_workflow_uses_baseline_before_downloads_and_requires_bound_evidence(self):
        import yaml
        workflow = yaml.safe_load((ROOT / '.github/workflows/integration-tests.yml').read_text())
        jobs = workflow['jobs']; steps = jobs['resolve']['steps']
        names = [s.get('name') for s in steps]
        classify = names.index('Classify exact trees and check documentation before large downloads')
        self.assertLess(classify, names.index('Download staged aggregate'))
        self.assertLess(classify, names.index('Resolve exact immutable plan'))
        checkout = next(s for s in steps if s.get('name') == 'Check out documentation executor from trusted baseline')
        self.assertEqual(checkout['with']['ref'], '${{ steps.docs-policy.outputs.sha }}')
        selection = next(s['run'] for s in steps if s.get('id') == 'docs-policy')
        self.assertIn("os.environ['CANDIDATE_BASE_SHA']", selection)
        self.assertIn("policy = row['base_sha']", selection)
        stages = jobs['results']['steps'][0]['run']
        good = dict(DOCS_LANE='docs', INTEGRATION_MODE='source', RESOLVE_RESULT='success', X86_RESULT='skipped',
                    ARM_RESULT='skipped', SOURCE_RESULT='skipped', WORKBENCH_RESULT='skipped',
                    WORKBENCH_RELEASE_RESULT='skipped', WORKBENCH_SELECTED='false')
        self.assertEqual(subprocess.run(['bash', '-c', stages], env={**os.environ, **good}).returncode, 0)
        for key in good:
            for value in ('', 'cancelled', 'failure'):
                with self.subTest(key=key, value=value):
                    self.assertNotEqual(subprocess.run(['bash', '-c', stages], env={**os.environ, **good, key:value},
                                                       capture_output=True).returncode, 0)
        verifier = next(s for s in jobs['results']['steps'] if s.get('name') == 'Bind docs success to this exact source set and attempt')
        self.assertIn('--verify plan/docs-result.json', verifier['run'])
        self.assertIn('Recheck the current exact source candidate before acceptance',
                      [s.get('name') for s in jobs['results']['steps']])


if __name__ == '__main__':
    unittest.main()
