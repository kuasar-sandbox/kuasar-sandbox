#!/usr/bin/env python3
"""Source-only scheduling keeps real source tests and exact final evidence."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/integration-tests.yml').read_text())
JOBS = WORKFLOW['jobs']


def step(job, name):
    return next(row for row in JOBS[job]['steps'] if row.get('name') == name)


def run_shell(script, env, *, cwd=None):
    clean = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT') if key in os.environ}
    return subprocess.run(['bash', '--noprofile', '--norc', '-c', script],
                          cwd=cwd, env={**clean, **env}, capture_output=True, text=True)


class StageGateTests(unittest.TestCase):
    def setUp(self):
        self.script = step('results', 'Require every selected stage')['run']

    def successful(self, lane, *, mode='source', native=False):
        result = dict(DOCS_LANE=lane, INTEGRATION_MODE=mode, RESOLVE_RESULT='success',
                      X86_RESULT='skipped', ARM_RESULT='skipped', SOURCE_RESULT='skipped',
                      WORKBENCH_SELECTED='false', WORKBENCH_RESULT='skipped',
                      WORKBENCH_RELEASE_RESULT='skipped')
        if lane == 'source':
            result['SOURCE_RESULT'] = 'success'
        if lane == 'full':
            result.update(X86_RESULT='success', ARM_RESULT='success',
                          SOURCE_RESULT='success' if mode == 'source' else 'skipped',
                          WORKBENCH_RELEASE_RESULT='success' if mode == 'exact-assets' else 'skipped')
            if native:
                result.update(WORKBENCH_SELECTED='true', WORKBENCH_RESULT='success')
        return result

    def test_each_qualification_path_accepts_only_its_complete_stage_set(self):
        fixtures = [self.successful(lane) for lane in ('docs', 'manifest', 'tooling', 'source')]
        fixtures.extend([self.successful('full'), self.successful('full', native=True),
                         self.successful('full', mode='exact-assets')])
        for good in fixtures:
            with self.subTest(lane=good['DOCS_LANE'], mode=good['INTEGRATION_MODE'],
                              native=good['WORKBENCH_SELECTED']):
                result = run_shell(self.script, good)
                self.assertEqual(result.returncode, 0, result.stderr)
                for key in good:
                    if key in ('DOCS_LANE', 'INTEGRATION_MODE'):
                        continue
                    for bad in ('', 'failure', 'cancelled', 'neutral', 'success', 'skipped'):
                        if bad == good[key]:
                            continue
                        with self.subTest(field=key, bad=bad):
                            result = run_shell(self.script, {**good, key: bad})
                            self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_source_lane_rejects_missing_tests_or_any_product_execution(self):
        good = self.successful('source')
        for status in ('skipped', 'failure', 'cancelled', ''):
            with self.subTest(source=status):
                self.assertNotEqual(run_shell(self.script, {**good, 'SOURCE_RESULT': status}).returncode, 0)
        for key in ('X86_RESULT', 'ARM_RESULT', 'WORKBENCH_RESULT', 'WORKBENCH_RELEASE_RESULT'):
            with self.subTest(product=key):
                self.assertNotEqual(run_shell(self.script, {**good, key: 'success'}).returncode, 0)
        self.assertNotEqual(run_shell(self.script, {**good, 'WORKBENCH_SELECTED': 'true'}).returncode, 0)

    def test_light_and_source_paths_cannot_accept_exact_assets_or_unknown_modes(self):
        for lane in ('docs', 'manifest', 'tooling', 'source'):
            for mode in ('exact-assets', '', 'unknown'):
                with self.subTest(lane=lane, mode=mode):
                    self.assertNotEqual(run_shell(self.script, {**self.successful(lane),
                                                               'INTEGRATION_MODE': mode}).returncode, 0)
        for lane in ('', 'unknown', 'source-only'):
            self.assertNotEqual(run_shell(self.script, {**self.successful('source'), 'DOCS_LANE': lane}).returncode, 0)


class SchedulingTests(unittest.TestCase):
    def test_only_source_jobs_expand_to_the_new_lane(self):
        condition = JOBS['source-checks']['if']
        self.assertIn("(needs.resolve.outputs.lane == 'full' || needs.resolve.outputs.lane == 'source')", condition)
        self.assertIn("inputs.mode == 'source'", condition)
        for name in ('x86_64', 'aarch64', 'workbench-native'):
            condition = JOBS[name]['if']
            self.assertIn("needs.resolve.outputs.lane == 'full'", condition)
            self.assertNotIn("needs.resolve.outputs.lane == 'source'", condition)
        self.assertIn("inputs.mode == 'exact-assets'", JOBS['workbench-release']['if'])
        for name in ('Package exact trusted executor objects', 'Resolve exact immutable plan', 'Upload resolved plan'):
            condition = step('resolve', name)['if']
            self.assertIn('!contains', condition)
            self.assertIn('["docs", "manifest", "tooling"]', condition)
            self.assertNotIn('"source"', condition)
        paths = step('resolve', 'Upload resolved plan')['with']['path'].splitlines()
        self.assertIn('docs-result.json', paths)
        self.assertIn('plan.json', paths)
        self.assertIn('source-inputs.tar', paths)

    def test_source_finalization_requires_both_independent_evidence_checks(self):
        for name in ('Download source evidence in separate artifact directories',
                     'Bind every source result to its current successful producer'):
            condition = step('results', name)['if']
            self.assertIn("needs.resolve.outputs.lane == 'source'", condition)
            self.assertIn("inputs.mode == 'source'", condition)
        for name in ('Check out the same trusted docs verifier',
                     'Bind docs success to this exact source set and attempt'):
            condition = step('results', name)['if']
            self.assertIn('["docs", "manifest", "tooling", "source"]', condition)
        policy = step('results', 'Check out the same trusted docs verifier')
        self.assertEqual(policy['with']['ref'], '${{ needs.resolve.outputs.policy_sha }}')
        self.assertFalse(policy['with']['persist-credentials'])
        admission = step('results', 'Recheck the current exact source candidate before acceptance')
        self.assertEqual(admission['if'], "inputs.mode == 'source'")
        self.assertIn('validate-source-set.sh', admission['run'])
        self.assertEqual(JOBS['results']['needs'], ['resolve', 'x86_64', 'aarch64',
                                                   'source-checks', 'workbench-native', 'workbench-release'])

    def test_resolve_binds_source_scope_without_requesting_native_products(self):
        script = step('resolve', 'Resolve exact immutable plan')['run']
        fake_python = '''import json, os, pathlib, sys
args = sys.argv[1:]
if args and args[0] == '-B': args = args[1:]
with open(os.environ['COMMAND_LOG'], 'a') as output: output.write(json.dumps(args) + '\\n')
script = args[0]
if script.endswith('/resolve-artifacts.py'):
    pathlib.Path('plan.json').write_text('{}')
    pathlib.Path('workbench-selected.txt').write_text('true\\n')
    if '--source-scope' in args:
        assert pathlib.Path(args[args.index('--source-scope') + 1]).is_file()
elif script.endswith('/source_results.py'):
    print('["A"]')
elif script.endswith('/workbench.py') or script.endswith('/capability_routing.py'):
    pass
else:
    raise RuntimeError('unexpected command: ' + repr(args))
'''
        for lane, mode in (('source', 'source'), ('full', 'source'), ('', 'exact-assets')):
            with self.subTest(lane=lane, mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                binary = root / 'bin'
                binary.mkdir()
                executable = binary / 'python3'
                executable.write_text('#!' + sys.executable + '\n' + fake_python)
                executable.chmod(0o755)
                (root / 'docs-result.json').write_text('{}')
                output, log = root / 'output', root / 'commands'
                env = dict(PATH=str(binary) + os.pathsep + os.environ['PATH'],
                           INTEGRATION_MODE=mode, QUALIFICATION_LANE=lane,
                           FRAMEWORK_SHA='a' * 40, SOURCE_CHECK_GROUPS='true',
                           WORKBENCH_SELECTION_ARTIFACT='', GITHUB_OUTPUT=str(output),
                           GITHUB_RUN_ID='123', COMMAND_LOG=str(log))
                result = run_shell(script, env, cwd=root)
                self.assertEqual(result.returncode, 0, result.stderr)
                commands = [json.loads(line) for line in log.read_text().splitlines()]
                resolve = next(args for args in commands if args[0].endswith('/resolve-artifacts.py'))
                self.assertEqual('--source-scope' in resolve, lane == 'source')
                self.assertEqual('--stage' in resolve, mode == 'exact-assets')
                values = dict(line.split('=', 1) for line in output.read_text().splitlines())
                self.assertEqual(values['workbench'], 'false' if lane == 'source' else 'true')
                self.assertEqual(json.loads(values['source_groups']), ['A'])
                self.assertTrue(any(args[0].endswith('/workbench.py') for args in commands))


class EvidenceBindingTests(unittest.TestCase):
    def execute_verifier(self, directory, *, lane='source', docs_exit=0, scope_exit=0):
        root = Path(directory)
        trusted = root / 'docs-policy/ci/integration'
        trusted.mkdir(parents=True)
        (trusted / 'docs_lane.py').write_text('''import json, os, sys
with open(os.environ['BINDING_LOG'], 'a') as output: output.write(json.dumps(['docs', sys.argv[1:]]) + '\\n')
raise SystemExit(int(os.environ['DOCS_EXIT']))
''')
        (trusted / 'source_lane.py').write_text('''import json, os
def verify_plan_scope(plan, evidence):
    with open(os.environ['BINDING_LOG'], 'a') as output: output.write(json.dumps(['scope', plan, evidence]) + '\\n')
    if os.environ['SCOPE_EXIT'] != '0': raise ValueError('scope does not match plan')
''')
        # An accidentally imported working-directory module must not replace
        # the classifier selected from the admitted policy revision.
        (root / 'source_lane.py').write_text('raise RuntimeError("untrusted source module imported")\n')
        plan = root / 'plan'
        plan.mkdir()
        (plan / 'plan.json').write_text('{"plan": "exact"}')
        (plan / 'docs-result.json').write_text('{"scope": "exact"}')
        log = root / 'binding'
        env = dict(QUALIFICATION_LANE=lane, DOCS_POLICY_SHA='b' * 40,
                   BINDING_LOG=str(log), DOCS_EXIT=str(docs_exit), SCOPE_EXIT=str(scope_exit),
                   PYTHONDONTWRITEBYTECODE='1')
        result = run_shell(step('results', 'Bind docs success to this exact source set and attempt')['run'],
                           env, cwd=root)
        events = [json.loads(line) for line in log.read_text().splitlines()]
        return result, events, root / 'validation.json'

    def test_source_binding_runs_after_policy_verification_and_uses_same_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            result, events, validation = self.execute_verifier(directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([event[0] for event in events], ['docs', 'scope'])
            self.assertEqual(events[0][1], ['--policy-sha', 'b' * 40, '--verify', 'plan/docs-result.json'])
            self.assertEqual(events[1][1:], [{'plan': 'exact'}, {'scope': 'exact'}])
            self.assertEqual(json.loads(validation.read_text()), {'scope': 'exact'})

    def test_failed_or_stale_policy_and_mismatched_scope_cannot_publish_success(self):
        for docs_exit, scope_exit, expected in ((17, 0, ['docs']), (0, 1, ['docs', 'scope'])):
            with self.subTest(docs_exit=docs_exit, scope_exit=scope_exit), tempfile.TemporaryDirectory() as directory:
                result, events, validation = self.execute_verifier(directory, docs_exit=docs_exit, scope_exit=scope_exit)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([event[0] for event in events], expected)
                self.assertFalse(validation.exists())

    def test_existing_light_lanes_do_not_require_a_source_plan(self):
        for lane in ('docs', 'manifest', 'tooling'):
            with self.subTest(lane=lane), tempfile.TemporaryDirectory() as directory:
                result, events, validation = self.execute_verifier(directory, lane=lane, scope_exit=1)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([event[0] for event in events], ['docs'])
                self.assertTrue(validation.exists())


if __name__ == '__main__':
    unittest.main()
