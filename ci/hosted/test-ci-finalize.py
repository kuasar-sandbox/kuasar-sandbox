#!/usr/bin/env python3
"""Exercise production finalizer shell with an offline GitHub API fixture."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
REPO = 'kuasar-sandbox/kuasar-sandbox'
BASE, HEAD, MERGE = '1' * 40, '2' * 40, '3' * 40


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        trusted = self.root / 'trusted/platform/ci/integration'
        trusted.mkdir(parents=True)
        (trusted / 'validate-source-set.sh').write_text((ROOT / 'ci/integration/validate-source-set.sh').read_text())
        workflow = (ROOT / '.github/workflows/ci-entry.yml').read_text()
        if os.environ.get('CI_FINALIZE_BASELINE') == '1':
            workflow = subprocess.check_output(
                ['git', 'show', 'HEAD:.github/workflows/ci-entry.yml'], cwd=ROOT, text=True)
        steps = yaml.safe_load(workflow)['jobs']['finalize']['steps']
        self.script = next(step['run'] for step in steps
                           if step.get('name') == 'Finalize the exact integration status')
        self.pr = {'number': 7, 'state': 'open', 'draft': False,
                   'base': {'ref': 'main', 'sha': BASE, 'repo': {'full_name': REPO}},
                   'head': {'sha': HEAD, 'repo': {'full_name': REPO}},
                   'user': {'login': 'contributor'}, 'body': '', 'merge_commit_sha': MERGE}
        self.commit = {'sha': MERGE, 'parents': [{'sha': BASE}, {'sha': HEAD}]}
        self.responses = {
            f'/repos/{REPO}/pulls/7': self.pr,
            f'/repos/{REPO}/git/commits/{MERGE}': self.commit,
            f'/repos/{REPO}/git/ref/heads/main': {'object': {'sha': BASE}},
        }
        self.env = dict(os.environ, GITHUB_REPOSITORY=REPO,
                        GITHUB_API_URL='https://api.github.com',
                        GITHUB_SERVER_URL='https://github.com', GITHUB_RUN_ID='9',
                        GH_TOKEN='offline-fixture', STATUS_CONTEXT='kuasar/ci-exact-head',
                        ADMISSION_RESULT='success', ELIGIBLE='true', E2E_RESULT='success',
                        CANDIDATE_PR='7', CANDIDATE_SHA=MERGE, CANDIDATE_BASE_SHA=BASE,
                        CANDIDATE_BASE_REF='main', CANDIDATE_HEAD_SHA=HEAD,
                        COMPANION_CANDIDATES='[]', COMPANION_TOKEN='',
                        FIXTURE_ROOT=str(self.root))
        # No network operation is possible: the fixture only reads/writes temp files.
        mock = self.root / 'curl'
        mock.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
from urllib.parse import urlsplit
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
path = urlsplit(sys.argv[-1]).path
if '--request' in sys.argv and sys.argv[sys.argv.index('--request') + 1] == 'POST':
    with (root / 'statuses.jsonl').open('a') as out:
        out.write(json.dumps({'path': path, 'payload': json.load(sys.stdin)}) + '\\n')
    print('{}')
else:
    responses = json.loads((root / 'responses.json').read_text())
    if path not in responses:
        print('offline fixture has no response for ' + path, file=sys.stderr)
        sys.exit(22)
    value = responses[path]
    if '--output' in sys.argv:
        output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])
        output.write_text(json.dumps(value.get('_body', value)))
        print(value.get('_http', 200), end='')
    else:
        print(json.dumps(value))
''')
        mock.chmod(0o755)
        self.env['PATH'] = str(self.root) + os.pathsep + self.env['PATH']

    def run_finalizer(self, **overrides):
        (self.root / 'responses.json').write_text(json.dumps(self.responses))
        return subprocess.run(['bash', '-c', self.script], cwd=self.root, env={**self.env, **overrides},
                              text=True, capture_output=True, timeout=10)

    def statuses(self):
        path = self.root / 'statuses.jsonl'
        return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []

    def test_exact_success(self):
        result = self.run_finalizer()
        self.assertEqual(result.returncode, 0, result.stderr)
        records = self.statuses()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['path'], f'/repos/{REPO}/statuses/{MERGE}')
        self.assertEqual(records[0]['payload']['state'], 'success')

    def test_admission_must_succeed(self):
        for state in ('failure', 'cancelled', 'skipped', ''):
            with self.subTest(state=state):
                result = self.run_finalizer(ADMISSION_RESULT=state)
                self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.statuses(), [])

    def test_selected_e2e_must_succeed(self):
        for state in ('failure', 'cancelled', 'skipped', 'neutral', ''):
            with self.subTest(state=state):
                self.assertNotEqual(self.run_finalizer(E2E_RESULT=state).returncode, 0)
        self.assertTrue(all(s['payload']['state'] == 'failure' for s in self.statuses()))

    def test_deferred_candidate_does_not_satisfy_required_check(self):
        self.pr['draft'] = True
        self.assertNotEqual(self.run_finalizer(ELIGIBLE='false').returncode, 0)
        self.assertEqual(self.statuses()[-1]['payload']['state'], 'pending')

    def test_deferred_without_integration_is_not_success(self):
        self.assertNotEqual(self.run_finalizer(ELIGIBLE='false', CANDIDATE_SHA='').returncode, 0)
        self.assertEqual(self.statuses(), [])

    def test_changed_deferred_candidate_is_not_success(self):
        self.pr['head']['sha'] = '4' * 40
        self.assertNotEqual(self.run_finalizer(ELIGIBLE='false').returncode, 0)
        self.assertEqual(self.statuses(), [])

    def test_converted_to_draft_does_not_satisfy_required_check(self):
        self.pr['draft'] = True
        self.assertNotEqual(self.run_finalizer().returncode, 0)
        self.assertEqual(self.statuses()[-1]['payload']['state'], 'pending')

    def test_changed_head_rejected(self):
        self.pr['head']['sha'] = '4' * 40
        self.assertNotEqual(self.run_finalizer().returncode, 0)

    def test_changed_base_rejected(self):
        self.pr['base']['sha'] = '4' * 40
        self.assertNotEqual(self.run_finalizer().returncode, 0)

    def test_live_base_branch_change_rejected_even_with_cached_pr(self):
        self.responses[f'/repos/{REPO}/git/ref/heads/main']['object']['sha'] = '4' * 40
        self.assertNotEqual(self.run_finalizer().returncode, 0)

    def test_primary_source_validator_binds_live_branch(self):
        script = (ROOT / 'ci/integration/validate-source-set.sh').read_text()
        env = dict(self.env, CALLER_TOKEN='offline-fixture', CANDIDATE_REPOSITORY=REPO)
        (self.root / 'responses.json').write_text(json.dumps(self.responses))
        self.assertEqual(subprocess.run(['bash', '-c', script], env=env,
                         capture_output=True, timeout=10).returncode, 0)
        self.responses[f'/repos/{REPO}/git/ref/heads/main']['object']['sha'] = '4' * 40
        (self.root / 'responses.json').write_text(json.dumps(self.responses))
        self.assertNotEqual(subprocess.run(['bash', '-c', script], env=env,
                            capture_output=True, timeout=10).returncode, 0)

    def test_changed_integration_rejected(self):
        self.pr['merge_commit_sha'] = '4' * 40
        self.assertNotEqual(self.run_finalizer().returncode, 0)

    def test_parent_order_is_binding(self):
        self.commit['parents'].reverse()
        self.assertNotEqual(self.run_finalizer().returncode, 0)

    def test_closed_candidate_rejected(self):
        self.pr['state'] = 'closed'
        self.assertNotEqual(self.run_finalizer().returncode, 0)

    def test_unavailable_companion_validation_rejected(self):
        self.assertNotEqual(self.run_finalizer(COMPANION_CANDIDATES='[{}]').returncode, 0)

    def test_new_companion_declaration_rejected(self):
        self.pr['body'] = '<!-- kuasar-ci-companions\nkuasar-sandbox/connector#9\n-->'
        self.assertNotEqual(self.run_finalizer().returncode, 0)
        self.assertEqual(self.statuses()[-1]['payload']['state'], 'failure')

    def test_unrelated_body_edit_does_not_invalidate_candidate(self):
        self.pr['body'] = 'Updated explanation; no companion change.'
        self.assertEqual(self.run_finalizer().returncode, 0)

    def test_malformed_companion_declaration_rejected(self):
        self.pr['body'] = '<!-- kuasar-ci-companions broken -->'
        self.assertNotEqual(self.run_finalizer().returncode, 0)

    def test_api_failure_rejected(self):
        self.responses.clear()
        self.assertNotEqual(self.run_finalizer().returncode, 0)

    def run_admission(self, *, fork=False, membership=None, actor='maintainer'):
        self.pr['head']['repo']['full_name'] = 'outside/project' if fork else REPO
        if membership is not None:
            self.responses['/orgs/kuasar-sandbox/memberships/contributor'] = membership
        event = self.root / 'event.json'
        event.write_text(json.dumps({'action': 'opened', 'pull_request': self.pr}))
        (self.root / 'responses.json').write_text(json.dumps(self.responses))
        steps = yaml.safe_load((ROOT / '.github/workflows/ci-entry.yml').read_text())['jobs']['admission']['steps']
        script = next(step['run'] for step in steps if step.get('id') == 'admission')
        env = dict(self.env, GITHUB_EVENT_NAME='pull_request_target', GITHUB_EVENT_PATH=str(event),
                   GITHUB_OUTPUT=str(self.root / 'outputs'), RUNNER_TEMP=str(self.root),
                   GITHUB_ACTOR=actor, MEMBERSHIP_TOKEN='offline-fixture' if fork else '')
        return subprocess.run(['bash', '-c', script], env=env,
                              capture_output=True, text=True, timeout=10)

    def test_same_repository_admission(self):
        result = self.run_admission()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('eligible=true', (self.root / 'outputs').read_text())

    def test_active_member_fork_admission(self):
        result = self.run_admission(fork=True, membership={'_http': 200,
                                    '_body': {'state': 'active', 'role': 'member'}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('eligible=true', (self.root / 'outputs').read_text())

    def test_non_member_fork_cannot_be_admitted_by_maintainer_rerun(self):
        result = self.run_admission(fork=True, actor='maintainer', membership={'_http': 404, '_body': {}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fork author is not an active organization member', result.stderr)
        self.assertIn('eligible=false', (self.root / 'outputs').read_text())
        self.assertEqual(self.statuses()[-1]['payload']['state'], 'failure')

    def test_membership_lookup_error_is_rejected(self):
        result = self.run_admission(fork=True, membership={'_http': 500, '_body': {}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('organization membership verification failed', result.stderr)

    def test_pending_membership_is_rejected(self):
        result = self.run_admission(fork=True, membership={'_http': 200,
                                    '_body': {'state': 'pending', 'role': 'member'}})
        self.assertNotEqual(result.returncode, 0)

    def test_admission_rejects_live_base_change(self):
        self.responses[f'/repos/{REPO}/git/ref/heads/main']['object']['sha'] = '4' * 40
        self.assertNotEqual(self.run_admission().returncode, 0)

    def test_selected_architecture_and_source_stages_must_succeed(self):
        data = yaml.safe_load((ROOT / '.github/workflows/integration-tests.yml').read_text())
        stage = next(step['run'] for step in data['jobs']['results']['steps']
                     if step.get('name') == 'Require every selected stage')
        good = {'RESOLVE_RESULT': 'success', 'X86_RESULT': 'success',
                'ARM_RESULT': 'success', 'SOURCE_RESULT': 'success'}
        self.assertEqual(subprocess.run(['bash', '-c', stage], env={**self.env, **good}).returncode, 0)
        for name in good:
            for value in ('failure', 'cancelled', 'skipped', 'neutral', ''):
                with self.subTest(stage=name, result=value):
                    env = {**self.env, **good, name: value}
                    self.assertNotEqual(subprocess.run(['bash', '-c', stage], env=env).returncode, 0)


if __name__ == '__main__':
    unittest.main()
