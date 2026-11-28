#!/usr/bin/env python3
"""Source-only scheduling keeps real source tests and exact final evidence."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/integration-tests.yml').read_text())
JOBS = WORKFLOW['jobs']
SPEC = importlib.util.spec_from_file_location('source_recheck', ROOT / 'ci/integration/recheck-source-set.py')
RECHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECHECK)


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
    def test_declared_stages_use_existing_isolated_matrix_and_final_collector(self):
        # Stage activation changes only trusted plan/groups, not execution
        # privileges, the image, per-job instance, or final success conditions.
        job = JOBS['source-checks']
        self.assertEqual(job['strategy']['matrix'],
                         {'group': '${{ fromJSON(needs.resolve.outputs.source_groups) }}'})
        self.assertFalse(job['strategy']['fail-fast'])
        fetch = step('source-checks', 'Fetch exact test sources without a compiler')['run']
        self.assertIn("'${{ matrix.group }}' > source-checks/.source-group", fetch)
        execute = step('source-checks', 'Run the complete required source gate in Workbench system mode')['run']
        self.assertIn('--cpus 2 --memory-gib 8', execute)
        self.assertIn('--materialized --group "$(cat /src/.source-group)"', execute)
        self.assertIn('--reuid "$source_uid"', execute)
        self.assertIn('install_readers', execute)
        self.assertIn('install_runtime_reader', execute)
        self.assertEqual(step('source-checks', 'Upload source checks and system diagnostics')['with']['name'],
                         'integration-source-result-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.group }}')
        final = step('results', 'Bind every source result to its current successful producer')['run']
        self.assertIn('source_results.py', final)
        self.assertIn('--groups', final)
        self.assertIn('"$SOURCE_GROUPS"', final)
        self.assertEqual(step('results', 'Bind every source result to its current successful producer')['env']['SOURCE_GROUPS'],
                         '${{ needs.resolve.outputs.source_groups }}')

    def test_owner_schedule_uses_one_exact_candidate_framework_without_a_second_job_path(self):
        entry = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']
        self.assertIn("github.event_name == 'pull_request'", entry['ci']['if'])
        self.assertIn('github.event.pull_request.head.repo.full_name == github.repository', entry['ci']['if'])
        self.assertEqual(entry['ci']['uses'], './.github/workflows/integration-tests.yml')
        self.assertTrue(entry['ci']['with']['source_check_groups'])
        self.assertEqual(entry['ci']['with']['candidate_sha'], '${{ github.sha }}')
        self.assertTrue(entry['trusted-fork']['uses'].endswith('/ci-entry.yml@main'))
        self.assertEqual(step('resolve', 'Check out exact trusted framework')['with']['ref'], '${{ job.workflow_sha }}')
        self.assertEqual(step('resolve', 'Resolve exact immutable plan')['env']['FRAMEWORK_SHA'], '${{ job.workflow_sha }}')
        self.assertEqual(JOBS['source-checks']['strategy']['matrix'],
                         {'group': '${{ fromJSON(needs.resolve.outputs.source_groups) }}'})
        self.assertFalse(JOBS['source-checks']['strategy']['fail-fast'])
        for job in ('source-checks', 'results'):
            checkout = next(row for row in JOBS[job]['steps'] if row.get('with', {}).get('path') == 'framework')
            self.assertEqual(checkout['with']['ref'], '${{ needs.resolve.outputs.framework_sha }}')
            self.assertFalse(checkout['with']['persist-credentials'])
        worker = step('source-checks', 'Run the complete required source gate in Workbench system mode')['run']
        self.assertIn('--cpus 2 --memory-gib 8', worker)
        self.assertIn('--group "$(cat /src/.source-group)"', worker)
        self.assertIn('id -u nobody', worker)
        self.assertIn('--inh-caps=-all --ambient-caps=-all', worker)
        collector = step('results', 'Bind every source result to its current successful producer')
        self.assertEqual(collector['env']['SOURCE_GROUPS'], '${{ needs.resolve.outputs.source_groups }}')
        self.assertIn('--groups "$SOURCE_GROUPS"', collector['run'])

    def test_current_source_barriers_precede_queued_expensive_stages(self):
        architecture = yaml.safe_load((ROOT / '.github/workflows/integration-architecture.yml').read_text())
        self.assertEqual(architecture['permissions'], {'contents': 'read', 'pull-requests': 'read'})
        build_names = [row.get('name') for row in architecture['jobs']['build']['steps']]
        self.assertLess(build_names.index('Validate trusted architecture and shard selection'),
                        build_names.index('Recheck exact source set before expensive work'))
        # Keep the source-check job's stronger whole-job credential-free
        # contract; its existing resolve admission remains authoritative.
        self.assertNotIn('GH_TOKEN', json.dumps(JOBS['source-checks']))
        selections = [(JOBS['workbench-native'], 'Qualify the complete native toolchain with ordinary UID builds')]
        selections += [(architecture['jobs'][name], target) for name, target in (
            ('build', 'Build trusted native archive readers in Workbench'),
            ('prepare', 'Bootstrap archive execution prerequisites'),
            ('e2e', 'Run the selected capability group from immutable inputs'),
            ('performance', 'Bootstrap declared native execution profile'))]
        for job, target in selections:
            steps = job['steps']
            index = next(i for i, row in enumerate(steps)
                         if row.get('name') == 'Recheck exact source set before expensive work')
            barrier = steps[index]
            with self.subTest(target=target):
                self.assertLess(index, next(i for i, row in enumerate(steps) if row.get('name') == target))
                # Failures must stop the ordinary following steps. Tokens stay
                # in this control step, outside all candidate build commands.
                self.assertNotIn('continue-on-error', barrier)
                self.assertEqual(barrier['env'], {'GH_TOKEN': '${{ github.token }}'})
                self.assertIn('recheck-source-set.py', barrier['run'])
                self.assertIn('--framework-sha', barrier['run'])
                self.assertNotIn('GH_TOKEN', job.get('env', {}))
                for row in steps[index + 1:]:
                    if 'integration-build-' in str(row.get('with', {}).get('name', '')) or \
                       'integration-prepared-' in str(row.get('with', {}).get('name', '')):
                        self.assertNotIn('always()', row.get('if', ''))

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
    print('["owner-platform", "owner-connector"]')
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
                self.assertEqual(json.loads(values['source_groups']), ['owner-platform', 'owner-connector'])
                schedule = next(args for args in commands if args[0].endswith('/source_results.py'))
                self.assertEqual(schedule, ['framework/ci/integration/source_results.py', '--plan', 'plan.json',
                                            '--schedule', 'grouped'])
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


class EarlyAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.primary = dict(repository='kuasar-sandbox/orchestrator', pull_request_number=1,
                            candidate_sha='a' * 40, base_sha='b' * 40, head_sha='c' * 40, base_ref='main')
        self.companion = dict(repository='kuasar-sandbox/connector', pull_request_number=2,
                              candidate_sha='d' * 40, base_sha='e' * 40, head_sha='f' * 40, base_ref='main')
        self.plan = dict(mode='source', framework_sha='1' * 40,
                         candidate_records=[self.primary, self.companion])
        self.responses = {}
        for row in self.plan['candidate_records']:
            prefix = '/repos/' + row['repository']
            pr = dict(number=row['pull_request_number'], state='open', draft=False,
                      base=dict(ref=row['base_ref'], sha=row['base_sha'], repo={'full_name': row['repository']}),
                      head={'sha': row['head_sha']}, merge_commit_sha=row['candidate_sha'], body='')
            self.responses[prefix + '/pulls/' + str(row['pull_request_number'])] = pr
            self.responses[prefix + '/git/commits/' + row['candidate_sha']] = {
                'sha': row['candidate_sha'], 'parents': [{'sha': row['base_sha']}, {'sha': row['head_sha']}]}
            self.responses[prefix + '/git/ref/heads/main'] = {'object': {'sha': row['base_sha']}}
            for kind, sha in [('merge', row['candidate_sha']), ('head', row['head_sha'])]:
                ref = f'pull/{row["pull_request_number"]}/{kind}'
                self.responses[prefix + '/git/ref/' + ref] = {'ref': 'refs/' + ref, 'object': {'sha': sha}}
            self.responses[prefix + '/commits/' + row['head_sha'] + '/branches-where-head'] = [{'name': 'candidate'}]
        self.responses['/repos/kuasar-sandbox/orchestrator/pulls/1']['body'] = (
            '<!-- kuasar-ci-companions\nkuasar-sandbox/connector#2\n-->')
        self.payload = self.root / 'responses.json'
        binary = self.root / 'bin'
        binary.mkdir()
        curl = binary / 'curl'
        curl.write_text('#!' + sys.executable + '\nimport json, os, sys, urllib.parse\n'
                        'with open(os.environ["SOURCE_RECHECK_FIXTURE"]) as source: rows=json.load(source)\n'
                        'print(json.dumps(rows[urllib.parse.urlsplit(sys.argv[-1]).path]))\n')
        curl.chmod(0o755)
        environment = patch.dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                                 GH_TOKEN='fixture-read-only', SOURCE_RECHECK_FIXTURE=str(self.payload))
        environment.start()
        self.addCleanup(environment.stop)

    def execute(self):
        self.payload.write_text(json.dumps(self.responses))
        # These focused fixtures exercise the real existing Bash admission,
        # including jq/body parsing. Plan schema validation has its own suite.
        with patch.object(RECHECK.artifacts, 'check_plan') as validate:
            result = RECHECK.recheck(self.plan, self.plan['framework_sha'])
        validate.assert_called_once_with(self.plan)
        return result

    def test_unchanged_primary_and_companion_admission_passes(self):
        self.assertEqual(self.execute(), 0)

    def test_moved_closed_draft_or_redeclared_source_set_stops_the_stage(self):
        original = copy.deepcopy(self.responses)
        mutations = [
            ('/repos/kuasar-sandbox/orchestrator/pulls/1', lambda row: row.update(state='closed')),
            ('/repos/kuasar-sandbox/orchestrator/pulls/1', lambda row: row.update(body='')),
            ('/repos/kuasar-sandbox/connector/pulls/2', lambda row: row.update(draft=True)),
            ('/repos/kuasar-sandbox/connector/pulls/2', lambda row: row['head'].update(sha='9' * 40)),
            ('/repos/kuasar-sandbox/connector/git/ref/heads/main', lambda row: row['object'].update(sha='8' * 40)),
            ('/repos/kuasar-sandbox/connector/git/ref/pull/2/merge', lambda row: row['object'].update(sha='7' * 40)),
        ]
        for path, mutate in mutations:
            self.responses = copy.deepcopy(original)
            mutate(self.responses[path])
            with self.subTest(path=path):
                self.assertNotEqual(self.execute(), 0)

    def test_release_mode_needs_no_pr_or_control_credentials(self):
        plan = dict(self.plan, mode='exact-assets', candidate_records=[])
        with patch.object(RECHECK.artifacts, 'check_plan'), patch.object(RECHECK.subprocess, 'run') as run, \
             patch.dict(os.environ, GH_TOKEN=''):
            self.assertEqual(RECHECK.recheck(plan, plan['framework_sha']), 0)
        run.assert_not_called()

    def test_invalid_plan_or_framework_is_rejected_before_admission_io(self):
        with patch.object(RECHECK.subprocess, 'run') as run:
            with patch.object(RECHECK.artifacts, 'check_plan', side_effect=ValueError('invalid plan')):
                with self.assertRaisesRegex(ValueError, 'invalid plan'):
                    RECHECK.recheck(self.plan, self.plan['framework_sha'])
            with patch.object(RECHECK.artifacts, 'check_plan'):
                with self.assertRaisesRegex(ValueError, 'framework differs'):
                    RECHECK.recheck(self.plan, '2' * 40)
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
