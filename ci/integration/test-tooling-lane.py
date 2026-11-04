#!/usr/bin/env python3
"""Exact tooling classification and syntax-only execution boundaries."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

import docs_lane
import tooling_lane as subject


ROOT = Path(__file__).resolve().parents[2]
REPO = 'kuasar-sandbox/kuasar-sandbox'
ENV = dict(GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='2')


def entry(data=b'', mode='100644', kind='blob'):
    if isinstance(data, str):
        data = data.encode()
    return dict(mode=mode, type=kind,
                sha=hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest())


def tree(files=None, truncated=False):
    return dict(sha='a' * 40, entries=files or {}, truncated=truncated)


class ClassificationTests(unittest.TestCase):
    def test_script_workflow_action_and_cutover_scope(self):
        common = ('ci/nested/check.py', 'ci/nested/check.sh', 'release/publish.sh',
                  'release/nested/reader.py', 'test/release/test_reader.py',
                  '.github/workflows/ci.yml', '.github/workflows/release.yaml',
                  '.github/actions/example/action.yml', '.github/actions/example/index.js',
                  '.github/actions/example/index.mjs', '.github/actions/example/index.cjs',
                  'scripts/ci-source-checks.sh', 'scripts/release-check.go',
                  'scripts/test-release.py', *sorted(subject.COMPONENT_SCRIPTS))
        for repository in docs_lane.REPOSITORIES:
            for path in common:
                with self.subTest(repository=repository, path=path):
                    self.assertEqual(docs_lane.classify(repository, tree(), tree({path: entry()})), [path])
        self.assertEqual(docs_lane.classify(REPO, tree(), tree({subject.CUTOVER: entry()})), [subject.CUTOVER])
        self.assertIsNone(docs_lane.classify('kuasar-sandbox/connector', tree(),
                                            tree({subject.CUTOVER: entry()})))
        for path in subject.PLATFORM_SHELLS:
            self.assertEqual(docs_lane.classify(REPO, tree(), tree({path: entry(mode='100755')})), [path])
            self.assertIsNone(docs_lane.classify('kuasar-sandbox/connector', tree(), tree({path: entry()})))

    def test_packaged_human_attribution_and_license_scope_are_documentation(self):
        notice = 'scripts/release-notices/rootless-containers-proto/NOTICE'
        for repository in ('kuasar-sandbox/orchestrator', 'kuasar-sandbox/guest-runtime'):
            paths = [notice, 'LICENSE_SCOPE.md', 'LICENSE_SCOPE_zh.md', 'scripts/release.sh']
            changes = {path: entry() for path in paths}
            self.assertEqual(docs_lane.classify(repository, tree(), tree(changes)), sorted(paths))
            for unsafe in ('100755', '120000', '160000'):
                self.assertIsNone(docs_lane.classify(repository, tree(), tree({notice: entry(mode=unsafe)})))
            for product in ('scripts/release-notices/rootless-containers-proto/COPYING',
                            'scripts/release-notices/another/NOTICE', 'LICENSE', 'cmd/main.go'):
                self.assertIsNone(docs_lane.classify(repository, tree(), tree({**changes, product: entry()})))
        self.assertIsNone(docs_lane.classify('kuasar-sandbox/connector', tree(), tree({notice: entry()})))

    def test_platform_harness_is_tooling_but_product_cases_remain_full(self):
        for path in ('test/e2e/e2e', 'test/e2e/lib/helper_contract.py',
                     'test/e2e/lib/nested/runner.py', 'test/e2e/test_e2e_runner.py',
                     'test/perf/working_set_directory_test.py'):
            with self.subTest(path=path):
                self.assertEqual(docs_lane.classify(REPO, tree(), tree({path: entry(mode='100755')})), [path])
                self.assertEqual(subject.parser_for(REPO, path), 'python-compile')
                self.assertEqual(subject.check_blob(REPO, path, b'raise RuntimeError("not executed")'), 'python-compile')
                with self.assertRaises(SyntaxError): subject.check_blob(REPO, path, b'return 1')
                self.assertIsNone(docs_lane.classify('kuasar-sandbox/connector', tree(), tree({path: entry()})))
        for path in ('test/e2e/cases/basic.new.sh', 'test/e2e/platform/lib/probe.py',
                     'test/e2e/lib/helper.go', 'test/e2e/lib/config.json', 'test/e2e/test_hidden/product.py'):
            self.assertIsNone(docs_lane.classify(REPO, tree(), tree({path: entry()})))

    def test_product_configuration_and_unknown_paths_remain_full(self):
        rejected = ('src/product.go', 'Makefile', 'workbench/build.py', 'workbench/Dockerfile',
                    'test/e2e/cases/basic.test.sh', 'test/perf/performance.sh',
                    'ci/hosted/e2e-runtime.Dockerfile', 'ci/integration/runner-capabilities.json',
                    'release/inputs/bin-inputs.yaml', 'release/inputs/helper-inputs.yaml',
                    'release/inputs/helper-tools.yaml', 'release/inputs/guide-inputs.yaml',
                    'scripts/product.go', 'scripts/ci-product.txt', '.github/workflows/source.go',
                    'scripts/ci-hidden/product.py', 'scripts/release-hidden/product.go',
                    'ci/../product.py', '/ci/check.py', 'ci//check.py', 'ci/.git/check.py')
        for path in rejected:
            with self.subTest(path=path):
                self.assertIsNone(docs_lane.classify(REPO, tree(),
                                                     tree({'ci/check.py': entry(), path: entry()})))
        self.assertIsNone(docs_lane.classify('unknown/repository', tree(), tree({'ci/check.py': entry()})))

    def test_regular_script_modes_and_unsafe_old_or_new_types(self):
        path = 'release/publish.sh'
        for old, new in ((None, entry(mode='100755')), (entry(), entry(mode='100755')),
                         (entry(mode='100755'), entry()), (entry(mode='100755'), None)):
            with self.subTest(old=old, new=new):
                self.assertEqual(docs_lane.classify(REPO, tree({path: old} if old else {}),
                                                    tree({path: new} if new else {})), [path])
        for bad in (entry(mode='120000'), entry(mode='160000', kind='commit'), entry(mode='100664')):
            for before, after in (({}, {path: bad}), ({path: bad}, {}),
                                  ({path: entry()}, {path: bad}), ({path: bad}, {path: entry()})):
                with self.subTest(before=before, after=after):
                    self.assertIsNone(docs_lane.classify(REPO, tree(before), tree(after)))
        self.assertIsNone(docs_lane.classify(REPO, tree(), tree({'README.md': entry(mode='100755')})))
        self.assertIsNone(docs_lane.classify(REPO, tree({path: entry()}), tree({'src/product.sh': entry()})))
        self.assertIsNone(docs_lane.classify(REPO, tree(truncated=True), tree({path: entry()})))
        self.assertIsNone(docs_lane.classify(REPO, tree(), tree({path: entry()}, truncated=True)))


class ParserTests(unittest.TestCase):
    def test_docs_and_non_yaml_tooling_need_no_site_packages(self):
        code = ('import sys; sys.path.insert(0, ' + repr(str(ROOT / 'ci/integration')) + '); '
                'import docs_lane, tooling_lane; '
                'assert tooling_lane.check_blob("kuasar-sandbox/kuasar-sandbox", '
                '"ci/check.py", b"x = 1\\n") == "python-compile"')
        result = subprocess.run([sys.executable, '-S', '-B', '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_python_compile_checks_scope_encoding_and_never_executes(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'executed'
            code = ('from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("executed")\n'
                    'raise RuntimeError("must never execute")\n').encode()
            self.assertEqual(subject.check_blob(REPO, 'ci/check.py', code), 'python-compile')
            self.assertFalse(marker.exists())
        self.assertEqual(subject.check_blob(REPO, 'ci/check.py', b'# coding: latin-1\nx = "\xe9"\n'),
                         'python-compile')
        for source in (b'return 1', b'break', b'await f()', b'x =', b'\x00'):
            with self.subTest(source=source), self.assertRaises((SyntaxError, ValueError)):
                subject.check_blob(REPO, 'ci/check.py', source)

    def test_shell_parse_does_not_source_expand_or_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'executed'
            source = ('touch ' + str(marker) + '\necho "$(touch ' + str(marker) + ')"\n'
                      'source /nonexistent/candidate/startup\nexit 77\n').encode()
            self.assertEqual(subject.check_blob(REPO, 'release/publish.sh', source), 'bash-syntax')
            self.assertFalse(marker.exists())
        with self.assertRaises(ValueError):
            subject.check_blob(REPO, 'release/publish.sh', b'if true; then\n')
        for path in subject.PLATFORM_SHELLS:
            self.assertEqual(subject.check_blob(REPO, path, b'exit 77\n'), 'bash-syntax')

    @unittest.skipUnless(shutil.which('node'), 'Node is not installed in this test environment')
    def test_javascript_parse_does_not_execute_and_checks_module_suffixes(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'executed'
            source = ('require("fs").writeFileSync(' + json.dumps(str(marker)) + ', "executed");\n'
                      'throw Error("must never execute");\n').encode()
            for suffix in ('.js', '.cjs'):
                self.assertEqual(subject.check_blob(REPO, '.github/actions/example/index' + suffix, source),
                                 'node-syntax')
            self.assertEqual(subject.check_blob(REPO, '.github/actions/example/index.mjs',
                                               b'export const value = 1;\n'), 'node-syntax')
            self.assertFalse(marker.exists())
        with self.assertRaises(ValueError):
            subject.check_blob(REPO, '.github/actions/example/index.js', b'const x = ;\n')

    @unittest.skipUnless(shutil.which('gofmt'), 'gofmt is not installed in this test environment')
    def test_go_syntax_does_not_run_init(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'executed'
            source = ('package main\nimport "os"\nfunc init() { os.WriteFile(' +
                      json.dumps(str(marker)) + ', []byte("executed"), 0600) }\nfunc main() {}\n').encode()
            self.assertEqual(subject.check_blob(REPO, 'scripts/release-check.go', source), 'go-syntax')
            self.assertFalse(marker.exists())
        with self.assertRaises(ValueError):
            subject.check_blob(REPO, 'scripts/release-check.go', b'package main\nfunc main( {\n')

    def test_yaml_is_safe_single_document_syntax_only(self):
        self.assertEqual(subject.check_blob(REPO, '.github/workflows/ci.yml',
                                           b'name: CI\non: push\njobs:\n  check:\n    runs-on: ubuntu-latest\n'
                                           b'    steps:\n      - run: echo fixture\n'), 'yaml-syntax')
        for source in (b'name: [\n', b'name: one\n---\nname: two\n',
                       b'!!python/object/apply:os.system ["exit 91"]\n'):
            with self.subTest(source=source), self.assertRaises((ValueError, yaml.YAMLError)):
                subject.check_blob(REPO, '.github/workflows/ci.yml', source)

    def test_workflow_shell_defaults_and_expressions_are_not_candidate_execution(self):
        # Valid Actions interpolation and inherited shells are not standalone
        # Bash/Python source. A syntax-only gate must not reject the templates.
        for document in (
            {'defaults': {'run': {'shell': 'python'}}, 'jobs': {'check': {'steps': [
                {'run': 'for value in [1, 2]:\n    print(value)\n'}]}}},
            {'jobs': {'check': {'defaults': {'run': {'shell': 'python'}}, 'steps': [
                {'run': 'for value in [1, 2]:\n    print(value)\n'}]}}},
            {'jobs': {'check': {'steps': [{'shell': 'python', 'run': 'value = ${{ inputs.value }}\n'}]}}},
        ):
            with self.subTest(document=document):
                self.assertEqual(subject.check_blob(REPO, '.github/workflows/ci.yml',
                                                   yaml.safe_dump(document).encode()), 'yaml-syntax')

    def test_parser_process_has_private_cwd_no_preloads_and_timeout(self):
        captured = {}
        def run(command, **kwargs):
            captured.update(kwargs, command=command)
            self.assertEqual(Path(command[-1]).parent, Path(kwargs['cwd']))
            self.assertEqual(Path(command[-1]).read_bytes(), b'echo fixture\n')
            self.assertEqual(Path(command[-1]).name, 'candidate.sh')
            return subprocess.CompletedProcess(command, 0, b'', b'')
        with patch.dict(os.environ, GH_TOKEN='never-forward', BASH_ENV='/candidate/startup',
                        ENV='/candidate/startup', NODE_OPTIONS='--require /candidate/preload',
                        NODE_PATH='/candidate/modules', PYTHONPATH='/candidate/imports'), \
             patch.object(subject.subprocess, 'run', side_effect=run):
            subject.check_blob(REPO, 'ci/nested/check.sh', b'echo fixture\n')
        self.assertEqual(captured['command'][:-1], ['bash', '--noprofile', '--norc', '-n'])
        self.assertEqual(captured['timeout'], 20)
        self.assertLessEqual(set(captured['env']), {'PATH', 'SYSTEMROOT', 'HOME', 'LC_ALL'})
        self.assertEqual(captured['env']['HOME'], captured['cwd'])
        self.assertFalse(Path(captured['cwd']).exists())

    def test_cutover_schema_is_checked_without_selection_or_network(self):
        self.assertEqual(subject.check_blob(REPO, subject.CUTOVER, b'{"schema":1,"requests":[]}'),
                         'dependency-cutover-schema')
        for source in (b'{"schema":true,"requests":[]}', b'{"schema":1,"schema":1,"requests":[]}',
                       b'{"schema":1,"requests":[{}]}'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                subject.check_blob(REPO, subject.CUTOVER, source)
        # The repository's real declaration exercises all existing records.
        with patch.object(subprocess, 'check_output', side_effect=AssertionError('no API or command execution')), \
             patch.object(subprocess, 'run', side_effect=AssertionError('no source or publication execution')):
            self.assertEqual(subject.check_blob(REPO, subject.CUTOVER, (ROOT / subject.CUTOVER).read_bytes()),
                             'dependency-cutover-schema')

    def test_deleted_tooling_reads_no_blob_and_parser_failures_are_findings(self):
        with patch.object(subject, 'check_blob', side_effect=AssertionError('deleted file cannot be parsed')):
            result = subject.check_tree(REPO, tree(), ['ci/deleted.py'], lambda *_: self.fail('read deleted blob'))
        self.assertEqual(result, dict(checks=[dict(path='ci/deleted.py', state='deleted')], findings=[]))
        subject.verify(result, ['ci/deleted.py'], REPO)
        for error in (FileNotFoundError('missing parser'), subprocess.TimeoutExpired('parser', 20),
                      UnicodeError('bad encoding'), SyntaxError('bad syntax')):
            with self.subTest(error=error), patch.object(subject, 'check_blob', side_effect=error):
                result = subject.check_tree(REPO, tree({'ci/check.py': entry()}), ['ci/check.py'], lambda *_: b'')
                self.assertTrue(result['findings'])
                with self.assertRaises(ValueError):
                    subject.verify(result, ['ci/check.py'], REPO)


class QualificationTests(unittest.TestCase):
    def qualify(self, candidate, *, head=None, before=None, companion=None):
        before = before or {}
        head = candidate if head is None else head
        record = dict(repository=REPO, base_sha='b' * 40, head_sha='c' * 40,
                      candidate_sha='d' * 40, base_ref='main', pull_request_number=7)
        records = [record]
        if companion is not None:
            records.append(dict(record, repository='kuasar-sandbox/connector'))
        views = {'b' * 40: before, 'c' * 40: head, 'd' * 40: candidate}
        blobs = {entry(value)['sha']: value.encode() if isinstance(value, str) else value
                 for view in views.values() for value in view.values()}
        def read_tree(repository, commit):
            value = views[commit] if repository == REPO else ({} if commit == 'b' * 40 else companion)
            return tree({name: entry(data) for name, data in value.items()})
        with patch.dict(os.environ, ENV), patch.object(docs_lane, 'tree', side_effect=read_tree), \
             patch.object(docs_lane, 'blob', side_effect=lambda _, identity: blobs[identity]), \
             patch.object(docs_lane, 'check_tree', return_value=dict(checked_files=[], findings=[])):
            result = docs_lane.qualify(records, 'e' * 40)
            if result['lane'] != 'full' and result['conclusion'] == 'success':
                docs_lane.verify(result, records, 'e' * 40)
        return result

    def test_tooling_docs_and_documentation_companions_share_light_lane(self):
        result = self.qualify({'ci/check.py': 'x = 1\n', 'docs/topic.md': '# Topic\n'},
                              companion={'README.md': '# Companion\n'})
        self.assertEqual((result['lane'], result['conclusion']), ('tooling', 'success'))
        for row in result['repositories']:
            self.assertEqual(set(row['tooling']), {'head', 'candidate'})
        self.assertEqual(result['repositories'][1]['tooling']['candidate']['checks'], [])

    def test_product_companion_or_product_head_forces_full_without_parsing(self):
        with patch.object(subject, 'check_blob', side_effect=AssertionError('full lane cannot parse tooling')):
            result = self.qualify({'ci/check.py': 'x = 1\n'}, companion={'main.go': 'package main\n'})
            self.assertEqual(result['lane'], 'full')
            result = self.qualify({'ci/check.py': 'x = 1\n'}, head={'main.go': 'package main\n'})
            self.assertEqual(result['lane'], 'full')

    def test_tooling_head_is_checked_even_when_merge_cancels_its_change(self):
        result = self.qualify({'README.md': '# Doc\n'}, head={'ci/check.py': 'x = 1\n'})
        self.assertEqual(result['lane'], 'tooling')
        row = result['repositories'][0]
        self.assertEqual([check['path'] for check in row['tooling']['head']['checks']], ['ci/check.py'])
        self.assertEqual(row['tooling']['candidate']['checks'], [])
        result = self.qualify({'ci/check.py': 'x = 1\n'}, head={'ci/check.py': 'return 1\n'})
        self.assertEqual((result['lane'], result['conclusion']), ('tooling', 'failure'))
        self.assertTrue(result['repositories'][0]['tooling']['head']['findings'])
        self.assertEqual(result['repositories'][0]['tooling']['candidate']['findings'], [])

    def test_mixed_manifest_tooling_keeps_both_manifest_views(self):
        import manifest_lane
        manifest_evidence = {name: dict(units={unit: {} for unit in
                            ('accelerator', 'connector', 'sandboxer', 'orchestrator', 'runtime', 'vmlinux')},
                            pending=[], acceptance='release exact-assets E2E and Workbench remain required')
                             for name in docs_lane.MANIFESTS}
        with patch.object(manifest_lane, 'References'), \
             patch.object(manifest_lane, 'check_tree', return_value=manifest_evidence) as checker:
            result = self.qualify({'ci/check.py': 'x = 1\n', 'releases/release.yaml': 'version: fixture\n'})
        self.assertEqual((result['lane'], result['conclusion']), ('tooling', 'success'))
        self.assertEqual(checker.call_count, 2)
        self.assertEqual(set(result['repositories'][0]['manifests']), {'head', 'candidate'})
        with patch.dict(os.environ, ENV), self.assertRaises(ValueError):
            bad = copy.deepcopy(result)
            del bad['repositories'][0]['manifests']
            docs_lane.verify(bad, result['sources'], 'e' * 40)
        with patch.object(manifest_lane, 'References'), \
             patch.object(manifest_lane, 'check_tree', return_value=manifest_evidence) as checker:
            result = self.qualify({'ci/check.py': 'x = 1\n'},
                                  head={'releases/release.yaml': 'version: fixture\n'})
        self.assertEqual((result['lane'], result['conclusion']), ('tooling', 'success'))
        self.assertEqual(checker.call_count, 2)

    def test_empty_candidate_diff_stays_full(self):
        result = self.qualify({}, head={'ci/check.py': 'x = 1\n'})
        self.assertEqual(result['lane'], 'full')

    def test_incomplete_stale_wrong_lane_and_parser_evidence_rejected(self):
        result = self.qualify({'ci/check.py': 'x = 1\n'})
        mutations = [
            lambda value: value.update(lane='docs'),
            lambda value: value.update(lane='manifest'),
            lambda value: value.update(conclusion='failure'),
            lambda value: value.update(run_id='124'),
            lambda value: value.update(run_attempt='1'),
            lambda value: value.update(policy_sha='f' * 40),
            lambda value: value.update(sources=[]),
            lambda value: value['repositories'][0].pop('head_changed'),
            lambda value: value['repositories'][0].pop('tooling'),
            lambda value: value['repositories'][0]['tooling'].pop('head'),
            lambda value: value['repositories'][0]['tooling']['candidate'].update(checks=[]),
            lambda value: value['repositories'][0]['tooling']['candidate'].update(findings=['bad syntax']),
            lambda value: value['repositories'][0]['tooling']['candidate']['checks'][0].update(sha='bad'),
            lambda value: value['repositories'][0]['tooling']['candidate']['checks'][0].update(parser='yaml-syntax'),
        ]
        with patch.dict(os.environ, ENV):
            for index, mutation in enumerate(mutations):
                bad = copy.deepcopy(result)
                mutation(bad)
                with self.subTest(index=index), self.assertRaises(ValueError):
                    docs_lane.verify(bad, result['sources'], 'e' * 40)


class WorkflowTests(unittest.TestCase):
    def test_tooling_requires_resolve_success_skipped_product_stages_and_exact_source_mode(self):
        jobs = yaml.safe_load((ROOT / '.github/workflows/integration-tests.yml').read_text())['jobs']
        script = jobs['results']['steps'][0]['run']
        good = dict(DOCS_LANE='tooling', INTEGRATION_MODE='source', RESOLVE_RESULT='success',
                    X86_RESULT='skipped', ARM_RESULT='skipped', SOURCE_RESULT='skipped',
                    WORKBENCH_SELECTED='false', WORKBENCH_RESULT='skipped', WORKBENCH_RELEASE_RESULT='skipped')
        self.assertEqual(subprocess.run(['bash', '-c', script], env={**os.environ, **good}).returncode, 0)
        for key in good:
            for value in ('', 'failure', 'cancelled', 'neutral'):
                with self.subTest(key=key, value=value):
                    self.assertNotEqual(subprocess.run(['bash', '-c', script],
                                        env={**os.environ, **good, key: value}, capture_output=True).returncode, 0)
        self.assertNotEqual(subprocess.run(['bash', '-c', script],
                            env={**os.environ, **good, 'INTEGRATION_MODE': 'exact-assets'},
                            capture_output=True).returncode, 0)
        for name in ('x86_64', 'aarch64', 'source-checks', 'workbench-native'):
            self.assertIn("needs.resolve.outputs.lane == 'full'", jobs[name]['if'])
        for name in ('Package exact trusted executor objects', 'Resolve exact immutable plan', 'Upload resolved plan'):
            step = next(step for step in jobs['resolve']['steps'] if step.get('name') == name)
            self.assertIn('!contains', step['if'])
            self.assertIn('"tooling"', step['if'])
        classify = next(step for step in jobs['resolve']['steps'] if step.get('id') == 'docs')
        self.assertIn('docs-policy/ci/integration/docs_lane.py', classify['run'])
        self.assertIn("echo 'lane=full'", classify['run'])
        verify = next(step for step in jobs['results']['steps'] if step.get('name', '').startswith('Bind docs success'))
        self.assertIn('"tooling"', verify['if'])
        self.assertIn('--verify plan/docs-result.json', verify['run'])


if __name__ == '__main__':
    unittest.main()
