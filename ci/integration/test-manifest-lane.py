#!/usr/bin/env python3
"""Manifest qualification is planning evidence, not future release acceptance."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch

import docs_lane
import manifest_lane as subject

ROOT = Path(__file__).resolve().parents[2]
REPO = 'kuasar-sandbox/kuasar-sandbox'
SHA = 'a' * 40
ENV = dict(GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1')


def entry(text='', mode='100644', kind='blob'):
    return dict(mode=mode, type=kind, sha=hashlib.sha1(b'blob ' + str(len(text.encode())).encode() + b'\0' + text.encode()).hexdigest())


def tree(files=None, truncated=False):
    return dict(sha=SHA, entries=files or {}, truncated=truncated)


def manifest(preview=False):
    return ('version: release-v1.2.3\ndelivery: workbench-v1\nprevious_version: release-v1.2.2\n' +
            ('preview_version: preview.20261026\n' if preview else '') + 'components:\n' +
            ''.join(f'  {name}: {name + "-" if name in ("runtime", "vmlinux") else ""}v1.2.3\n'
                    for name in subject.selection.UNITS))


class FakeReferences:
    def __init__(self):
        self.published = {}
        self.aggregates = {'release-v1.2.2': dict(sha='b' * 40)}
        self.trees = {}

    def unit(self, name, tag):
        return self.published.get((name, tag), dict(state='pending-publication', sha=None, release=None))

    def aggregate(self, version):
        return self.aggregates.get(version)

    def tree(self, repo, sha):
        return self.trees[repo, sha]


def published(name, tag, *, dependencies='', sha=SHA):
    binding = dict(unit=name, source_sha=sha, source_ref='main', aggregate_sha='c' * 40,
                   aggregate_version='release-v1.2.3-preview.20261026', dependencies=dependencies)
    assets = [dict(id=index, name=archive, size=10, digest='sha256:' + 'd' * 64, state='uploaded')
              for index, archive in enumerate(['SHA256SUMS'] +
              [subject.release.archive_name(name, tag, arch) for arch in ('x86_64', 'aarch64')], 1)]
    return dict(state='published', sha=sha, release=dict(id=7, tag_name=tag, draft=False,
        prerelease='-preview.' in tag, target_commitish=sha, assets=assets,
        body='<!-- kuasar-preview-binding ' + json.dumps(binding) + ' -->'))


class ClassificationTests(unittest.TestCase):
    def qualify(self, files, *, companion=None, head=None):
        before = {name: entry(manifest(index == 1)) for index, name in enumerate(subject.MANIFESTS)}
        contents = dict(zip(subject.MANIFESTS, (manifest(), manifest(True)))) | files
        after = {name: entry(text) for name, text in contents.items() if text is not None}
        blobs = {entry(text)['sha']: text.encode() for text in contents.values() if text is not None}
        records = [dict(repository=REPO, base_sha='b' * 40, head_sha='c' * 40,
                        candidate_sha='d' * 40, base_ref='main')]
        if companion:
            records += [dict(records[0], repository='kuasar-sandbox/connector')]
        def read(repo, sha):
            if repo != REPO:
                return tree({} if sha == 'b' * 40 else {companion: entry('# Companion\n')})
            return tree(before if sha == 'b' * 40 else head if head is not None and sha == 'c' * 40 else after)
        with patch.dict(os.environ, ENV), patch.object(docs_lane, 'tree', side_effect=read), \
             patch.object(docs_lane, 'blob', side_effect=lambda repo, sha: blobs[sha] if sha in blobs else b'# Companion\n'), \
             patch.object(subject, 'References', side_effect=FakeReferences):
            result = docs_lane.qualify(records, 'e' * 40)
            if result['lane'] != 'full' and result['conclusion'] == 'success':
                docs_lane.verify(result, records, 'e' * 40)
            return result

    def test_manifest_only_and_docs_companions_check_both_light_contracts(self):
        for companion in (None, 'README.md'):
            result = self.qualify({subject.MANIFESTS[0]: manifest() + '# plan\n',
                                   'docs/plan.md': '# Release plan\n'}, companion=companion)
            self.assertEqual(result['lane'], 'manifest')
            self.assertIn('docs/plan.md', result['repositories'][0]['checked_files'])
            plans = result['repositories'][0]['manifests']
            self.assertEqual(set(plans), {'head', 'candidate'})
            self.assertEqual(len(plans['candidate'][subject.MANIFESTS[0]]['pending']), 6)
        self.assertEqual(self.qualify({subject.MANIFESTS[1]: manifest(True) + '# plan\n'})['lane'], 'manifest')
        broken = self.qualify({subject.MANIFESTS[0]: manifest() + '# plan\n', 'docs/plan.md': '[bad](missing.md)\n'})
        self.assertEqual(broken['conclusion'], 'failure')

    def test_code_tooling_tests_configuration_unknown_and_companion_mix_are_full(self):
        paths = ('src/code.go', 'ci/integration/docs_lane.py', '.github/workflows/ci.yml',
                 'release/selection.py', 'test/e2e/cases/a.sh', 'config/settings.yaml',
                 'releases/other.yaml', 'releases/release.yml', 'AGENTS.md')
        for path in paths:
            with self.subTest(path=path):
                result = self.qualify({subject.MANIFESTS[0]: manifest() + '# plan\n', path: 'changed'})
                self.assertEqual(result['lane'], 'full')
        self.assertEqual(self.qualify({subject.MANIFESTS[0]: manifest() + '# plan\n'}, companion='main.go')['lane'], 'full')
        self.assertIsNone(docs_lane.classify('kuasar-sandbox/connector', tree(),
                                            tree({subject.MANIFESTS[0]: entry()})))

    def test_modes_deletions_and_incomplete_trees(self):
        for mode in ('120000', '100755', '160000'):
            for before, after in (({}, {subject.MANIFESTS[0]: entry(mode=mode)}),
                                  ({subject.MANIFESTS[0]: entry(mode=mode)}, {})):
                self.assertIsNone(docs_lane.classify(REPO, tree(before), tree(after)))
        with self.assertRaisesRegex(ValueError, 'ordinary files'):
            self.qualify({subject.MANIFESTS[0]: None})
        # Unchanged companion manifest must also be a regular file.
        files = {subject.MANIFESTS[0]: entry(), subject.MANIFESTS[1]: entry(mode='120000')}
        with self.assertRaises(ValueError), patch.object(docs_lane, 'blob', return_value=manifest().encode()):
            subject.check_tree(tree(files), 'main', FakeReferences())
        for first, second in ((tree(truncated=True), tree()), (tree(), tree(truncated=True))):
            self.assertIsNone(docs_lane.classify(REPO, first, second))
        self.assertEqual(self.qualify({subject.MANIFESTS[0]: manifest() + '# plan\n'},
                                     head={'source.py': entry()})['lane'], 'full')

    def test_invalid_schema_version_predecessor_pair_and_head_fail(self):
        good = manifest()
        bad = [good + 'unknown: value\n', good.replace('v1.2.3', 'v01.2.3'),
               good.replace('release-v1.2.2', 'release-v1.2.3'),
               good.replace('connector: v1.2.3', 'connector: main'),
               good.replace('connector: v1.2.3', 'connector: v1.2.3-preview.20261026'),
               good + 'components:\n', good.replace('release-v1.2.2', 'release-v1.2.1')]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.qualify({subject.MANIFESTS[0]: text})
        with self.assertRaisesRegex(ValueError, 'must not be older'):
            self.qualify({subject.MANIFESTS[1]: manifest(True).replace('v1.2.3', 'v1.2.1').replace('v1.2.2', 'v1.2.0')})
        preview = manifest(True) + 'previous_preview_version: preview.20261026\n'
        with self.assertRaises(ValueError):
            self.qualify({subject.MANIFESTS[1]: preview})

    def test_manifest_only_in_head_still_gets_manifest_checks(self):
        files = {name: entry(manifest(index == 1)) for index, name in enumerate(subject.MANIFESTS)}
        files[subject.MANIFESTS[0]] = entry(manifest() + '# head plan\n')
        # Model a merge which retains a newer base selection and only changes a
        # document. The head must not hide its manifest behind the docs lane.
        with patch.object(subject, 'check_tree', return_value={name: dict(units={
                unit: {} for unit in subject.selection.UNITS}, pending=[],
                acceptance='release exact-assets E2E and Workbench remain required')
                for name in subject.MANIFESTS}) as checker:
            result = self.qualify({'docs/plan.md': '# Plan\n'}, head=files)
            self.assertEqual(result['lane'], 'manifest')
            self.assertEqual(checker.call_count, 2)

    def test_missing_failed_stale_sources_policy_run_attempt_and_manifest_evidence(self):
        result = self.qualify({subject.MANIFESTS[0]: manifest() + '# plan\n'})
        with patch.dict(os.environ, ENV):
            for key, value in [('sources', []), ('policy_sha', 'f' * 40), ('run_id', '124'), ('run_attempt', '2'),
                               ('lane', 'docs'), ('conclusion', 'skipped'), ('conclusion', 'failure'),
                               ('conclusion', 'cancelled')]:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    docs_lane.verify(dict(result, **{key: value}), result['sources'], 'e' * 40)
            for key, value in [('manifests', {}), ('findings', ['bad']), ('changed', ['config.yaml'])]:
                bad = copy.deepcopy(result); bad['repositories'][0][key] = value
                with self.assertRaises(ValueError):
                    docs_lane.verify(bad, result['sources'], 'e' * 40)


class ReferenceTests(unittest.TestCase):
    def test_absent_and_in_progress_tag_are_explicit_prerequisites(self):
        for sha, state in ((None, None), (SHA, None), (SHA, dict(tag_name='v1.2.3', draft=True,
                                                             prerelease=False, target_commitish=SHA))):
            with patch.object(subject.release, 'tag_sha', return_value=sha), \
                 patch.object(subject.release, 'api_optional', return_value=state):
                self.assertEqual(subject.References().unit('connector', 'v1.2.3')['state'], 'pending-publication')
        refs = FakeReferences()
        plan = subject.check_plan(*subject.selection.parse_manifest(manifest(), 'fixture', False), 'main', refs)
        self.assertEqual(len(plan['pending']), 6)
        self.assertTrue(all(row['source_ref'] == 'main' and row['sha'] is None for row in plan['units'].values()))
        with self.assertRaisesRegex(ValueError, 'maintenance line'):
            subject.check_plan(*subject.selection.parse_manifest(manifest(), 'fixture', False), 'release/v2.0.x', refs)

    def test_existing_publication_uses_real_identity_and_asset_reader(self):
        state = published('connector', 'v1.2.3')['release']
        repo = dict(full_name='kuasar-sandbox/connector', visibility='public', private=False)
        for mutate in (None, 'target', 'digest', 'assets', 'tag', 'draft'):
            value = copy.deepcopy(state)
            if mutate == 'target': value['target_commitish'] = 'b' * 40
            if mutate == 'digest': value['assets'][0]['digest'] = None
            if mutate == 'assets': value['assets'].pop()
            if mutate == 'tag': value['tag_name'] = 'v9.9.9'
            if mutate == 'draft': value['draft'] = 'false'
            with patch.object(subject.release, 'tag_sha', return_value=SHA), \
                 patch.object(subject.release, 'api_optional', return_value=value), \
                 patch.object(subject.release, 'api', return_value=repo):
                if mutate:
                    with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                        subject.References().unit('connector', 'v1.2.3')
                else:
                    self.assertEqual(subject.References().unit('connector', 'v1.2.3')['state'], 'published')

    def test_preview_source_and_dependency_binding_including_legal_cli_only_reuse(self):
        _, previous, components = subject.selection.parse_manifest(manifest(True), 'fixture', True)
        components['runtime'] = 'runtime-v1.2.3-preview.20261026'
        refs = FakeReferences()
        refs.published['runtime', components['runtime']] = published('runtime', components['runtime'],
                                                       dependencies='accelerator=v1.2.3,sandboxer=v1.2.3')
        plan = subject.check_plan('release-v1.2.3-preview.20261026', previous, components, 'main', refs)
        self.assertEqual(len(plan['pending']), 5)
        refs.published['runtime', components['runtime']]['release']['body'] = refs.published['runtime', components['runtime']]['release']['body'].replace(SHA, 'f' * 40)
        with self.assertRaisesRegex(RuntimeError, 'source binding'):
            subject.check_plan('release-v1.2.3-preview.20261026', previous, components, 'main', refs)
        refs.published['runtime', components['runtime']] = published('runtime', components['runtime'],
                                                       dependencies='accelerator=v1.2.2,sandboxer=v1.2.3')
        refs.published['accelerator', 'v1.2.2'] = published('accelerator', 'v1.2.2', sha='b' * 40)
        refs.published['sandboxer', 'v1.2.3'] = published('sandboxer', 'v1.2.3')
        repo = 'kuasar-sandbox/accelerator'
        refs.trees[repo, 'b' * 40] = tree({'pkg/flatten/a.go': entry('same')})
        plan = subject.check_plan('release-v1.2.3-preview.20261026', previous, components, 'main', refs)
        self.assertIn(dict(unit='runtime', dependency='accelerator', requirement='verify-linked-inputs-before-reuse'), plan['pending'])
        refs.published['accelerator', 'v1.2.3'] = published('accelerator', 'v1.2.3')
        refs.trees[repo, SHA] = tree({'pkg/flatten/a.go': entry('same'), 'cmd/cli/main.go': entry('new')})
        refs.trees['kuasar-sandbox/sandboxer', SHA] = tree()
        subject.check_plan('release-v1.2.3-preview.20261026', previous, components, 'main', refs)
        refs.trees[repo, SHA]['entries']['pkg/flatten/a.go'] = entry('different')
        with self.assertRaisesRegex(ValueError, 'linked dependency inputs'):
            subject.check_plan('release-v1.2.3-preview.20261026', previous, components, 'main', refs)

    def test_already_published_aggregate_cannot_select_other_units(self):
        refs = FakeReferences()
        refs.aggregates['release-v1.2.3'] = dict(sha=SHA, units={})
        with self.assertRaisesRegex(ValueError, 'already published aggregate'):
            subject.check_plan(*subject.selection.parse_manifest(manifest(), 'fixture', False), 'main', refs)


class WorkflowTests(unittest.TestCase):
    def test_trusted_sparse_checkout_can_import_manifest_reader_without_candidate_files(self):
        import yaml
        jobs = yaml.safe_load((ROOT / '.github/workflows/integration-tests.yml').read_text())['jobs']
        checkouts = [step['with'] for name in ('resolve', 'results') for step in jobs[name]['steps']
                     if step.get('with', {}).get('path') == 'docs-policy']
        self.assertEqual(len(checkouts), 2)
        self.assertEqual(checkouts[0]['sparse-checkout'], checkouts[1]['sparse-checkout'])
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            for pattern in checkouts[0]['sparse-checkout'].splitlines():
                for path in ROOT.glob(pattern + '**/*' if pattern.endswith('/') else pattern):
                    if path.is_file() and '__pycache__' not in path.parts:
                        target = destination / path.relative_to(ROOT)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, target)
            process = subprocess.run(['python3', '-B', '-c', 'import manifest_lane'],
                                     cwd=destination / 'ci/integration', capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)

    def test_final_gate_accepts_only_explicit_success_and_exact_assets_stays_full(self):
        import yaml
        jobs = yaml.safe_load((ROOT / '.github/workflows/integration-tests.yml').read_text())['jobs']
        script = jobs['results']['steps'][0]['run']
        good = dict(DOCS_LANE='manifest', INTEGRATION_MODE='source', RESOLVE_RESULT='success',
                    X86_RESULT='skipped', ARM_RESULT='skipped', SOURCE_RESULT='skipped',
                    WORKBENCH_SELECTED='false', WORKBENCH_RESULT='skipped', WORKBENCH_RELEASE_RESULT='skipped')
        self.assertEqual(subprocess.run(['bash', '-c', script], env={**os.environ, **good}).returncode, 0)
        for key in good:
            for value in ('', 'failure', 'cancelled', 'neutral'):
                self.assertNotEqual(subprocess.run(['bash', '-c', script], env={**os.environ, **good, key:value}, capture_output=True).returncode, 0)
        self.assertNotEqual(subprocess.run(['bash', '-c', script], env={**os.environ, **good, 'INTEGRATION_MODE':'exact-assets'}, capture_output=True).returncode, 0)
        resolve = jobs['resolve']['steps']
        classifier = next(step for step in resolve if step.get('id') == 'docs')
        self.assertEqual(classifier['if'], "inputs.mode == 'source'")
        self.assertIn('docs-policy/ci/integration/docs_lane.py', classifier['run'])
        self.assertIn("echo 'lane=full'", classifier['run'])
        for name in ('x86_64', 'aarch64', 'source-checks', 'workbench-native'):
            self.assertIn("needs.resolve.outputs.lane == 'full'", jobs[name]['if'])
        verify = next(step for step in jobs['results']['steps'] if step.get('name', '').startswith('Bind docs success'))
        self.assertIn('"manifest"', verify['if'])
        self.assertIn('--verify plan/docs-result.json', verify['run'])
        self.assertIn("inputs.mode == 'exact-assets'", jobs['workbench-release']['if'])


if __name__ == '__main__':
    unittest.main()
