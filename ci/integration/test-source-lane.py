#!/usr/bin/env python3
"""Behavioral regressions for source-only admission and exact scope binding."""
from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import docs_lane
import source_lane as subject
import tooling_lane


PLATFORM = 'kuasar-sandbox/kuasar-sandbox'
POLICY = 'e' * 40
ENV = {'GITHUB_RUN_ID': '42001', 'GITHUB_RUN_ATTEMPT': '2'}
PYTHON_TEST = 'import unittest\nclass Contract(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(1, 1)\n'
GO_TEST = 'package fixture\nimport "testing"\nfunc TestContract(t *testing.T) {}\n'
GO_MODULE = {'go.mod': 'module example.com/fixture\n'}


def record(repository, index=1):
    def identity(kind):
        return hashlib.sha1((repository + ':' + kind).encode()).hexdigest()
    return dict(repository=repository, base_sha=identity('base'), head_sha=identity('head'),
                candidate_sha=identity('merge'), pull_request_number=index, base_ref='main')


def snapshot(repository, files, blobs=None, *, truncated=False):
    """Fake API trees carry real Git blob hashes and complete directory entries."""
    entries = {}
    for path, value in files.items():
        data, mode, kind = value if isinstance(value, tuple) else (value, '100644', 'blob')
        data = data.encode() if isinstance(data, str) else data
        identity = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        entries[path] = dict(type=kind, mode=mode, sha=identity)
        if blobs is not None:
            blobs[(repository, identity)] = data
        for parent in Path(path).parents:
            if parent == Path('.'):
                continue
            name = parent.as_posix()
            entries.setdefault(name, dict(type='tree', mode='040000', sha='f' * 40))
    identity = hashlib.sha1(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    return dict(sha=identity, truncated=truncated, entries=entries)


def qualify(candidate, *, repository=PLATFORM, before=None, head=None, companions=None):
    """Exercise production classify/check/verify with only Git transport mocked."""
    versions = {repository: {'base': before or {}, 'head': candidate if head is None else head,
                             'merge': candidate}}
    versions.update(companions or {})
    records = [record(repo, index) for index, repo in enumerate(sorted(versions), 1)]
    blobs, trees = {}, {}
    for row in records:
        for kind, key in (('base', 'base_sha'), ('head', 'head_sha'), ('merge', 'candidate_sha')):
            trees[(row['repository'], row[key])] = snapshot(row['repository'], versions[row['repository']][kind], blobs)
    with patch.dict(os.environ, ENV), \
            patch.object(docs_lane, 'tree', side_effect=lambda repo, sha: copy.deepcopy(trees[(repo, sha)])), \
            patch.object(docs_lane, 'blob', side_effect=lambda repo, sha: blobs[(repo, sha)]):
        result = docs_lane.qualify(records, POLICY)
        if result['lane'] != 'full' and result['conclusion'] == 'success':
            docs_lane.verify(result, records, POLICY)
    return result


@contextmanager
def without_host_go():
    # Go parser behavior has its own tooling tests. Scope tests must not need a
    # host compiler; Python/Bash parsing and all metadata parsers remain real.
    parser = tooling_lane.run_parser
    def run(command, data):
        if command[0] == 'gofmt':
            return None
        return parser(command, data)
    with patch.object(tooling_lane, 'run_parser', side_effect=run):
        yield


class PathAndQualificationTests(unittest.TestCase):
    def assert_source(self, result):
        self.assertEqual((result['lane'], result['conclusion']), ('source', 'success'), result)
        self.assertTrue(any(row['source']['head']['checks'] or row['source']['candidate']['checks']
                            for row in result['repositories']))

    def test_source_adjacent_markdown_is_documentation_in_all_six_repositories(self):
        paths = ('CONTRIBUTING.md', 'SECURITY.md', '.github/PULL_REQUEST_TEMPLATE.md',
                 'examples/plugin/README.md', 'native-deps/deps/linux-patches/README.md',
                 'test/README.md', 'test/QUICKSTART.md', 'test/perf/agent-density/README.md',
                 'ci/integration/fixtures/README.md')
        for repository in sorted(subject.REPOSITORIES):
            with self.subTest(repository=repository):
                result = qualify({path: '# Guide\n\nMaintained guidance.\n' for path in paths},
                                 repository=repository)
                self.assertEqual((result['lane'], result['conclusion']), ('docs', 'success'), result)
                self.assertEqual(set(result['repositories'][0]['checked_files']), set(paths))

    def test_markdown_test_inputs_are_not_ordinary_documentation(self):
        self.assertEqual(qualify({'pkg/parser/testdata/request.md': '# Fixture\n'})['lane'], 'full')
        self.assert_source(qualify({'ci/integration/fixtures/pull-body.md': 'A parsed request fixture.\n'}))

    def test_non_e2e_script_entries_reach_source_qualification(self):
        examples = {
            PLATFORM: ('ci/integration/test-new-contract.py', 'test/release/payload_test.py',
                       'test/demo/test_demo_build_selection.py', 'test/perf/working_set_directory_test.py',
                       'test/e2e/platform/lib/test_failure_diagnostics.py', 'workbench/test_workbench.py'),
            'kuasar-sandbox/orchestrator': ('test/e2e/lib/test_lifecycle_helpers.py', 'scripts/test-telemetry-image-pull.py'),
            'kuasar-sandbox/sandboxer': ('test/source/diffstore_test.py', 'scripts/test_e2e_memory.py'),
            'kuasar-sandbox/accelerator': ('test/scripts/test_flatten.py',),
            'kuasar-sandbox/connector': ('scripts/test-notify-helpers.sh',),
            'kuasar-sandbox/guest-runtime': ('test/e2e/test_scripts.py', 'native-deps/deps/test-common.sh'),
        }
        for repository, paths in examples.items():
            for path in paths:
                with self.subTest(repository=repository, path=path):
                    source = PYTHON_TEST if path.endswith('.py') else 'set -eu\ntest 1 = 1\n'
                    self.assertTrue(subject.test_script(repository, path))
                    self.assert_source(qualify({path: source}, repository=repository))

    def test_shared_fixture_module_is_not_executed_as_a_standalone_test(self):
        path = 'ci/integration/test_fixtures.py'
        self.assertFalse(subject.test_script(PLATFORM, path))
        self.assert_source(qualify({path: 'def fixture():\n    return {}\n'}))

    def test_documentation_delivery_checker_reaches_actual_source_execution(self):
        path = 'ci/integration/check-source-documentation.py'
        self.assertFalse(subject.test_script(PLATFORM, path))
        self.assert_source(qualify({path: 'def check():\n    return {}\n'}))

    def test_parameterized_test_helpers_select_their_real_fixture_suites(self):
        for (repository, path), driver in subject.TEST_DRIVERS.items():
            with self.subTest(repository=repository, path=path):
                self.assertTrue(subject.test_script(repository, driver))
                self.assertTrue(subject.test_script(repository, path))
                content = 'def run():\n    pass\n' if path.endswith('.py') else 'set -eu\ntest "$1" = fixture\n'
                self.assert_source(qualify({path: content}, repository=repository))

    def test_default_go_test_files_qualify_for_all_component_owners(self):
        with without_host_go():
            for repository in sorted(subject.REPOSITORIES - {PLATFORM}):
                for path in ('cmd/ctl/config_test.go', 'pkg/store/state_linux_amd64_test.go',
                             'pkg/files/copy_windows_names_test.go'):
                    with self.subTest(repository=repository, path=path):
                        self.assert_source(qualify({**GO_MODULE, path: GO_TEST}, before=GO_MODULE,
                                                   repository=repository))

    def test_go_files_outside_executed_root_modules_remain_full(self):
        repository = 'kuasar-sandbox/orchestrator'
        examples = ('pkg/vendor/lib/state_test.go', 'testdata/source_test.go',
                    '.internal/source_test.go', '_fixtures/source_test.go', 'pkg/_source_test.go')
        for path in examples:
            with self.subTest(path=path):
                self.assertEqual(qualify({**GO_MODULE, path: GO_TEST}, before=GO_MODULE,
                                         repository=repository)['lane'], 'full')
        self.assertEqual(qualify({'pkg/source_test.go': GO_TEST}, repository=repository)['lane'], 'full')
        self.assertEqual(qualify({'ci/source_test.go': GO_TEST})['lane'], 'full')
        common = {**GO_MODULE, 'tools/go.mod': 'module example.com/tools\n'}
        self.assertEqual(qualify({**common, 'tools/fixture_test.go': GO_TEST}, before=common,
                                 repository=repository)['lane'], 'full')

    def test_uncovered_go_architectures_and_custom_build_tags_remain_full(self):
        repository = 'kuasar-sandbox/orchestrator'
        cases = [
            ('pkg/state/disk_arm64_test.go', GO_TEST),
            ('pkg/state/disk_windows_test.go', GO_TEST),
            ('pkg/state/disk_linux_arm64_test.go', GO_TEST),
            ('pkg/state/disk_test.go', '//go:build integration\n\n' + GO_TEST),
            ('pkg/state/disk_test.go', '//go:build !linux\n\n' + GO_TEST),
            ('pkg/state/disk_test.go', '// +build cgo\n\n' + GO_TEST),
        ]
        with patch.object(tooling_lane, 'check_blob', side_effect=AssertionError('full cannot parse candidate tests')):
            for path, data in cases:
                with self.subTest(path=path, data=data):
                    self.assertEqual(qualify({**GO_MODULE, path: data}, before=GO_MODULE,
                                             repository=repository)['lane'], 'full')
            # Checking only merge would conceal the unqualified HEAD test.
            self.assertEqual(qualify({**GO_MODULE, 'pkg/state/disk_test.go': GO_TEST}, before=GO_MODULE,
                repository=repository, head={**GO_MODULE,
                'pkg/state/disk_test.go': '//go:build integration\n\n' + GO_TEST})['lane'], 'full')

    def test_build_directives_inside_go_test_data_are_not_file_constraints(self):
        text = ('package fixture\nimport "testing"\nfunc TestSourceText(t *testing.T) {\n'
                'source := `\n//go:build integration\n\npackage sample\n`\n'
                'if source == "" { t.Fatal("empty source") }\n}\n')
        with without_host_go():
            self.assert_source(qualify({**GO_MODULE, 'pkg/parser/source_test.go': text}, before=GO_MODULE,
                                      repository='kuasar-sandbox/orchestrator'))

    def test_product_cases_runtime_libraries_and_inputs_override_test_like_names(self):
        examples = {
            PLATFORM: ('test/e2e/cases/test_named_case.sh', 'test/e2e/lib/workspace.py',
                       'test/e2e/platform/lib/diagnostics.py', 'test/e2e/e2e',
                       'test/demo/prepared.py', 'test/demo/demo_e2b.sh',
                       'workbench/test-release.py', 'workbench/test-build.py', 'workbench/test-system.py',
                       'release/inputs/bin-inputs.yaml', 'release/inputs/helper-inputs.yaml'),
            'kuasar-sandbox/orchestrator': ('test/e2e/cases/test_case.sh', 'test/e2e/lib/lifecycle.py',
                                           'test/e2e/lib/requirements.json', 'release/helper-inputs.json'),
            'kuasar-sandbox/sandboxer': ('test/fixtures/dirtymemory/main.go', 'test/fixtures/cgroup_fork_probe.c'),
            'kuasar-sandbox/connector': ('pkg/internal/bpf/vswitch_x86_bpfel.o',),
            'kuasar-sandbox/guest-runtime': ('native-deps/deps/linux-patches/change.patch',),
        }
        for repository, paths in examples.items():
            for path in paths:
                with self.subTest(repository=repository, path=path):
                    self.assertFalse(subject.test_script(repository, path))
                    result = qualify({'README.md': '# Guide\n', path: 'changed\n'}, repository=repository)
                    self.assertEqual(result['lane'], 'full', result)

    def test_source_test_with_product_or_dependency_change_remains_full(self):
        for path in ('cmd/service/main.go', 'go.mod', 'go.sum', 'scripts/runtime.sh',
                     'deploy/node.service', 'test/unknown.py', 'Makefile'):
            with self.subTest(path=path):
                result = qualify({'test/demo/test_contract.py': PYTHON_TEST, path: 'changed\n'})
                self.assertEqual(result['lane'], 'full')

    def test_runtime_companion_and_head_only_product_changes_force_full(self):
        runtime = {'main.go': 'package main\n'}
        companion = {'kuasar-sandbox/connector': {'base': {}, 'head': runtime, 'merge': runtime}}
        with patch.object(tooling_lane, 'check_blob', side_effect=AssertionError('full cannot parse candidate tests')):
            self.assertEqual(qualify({'test/demo/test_contract.py': PYTHON_TEST}, companions=companion)['lane'], 'full')
            self.assertEqual(qualify({'test/demo/test_contract.py': PYTHON_TEST}, head=runtime)['lane'], 'full')
            self.assertEqual(qualify(runtime, head={'test/demo/test_contract.py': PYTHON_TEST})['lane'], 'full')

    def test_doc_companion_and_head_merge_test_paths_are_all_bound(self):
        path = 'test/demo/test_head.py'
        companion = {'kuasar-sandbox/connector': {'base': {}, 'head': {'README.md': '# Connector\n'},
                                                  'merge': {'README.md': '# Connector\n'}}}
        result = qualify({'test/demo/test_merge.py': PYTHON_TEST}, head={path: PYTHON_TEST}, companions=companion)
        self.assert_source(result)
        scope = subject.scope_from_evidence(result)
        self.assertEqual(scope['changes']['platform'], ['test/demo/test_head.py', 'test/demo/test_merge.py'])
        self.assertEqual(scope['changes']['connector'], ['README.md'])
        self.assertEqual(scope['sources'], sorted(result['sources'], key=lambda row: row['repository']))

    def test_deleting_or_renaming_tests_never_hides_a_product_destination(self):
        old = 'test/demo/test_old.py'
        result = qualify({}, before={old: PYTHON_TEST})
        self.assert_source(result)
        self.assertEqual(result['repositories'][0]['source']['candidate']['checks'], [dict(path=old, state='deleted')])
        self.assertEqual(qualify({'test/demo/runtime.py': PYTHON_TEST}, before={old: PYTHON_TEST})['lane'], 'full')
        self.assertEqual(qualify({}, head={old: PYTHON_TEST})['lane'], 'full')

    def test_symlink_submodule_mode_and_truncation_remain_full(self):
        path = 'test/demo/test_contract.py'
        normal = PYTHON_TEST
        for bad in ((b'../../outside.py', '120000', 'blob'), (b'', '160000', 'commit'),
                    (PYTHON_TEST, '100664', 'blob')):
            for before, after in (({}, {path: bad}), ({path: bad}, {}),
                                  ({path: normal}, {path: bad}), ({path: bad}, {path: normal})):
                with self.subTest(before=before, after=after):
                    self.assertIsNone(docs_lane.classify(PLATFORM, snapshot(PLATFORM, before), snapshot(PLATFORM, after)))
        for side in ('before', 'after'):
            before = snapshot(PLATFORM, {}, truncated=side == 'before')
            after = snapshot(PLATFORM, {path: normal}, truncated=side == 'after')
            self.assertIsNone(docs_lane.classify(PLATFORM, before, after))
        self.assertEqual(docs_lane.classify(PLATFORM, snapshot(PLATFORM, {}),
            snapshot(PLATFORM, {path: (PYTHON_TEST, '100755', 'blob')})), [path])
        for path in ('README.md', 'release/guide-migrations.json'):
            self.assertIsNone(docs_lane.classify(PLATFORM, snapshot(PLATFORM, {}),
                snapshot(PLATFORM, {path: ('{}', '100755', 'blob')})))

    def test_classification_never_executes_candidate_test_bodies(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'executed'
            python = 'from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\nraise RuntimeError("candidate")\n'
            shell = 'touch ' + str(marker) + '\necho "$(touch ' + str(marker) + ')"\nexit 77\n'
            self.assert_source(qualify({'test/demo/test_contract.py': python, 'test/demo/test_contract.sh': shell}))
            self.assertFalse(marker.exists())

    def test_invalid_test_syntax_fails_in_both_head_and_merge(self):
        good = {'test/demo/test_contract.py': PYTHON_TEST}
        bad = {'test/demo/test_contract.py': 'return 1\n'}
        for candidate, head in ((bad, good), (good, bad)):
            result = qualify(candidate, head=head)
            self.assertEqual((result['lane'], result['conclusion']), ('source', 'failure'))
            with patch.dict(os.environ, ENV), self.assertRaises(ValueError):
                docs_lane.verify(result, result['sources'], POLICY)

    def test_deliberately_invalid_parser_fixtures_are_test_data(self):
        for path, data in (('ci/integration/fixtures/bad-input.json', '{'),
                           ('ci/integration/fixtures/bad-input.yaml', '['),
                           ('ci/integration/fixtures/bad-input.py', 'return 1\n'),
                           ('ci/integration/fixtures/bad-input.sh', 'if true; then\n')):
            with self.subTest(path=path):
                self.assertFalse(subject.test_script(PLATFORM, path))
                result = qualify({path: data})
                self.assert_source(result)
                self.assertEqual(result['repositories'][0]['source']['candidate']['checks'][0]['parser'], 'source-fixture')


class DocumentationMetadataTests(unittest.TestCase):
    def candidate(self, declaration, *, path='release/inputs/guide-inputs.yaml', extra=None):
        return {'docs/guide.md': '# Guide\n', path: declaration, **(extra or {})}

    def test_actual_guide_selection_parsers_accept_current_legacy_and_component_forms(self):
        for repository, path, data in (
            (PLATFORM, 'release/inputs/guide-inputs.yaml', '["docs/*.md"]'),
            (PLATFORM, 'releases/guide-inputs.yaml', '["docs"]'),
            ('kuasar-sandbox/connector', 'release/guide-inputs.txt', '# Maintained guides\ndocs/guide.md\n'),
        ):
            with self.subTest(repository=repository, path=path):
                result = qualify(self.candidate(data, path=path), repository=repository)
                self.assertEqual((result['lane'], result['conclusion']), ('source', 'success'), result)
                self.assertEqual(result['repositories'][0]['source']['candidate']['checks'][0]['parser'], 'guide-inputs')

    def test_guide_patterns_are_semantically_checked_not_just_parsed_as_yaml(self):
        for declaration in ('[]', '{}', '["../outside.md"]', '["/absolute.md"]',
                            '["docs/missing*.md"]', '["docs", "docs/guide.md"]',
                            '["docs/data.txt"]', '["docs/.private.md"]', '- docs/guide.md\n'):
            with self.subTest(declaration=declaration):
                # The non-Markdown input already exists; changing it would
                # correctly make the whole PR unknown/full before parsing.
                result = qualify(self.candidate(declaration, extra={'docs/data.txt': 'data\n'}),
                                 before={'docs/data.txt': 'data\n'})
                self.assertEqual((result['lane'], result['conclusion']), ('source', 'failure'), result)
                self.assertTrue(result['repositories'][0]['source']['candidate']['findings'])
        # A valid merge must not hide a malformed declaration in the PR HEAD.
        result = qualify(self.candidate('["docs/guide.md"]'), head=self.candidate('["../outside.md"]'))
        self.assertEqual(result['conclusion'], 'failure')
        self.assertTrue(result['repositories'][0]['source']['head']['findings'])

    def test_selector_must_reject_an_unchanged_symlink_input(self):
        declaration = 'release/inputs/guide-inputs.yaml'
        common = {'docs/guide.md': '# Guide\n', 'docs/linked.md': ('guide.md', '120000', 'blob')}
        result = qualify({**common, declaration: '["docs/linked.md"]'},
                         before={**common, declaration: '["docs/guide.md"]'})
        self.assertEqual((result['lane'], result['conclusion']), ('source', 'failure'), result)
        self.assertTrue(any('symbolic link' in message for message in
                            result['repositories'][0]['source']['candidate']['findings']))

    def test_migration_schema_paths_owners_and_cycles_use_the_real_parser(self):
        path = 'release/guide-migrations.json'
        documents = {'docs/guide.md': '# Guide\n'}
        valid = {'schema': 1, 'redirects': [{'from': 'docs/old.md', 'to': 'docs/guide.md'}]}
        result = qualify({**documents, path: json.dumps(valid)})
        self.assertEqual((result['lane'], result['conclusion']), ('source', 'success'), result)
        variants = [
            {'schema': True, 'redirects': []},
            {'schema': 1, 'redirects': [{'from': '../outside.md', 'to': 'docs/guide.md'}]},
            {'schema': 1, 'redirects': [{'from': 'docs/old.md', 'to': 'https://example.org/guide.md'}]},
            {'schema': 1, 'redirects': [{'from': 'docs/old.md', 'to': 'docs/guide.md', 'to_owner': 'unknown'}]},
            {'schema': 1, 'redirects': [{'from': 'docs/a.md', 'to': 'docs/b.md'},
                                      {'from': 'docs/b.md', 'to': 'docs/a.md'}]},
            {'schema': 2, 'redirects': [{'from': 'docs/a.md', 'to': 'docs/b.md'}],
             'fallbacks': [{'from_owner': 'platform', 'from': 'docs/a.md', 'to': 'docs/c.md'}]},
        ]
        for data in variants:
            with self.subTest(data=data):
                result = qualify({**documents, path: json.dumps(data)})
                self.assertEqual((result['lane'], result['conclusion']), ('source', 'failure'), result)
        result = qualify({path: '{"schema":1,"schema":1,"redirects":[]}'})
        self.assertEqual(result['conclusion'], 'failure')
        # Optional migration declarations may legitimately be removed.
        result = qualify(documents, before={**documents, path: json.dumps(valid)})
        self.assertEqual((result['lane'], result['conclusion']), ('source', 'success'), result)


class EvidenceAndScopeTests(unittest.TestCase):
    def result(self):
        return qualify({'test/demo/test_contract.py': PYTHON_TEST, 'README.md': '# Guide\n'})

    def plan(self, result):
        return dict(mode='source', candidate_records=copy.deepcopy(result['sources']),
                    test_revisions={subject.owner(row['repository']): {
                        'repository': row['repository'], 'sha': row['candidate_sha'],
                        'role': 'candidate' if index == 0 else 'companion'}
                        for index, row in enumerate(result['sources'])},
                    source_scope=copy.deepcopy(subject.scope_from_evidence(result)))

    def test_primary_and_companion_tests_must_use_their_exact_integration_revisions(self):
        companion = {'kuasar-sandbox/connector': {'base': {}, 'head': {'scripts/test-new.py': PYTHON_TEST},
                                                  'merge': {'scripts/test-new.py': PYTHON_TEST}}}
        result = qualify({'test/demo/test_contract.py': PYTHON_TEST}, companions=companion)
        plan = self.plan(result)
        # Primary order is request order, independent of sorted evidence rows.
        plan['candidate_records'].sort(key=lambda row: row['repository'] != PLATFORM)
        for name, test in plan['test_revisions'].items():
            test['role'] = 'candidate' if name == 'platform' else 'companion'
        subject.verify_plan_scope(plan, result)
        for row in result['sources']:
            name = subject.owner(row['repository'])
            for field, value in (('sha', 'd' * 40), ('sha', row['head_sha']),
                                 ('repository', 'kuasar-sandbox/accelerator'), ('role', 'release')):
                invalid = copy.deepcopy(plan)
                invalid['test_revisions'][name][field] = value
                with self.subTest(owner=name, field=field, value=value), self.assertRaisesRegex(
                        ValueError, 'source tests differ'):
                    subject.verify_plan_scope(invalid, result)

    def test_resolver_requires_current_policy_run_attempt_and_source_mode(self):
        import artifacts
        from test_fixtures import CASES, select_plan
        spec = importlib.util.spec_from_file_location('scope_resolver',
                                                     Path(__file__).with_name('resolve-artifacts.py'))
        resolver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(resolver)
        result = self.result()
        plan = self.plan(result)
        revisions = artifacts.release_test_revisions(
            {name: 'd' * 40 for name in subject.OWNERS if name != 'platform'}, 'e' * 40)
        revisions.update(plan['test_revisions'])
        plan.update(schema=2, framework_sha='a' * 40, baseline={}, case_files=CASES,
                    test_overlays=list(subject.OWNERS), test_revisions=revisions,
                    sources=copy.deepcopy(revisions),
                    lanes={arch: {'products': []} for arch in artifacts.ARCHES},
                    changes={'platform': ['src/older-release-delta.go']})
        select_plan(plan, ['platform'])
        del plan['source_scope']
        with patch.dict(os.environ, ENV):
            good = resolver.attach_source_scope(copy.deepcopy(plan), result, POLICY)
            self.assertEqual(good['source_scope'], subject.scope_from_evidence(result))
            self.assertFalse(resolver.workbench_requested(good))
            artifacts.check_plan(good)
            for field, value in (('policy_sha', 'f' * 40), ('run_id', 'different'), ('run_attempt', '1')):
                evidence = copy.deepcopy(result)
                evidence[field] = value
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'stale source qualification'):
                    resolver.attach_source_scope(copy.deepcopy(plan), evidence, POLICY)
            for mutation in ({'mode': 'exact-assets'}, {'dependency_cutover': {'reviewed': True}}):
                invalid = copy.deepcopy(plan)
                invalid.update(mutation)
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    resolver.attach_source_scope(invalid, result, POLICY)

    def test_source_evidence_cannot_be_downgraded_or_replayed(self):
        result = self.result()
        changes = [
            lambda value: value.update(lane='docs'),
            lambda value: value.update(lane='tooling'),
            lambda value: value.update(lane='full'),
            lambda value: value.update(conclusion='failure'),
            lambda value: value.update(run_id='different'),
            lambda value: value.update(run_attempt='1'),
            lambda value: value.update(policy_sha='f' * 40),
            lambda value: value.update(sources=[]),
            lambda value: value['repositories'][0].pop('head_changed'),
            lambda value: value['repositories'][0].pop('source'),
            lambda value: value['repositories'][0]['source'].pop('candidate'),
            lambda value: value['repositories'][0]['source']['candidate'].update(checks=[]),
            lambda value: value['repositories'][0]['source']['head'].update(findings=['bad syntax']),
            lambda value: value['repositories'][0]['source']['head']['checks'][0].update(parser='source-fixture'),
            lambda value: value['repositories'][0]['source']['candidate']['checks'][0].update(sha='invalid'),
        ]
        with patch.dict(os.environ, ENV):
            for index, change in enumerate(changes):
                invalid = copy.deepcopy(result)
                change(invalid)
                with self.subTest(index=index), self.assertRaises(ValueError):
                    docs_lane.verify(invalid, result['sources'], POLICY)

    def test_runtime_path_cannot_be_inserted_into_an_otherwise_valid_scope(self):
        result = self.result()
        for path in ('src/product.go', 'test/e2e/cases/test_product.sh', 'test/e2e/lib/workspace.py',
                     'test/demo/prepared.py', 'release/inputs/bin-inputs.yaml'):
            plan = self.plan(result)
            plan['source_scope']['changes']['platform'].append(path)
            plan['source_scope']['changes']['platform'].sort()
            with self.subTest(path=path), self.assertRaises(ValueError):
                subject.validate_plan_scope(plan)

    def test_plan_must_preserve_exact_owners_identities_and_ordered_paths(self):
        result = self.result()
        good = self.plan(result)
        subject.validate_plan_scope(good)
        subject.verify_plan_scope(good, result)
        changes = [
            lambda value: value.update(mode='exact-assets'),
            lambda value: value.update(dependency_cutover={'reviewed': True}),
            lambda value: value['source_scope'].update(schema=True),
            lambda value: value['source_scope'].update(extra='unexpected'),
            lambda value: value['source_scope'].update(sources=[]),
            lambda value: value['source_scope'].update(changes={}),
            lambda value: value['source_scope']['changes'].update(connector=[]),
            lambda value: value['source_scope']['changes'].update(platform=['README.md']),
            lambda value: value['source_scope']['changes'].update(platform=['test/demo/test_contract.py', 'README.md']),
            lambda value: value['source_scope']['changes']['platform'].append('test/demo/test_contract.py'),
            lambda value: value['source_scope']['changes'].update(platform=['../test/demo/test_contract.py']),
            lambda value: value['source_scope']['sources'][0].update(head_sha='f' * 40),
        ]
        for index, change in enumerate(changes):
            invalid = copy.deepcopy(good)
            change(invalid)
            with self.subTest(index=index), self.assertRaises(ValueError):
                subject.validate_plan_scope(invalid)

    def test_two_independently_valid_evidence_records_cannot_be_mixed(self):
        first = self.result()
        second = qualify({'test/demo/test_different.py': PYTHON_TEST})
        with self.assertRaises(ValueError):
            subject.verify_plan_scope(self.plan(first), second)
        for field in ('base_sha', 'head_sha', 'candidate_sha', 'pull_request_number', 'base_ref'):
            plan = self.plan(first)
            value = 99 if field == 'pull_request_number' else 'release/v9.9.x' if field == 'base_ref' else 'f' * 40
            plan['candidate_records'][0][field] = value
            plan['source_scope']['sources'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                subject.verify_plan_scope(plan, first)
        plan = self.plan(first)
        del plan['source_scope']
        with self.assertRaises(ValueError):
            subject.verify_plan_scope(plan, first)

    def test_release_manifest_contract_is_preserved_with_source_tests(self):
        import manifest_lane
        evidence = {name: dict(units={unit: {} for unit in
                    ('accelerator', 'connector', 'sandboxer', 'orchestrator', 'runtime', 'vmlinux')},
                    pending=[], acceptance='release exact-assets E2E and Workbench remain required')
                    for name in docs_lane.MANIFESTS}
        with patch.object(manifest_lane, 'References'), \
                patch.object(manifest_lane, 'check_tree', return_value=evidence) as checker:
            result = qualify({'test/demo/test_contract.py': PYTHON_TEST, 'releases/release.yaml': 'version: fixture\n'})
        self.assertEqual((result['lane'], result['conclusion']), ('source', 'success'), result)
        self.assertEqual(checker.call_count, 2)
        self.assertEqual(set(result['repositories'][0]['manifests']), {'head', 'candidate'})
        invalid = copy.deepcopy(result)
        del invalid['repositories'][0]['manifests']
        with patch.dict(os.environ, ENV), self.assertRaises(ValueError):
            docs_lane.verify(invalid, result['sources'], POLICY)


if __name__ == '__main__':
    unittest.main()
