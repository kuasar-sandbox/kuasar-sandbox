#!/usr/bin/env python3
"""Trusted dependency cutovers fail closed without widening ordinary PR inputs."""
import base64
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import artifacts

spec = importlib.util.spec_from_file_location('cutover_resolver', Path(__file__).with_name('resolve-artifacts.py'))
resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolver)


class DependencyCutover(unittest.TestCase):
    def setUp(self):
        self.candidate = dict(repository='kuasar-sandbox/sandboxer', pull_request_number=371,
                              base_ref='main', base_sha='a'*40, head_sha='b'*40, candidate_sha='c'*40)
        tag = 'v0.1.9-preview.20261028'
        names = sorted({'SHA256SUMS'} | {artifacts.archive_name('accelerator', tag, arch) for arch in artifacts.ARCHES})
        self.pin = dict(tag=tag, sha='d'*40, release_id=100,
                        assets=[dict(name=name, id=i, size=1000, digest='sha256:'+'e'*64) for i, name in enumerate(names, 1)])
        self.request = dict(candidate=self.candidate, units={'accelerator': self.pin})
        self.config = dict(schema=1, requests=[self.request])
        self.state = dict(tag_name=tag, target_commitish=self.pin['sha'], draft=False, prerelease=True,
                          id=100, assets=[dict(asset, state='uploaded') for asset in self.pin['assets']])

    def select(self, config=None, records=None):
        return resolver.cutover_request(json.dumps(self.config if config is None else config),
                                        [self.candidate] if records is None else records)

    def test_default_schema4_selection_remains_the_validated_aggregate(self):
        selected = {'validation_plan': {'schema': 4}, 'units': {'accelerator': {'version': 'v0.1.8'}}}
        before = copy.deepcopy(selected)
        self.assertEqual(resolver.qualification_units(selected, {'accelerator': self.pin['tag']}), {'accelerator': 'v0.1.8'})
        self.assertEqual(selected, before)
        self.assertIsNone(self.select({'schema': 1, 'requests': []}))
        other = dict(self.candidate, pull_request_number=372)
        self.assertIsNone(self.select(records=[other]))
        with tempfile.TemporaryDirectory() as directory, patch.object(resolver, 'verify_cutover_publication') as check:
            self.assertIsNone(resolver.dependency_cutover(Path(directory), 'main', 'f'*40, [self.candidate]))
            check.assert_not_called()

    def test_exact_candidate_and_no_hidden_companion(self):
        self.assertEqual(self.select(), self.request)
        for field in ('base_sha', 'head_sha', 'candidate_sha', 'base_ref'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'identity changed'):
                self.select(records=[dict(self.candidate, **{field: 'release/v0.1.x' if field == 'base_ref' else 'f'*40})])
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            self.select(records=[self.candidate, dict(self.candidate, repository='kuasar-sandbox/connector')])

    def test_strict_schema_and_duplicates(self):
        variants = []
        for path in ((), ('requests', 0), ('requests', 0, 'candidate'), ('requests', 0, 'units', 'accelerator')):
            bad = copy.deepcopy(self.config); target = bad
            for key in path: target = target[key]
            target['unexpected'] = True; variants.append(bad)
        bad = copy.deepcopy(self.config); bad['requests'].append(copy.deepcopy(self.request)); variants.append(bad)
        bad = copy.deepcopy(self.config); bad['requests'][0]['units'] = {}; variants.append(bad)
        bad = copy.deepcopy(self.config); bad['requests'][0]['candidate']['pull_request_number'] = True; variants.append(bad)
        for bad in variants:
            with self.subTest(config=bad), self.assertRaises(ValueError): self.select(bad)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            resolver.cutover_request('{"schema":1,"schema":1,"requests":[]}', [self.candidate])
        for field, value in [('id', 0), ('size', 0), ('digest', 'sha256:bad'), ('name', 'extra')]:
            bad = copy.deepcopy(self.config); bad['requests'][0]['units']['accelerator']['assets'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.select(bad)

    def test_candidate_owner_cannot_be_replaced_including_both_runtime_units(self):
        for unit in ('sandboxer', 'runtime', 'vmlinux'):
            candidate = dict(self.candidate, repository='kuasar-sandbox/' + resolver.unit_owner(unit))
            bad = {'schema': 1, 'requests': [{'candidate': candidate, 'units': {unit: self.pin}}]}
            with self.subTest(unit=unit), self.assertRaisesRegex(ValueError, 'candidate owner'):
                self.select(bad, [candidate])

    def verify_publication(self, state=None, sha=None):
        with patch.object(resolver, 'public'), patch.object(resolver.release, 'api', return_value=self.state if state is None else state), \
             patch.object(resolver.release, 'tag_sha', return_value=self.pin['sha'] if sha is None else sha):
            resolver.verify_cutover_publication('accelerator', self.pin)

    def test_publication_requires_exact_release_tag_and_assets(self):
        self.verify_publication()
        with self.assertRaises(ValueError): self.verify_publication(sha='f'*40)
        changes = [('id', 101), ('draft', True), ('prerelease', False), ('target_commitish', 'main')]
        for field, value in changes:
            state = copy.deepcopy(self.state); state[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.verify_publication(state)
        for field, value in [('id', 99), ('size', 999), ('digest', 'sha256:'+'f'*64), ('state', 'new')]:
            state = copy.deepcopy(self.state); state['assets'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.verify_publication(state)
        state = copy.deepcopy(self.state); state['assets'].pop()
        with self.assertRaises(ValueError): self.verify_publication(state)

    def test_admission_reads_only_the_trusted_base_not_candidate_configuration(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(resolver, 'verify_cutover_publication') as check:
            root = Path(directory); trusted = root/'base'; candidate = root/'candidate'
            for where, config in [(trusted, {'schema':1, 'requests':[]}), (candidate, self.config)]:
                path = where/resolver.CUTOVER_PATH; path.parent.mkdir(parents=True); path.write_text(json.dumps(config))
            self.assertIsNone(resolver.dependency_cutover(trusted, 'main', 'f'*40, [self.candidate]))
            check.assert_not_called()
            (trusted/resolver.CUTOVER_PATH).write_text(json.dumps(self.config))
            evidence = resolver.dependency_cutover(trusted, 'main', 'f'*40, [self.candidate])
            self.assertEqual(evidence['platform_sha'], 'f'*40)
            self.assertEqual(evidence['request'], self.request)
            self.assertEqual(evidence['configuration_sha256'], artifacts.digest(trusted/resolver.CUTOVER_PATH))
            check.assert_called_once_with('accelerator', self.pin)

    def test_final_acceptance_rechecks_live_configuration_and_publications(self):
        record = dict(repository='kuasar-sandbox/accelerator', tag=self.pin['tag'], sha=self.pin['sha'], tree='e'*40)
        plan = dict(mode='source', candidate_records=[self.candidate],
                    run_inputs={'admission-platform': {'sha': 'f'*40}}, source_records={'accelerator': record},
                    maintained_deltas={'accelerator': record}, dependency_cutover=dict(platform_sha='f'*40, base_ref='main', request=self.request))
        def run(config):
            value = dict(type='file', encoding='base64', content=base64.b64encode(json.dumps(config).encode()).decode())
            with patch.object(artifacts, 'check_plan'), patch.object(resolver.release, 'api', return_value=value) as api, \
                 patch.object(resolver, 'verify_cutover_publication') as verify:
                resolver.verify_dependency_cutover(plan)
                self.assertTrue(api.call_args.args[0].endswith('?ref=main'))
                verify.assert_called_once_with('accelerator', self.pin)
        run(self.config)
        extra = copy.deepcopy(self.config); extra['requests'].append(dict(self.request, candidate=dict(self.candidate, pull_request_number=372)))
        run(extra)
        with self.assertRaisesRegex(ValueError, 'revoked'): run({'schema':1, 'requests':[]})
        changed = copy.deepcopy(self.config); changed['requests'][0]['units']['accelerator']['release_id'] += 1
        with self.assertRaisesRegex(ValueError, 'revoked'): run(changed)

    def test_source_plan_keeps_baseline_and_binds_new_tuple_with_full_coverage(self):
        import os
        import helper_inputs
        from contextlib import ExitStack
        from test_fixtures import CASES, helper_contract
        tags = {unit: ('runtime-' if unit == 'runtime' else 'vmlinux-' if unit == 'vmlinux' else '') + 'v0.1.8'
                for unit in artifacts.UNITS}
        selected = dict(version='release-v0.1.9', sha='1'*40, validation_plan={'schema':4}, case_files=CASES,
                        units={unit:dict(repository=resolver.REPOSITORIES[resolver.unit_owner(unit)], version=tag, sha='2'*40)
                               for unit, tag in tags.items()})
        original = copy.deepcopy(selected)
        capabilities = artifacts.capabilities_module()
        contract = {'schema': 1, 'cases': {name: {'sha256': 'b'*64, 'requires': []}
                    for names in CASES.values() for name in names}}
        contract['cases']['basic.demo.sh']['requires'] = ['kvm']
        def named(root, repository, ref, sha):
            root.mkdir(parents=True)
            if root.name == 'admission-platform':
                path = root/resolver.CUTOVER_PATH; path.parent.mkdir(parents=True)
                path.write_text(json.dumps(self.config))
            return dict(repository=repository, sha=sha, tree='3'*40, ref=ref)
        def unit_source(root, unit, tag, sha):
            root.mkdir(parents=True)
            return dict(repository=resolver.REPOSITORIES[resolver.unit_owner(unit)], tag=tag, sha=sha, tree='3'*40)
        def discover(roots, identities):
            contract = helper_contract()
            for owner in artifacts.OWNERS: contract['owners'][owner]['source'] = identities[owner]
            return contract
        env = dict(CANDIDATE_REPOSITORY=self.candidate['repository'], CANDIDATE_PR='371',
                   CANDIDATE_SHA=self.candidate['candidate_sha'], CANDIDATE_BASE_SHA=self.candidate['base_sha'],
                   CANDIDATE_HEAD_SHA=self.candidate['head_sha'], CANDIDATE_BASE_REF='main', COMPANION_CANDIDATES='[]')
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            for target, name, kwargs in [
                (resolver, 'public', {}), (resolver.release, 'branch_sha', {'return_value':'f'*40}),
                (resolver, 'baseline', {'return_value':selected}), (resolver, 'maintained_units', {'return_value':tags}),
                (resolver, 'verify_cutover_publication', {}), (resolver, 'published_unit', {'return_value':self.pin['sha']}),
                (resolver.source_inputs, 'fetch_named', {'side_effect':named}),
                (resolver.source_inputs.tag_sources, 'fetch_unit', {'side_effect':unit_source}),
                (resolver.source_inputs, 'inspect_platform', {'return_value':dict(repository=resolver.PLATFORM,sha='f'*40,tree='3'*40)}),
                (resolver, 'tree_changes', {'return_value':['pkg/tarstream/write.go']}),
                (resolver.source_inputs, 'cases', {'return_value':CASES}),
                (helper_inputs, 'discover', {'side_effect':discover}),
                (artifacts, 'capabilities_module', {'return_value':capabilities}),
                (capabilities, 'discover', {'return_value':contract})]:
                stack.enter_context(patch.object(target, name, **kwargs))
            stack.enter_context(patch.dict(os.environ, env))
            plan = resolver.source_plan('f'*40, Path(directory)/'inputs')
        self.assertEqual(plan['schema'], 4)
        self.assertEqual(plan['baseline'], original)
        self.assertEqual(plan['owners'], sorted(artifacts.OWNERS))
        self.assertEqual(plan['sources']['accelerator']['sha'], self.pin['sha'])
        self.assertEqual(plan['sources']['accelerator']['role'], 'release')
        self.assertEqual(plan['test_revisions']['accelerator']['sha'], self.pin['sha'])
        self.assertEqual(plan['sources']['sandboxer']['sha'], self.candidate['candidate_sha'])
        self.assertEqual(plan['sources']['sandboxer']['role'], 'candidate')
        self.assertEqual(plan['dependency_cutover']['request'], self.request)
        self.assertIn('maintained/accelerator', plan['run_inputs'])
        for arch in artifacts.ARCHES:
            self.assertTrue({'sandbox-ctl','node-ctl','flatten-ctl','sandbox-runtime.bundle'} <= set(plan['lanes'][arch]['products']))
            self.assertEqual(plan['lanes'][arch]['selection'], artifacts.suite_selection(artifacts.OWNERS, arch, CASES, native_full=True, changes=plan['changes']))
        artifacts.check_plan(plan)
        resolver.capability_routing.validate(plan)
        self.assertIn('supplemental-arm--basic', plan['lanes']['aarch64']['groups'])
        source_spec = importlib.util.spec_from_file_location('cutover_source_checks', Path(__file__).with_name('run-source-checks.py'))
        source_checks = importlib.util.module_from_spec(source_spec); source_spec.loader.exec_module(source_checks)
        selected_owners, records = source_checks.source_records(plan)
        self.assertEqual(selected_owners, set(artifacts.OWNERS))
        self.assertEqual(records['accelerator']['role'], 'release')
        self.assertEqual(records['sandboxer']['role'], 'candidate')

    def test_accelerator_cutover_rebuilds_transitive_consumers(self):
        products = resolver.maintained_products({'accelerator': {}})
        self.assertTrue({'manifest-ctl','store-ctl','cache-ctl','sandbox-ctl','node-ctl','flatten-ctl','sandbox-runtime.bundle'} <= products)
        self.assertNotIn('vmlinux', products)
        revisions = artifacts.release_test_revisions({owner:'d'*40 for owner in artifacts.OWNERS if owner != 'platform'}, 'f'*40)
        artifacts.validate_test_revisions(revisions)  # existing release role is admitted, not a baseline claim


if __name__ == '__main__':
    unittest.main()
