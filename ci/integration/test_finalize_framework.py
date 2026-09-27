"""Verify complete CI results and current PR identity before automatic publication."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import finalize_framework as finalizer


class FrameworkResultTests(unittest.TestCase):
    def setUp(self):
        self.base, self.head, self.merge = ('1' * 40, '2' * 40, '3' * 40)
        self.repo = finalizer.PLATFORM
        self.ref = 'refs/pull/188/merge'
        self.pr = {'number': 188, 'state': 'open', 'draft': False, 'body': '',
                   'head': {'sha': self.head, 'repo': {'full_name': self.repo}},
                   'base': {'sha': self.base, 'ref': 'main', 'repo': {'full_name': self.repo}},
                   'merge_commit_sha': self.merge}
        self.commit = {'sha': self.merge, 'parents': [{'sha': self.base}, {'sha': self.head}]}
        self.data = {f'repos/{self.repo}/pulls/188': self.pr,
                     f'repos/{self.repo}/git/commits/{self.merge}': self.commit}
        for name, sha in [('pull/188/head', self.head), ('pull/188/merge', self.merge), ('heads/main', self.base)]:
            self.data[f'repos/{self.repo}/git/ref/{name}'] = {'ref': 'refs/' + name, 'object': {'sha': sha}}

    def resolve(self, **changes):
        return finalizer.resolve(changes.get('repository', self.repo), changes.get('ref', self.ref),
                                 changes.get('sha', self.merge), self.data.__getitem__)

    def test_exact_candidate_reads_all_live_refs(self):
        reads = []
        def fetch(path):
            reads.append(path)
            return self.data[path]
        record = finalizer.resolve(self.repo, self.ref, self.merge, fetch)
        self.assertEqual(set(reads), set(self.data))
        self.assertEqual(record, {'repository': self.repo, 'pull_request_number': '188',
                                 'candidate_sha': self.merge, 'base_sha': self.base,
                                 'base_ref': 'main', 'head_sha': self.head})

    def test_unrelated_or_malformed_refs_are_rejected(self):
        for changes in [{'repository': 'outside/fork'}, {'ref': 'refs/heads/main'},
                        {'ref': self.ref + '/extra'}, {'ref': self.ref.replace('/188/', '/0188/')},
                        {'sha': self.merge[:7]}, {'ref': 'refs/heads/ci/framework/pr-188/' + self.merge}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.resolve(**changes)

    def test_ready_same_repository_pr_and_current_merge_are_required(self):
        original = deepcopy(self.pr)
        for fault in ('draft', 'closed', 'fork', 'base-repository', 'number', 'merge', 'head', 'base', 'base-ref', 'companion'):
            self.pr.clear(); self.pr.update(deepcopy(original))
            if fault == 'draft': self.pr['draft'] = True
            elif fault == 'closed': self.pr['state'] = 'closed'
            elif fault == 'fork': self.pr['head']['repo']['full_name'] = 'outside/fork'
            elif fault == 'base-repository': self.pr['base']['repo']['full_name'] = 'outside/fork'
            elif fault == 'number': self.pr['number'] = 187
            elif fault == 'merge': self.pr['merge_commit_sha'] = '4' * 40
            elif fault == 'head': self.pr['head']['sha'] = '4' * 40
            elif fault == 'base': self.pr['base']['sha'] = '4' * 40
            elif fault == 'base-ref': self.pr['base']['ref'] = 'feature'
            else: self.pr['body'] = '<!--\n kuasar-ci-companions\nkuasar-sandbox/connector#1\n-->'
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                self.resolve()

    def test_ordered_parents_and_live_ref_changes_fail_closed(self):
        original = deepcopy(self.data)
        for fault in ('swapped-parents', 'extra-parent', 'commit', 'pull/188/head', 'pull/188/merge', 'heads/main'):
            self.data = deepcopy(original)
            commit = self.data[f'repos/{self.repo}/git/commits/{self.merge}']
            if fault == 'swapped-parents': commit['parents'].reverse()
            elif fault == 'extra-parent': commit['parents'].append({'sha': '4' * 40})
            elif fault == 'commit': commit['sha'] = self.head
            else: self.data[f'repos/{self.repo}/git/ref/{fault}']['object']['sha'] = '4' * 40
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                self.resolve()

    def test_regenerated_merge_rejects_the_previously_tested_sha(self):
        previous = self.merge
        self.merge = '4' * 40
        self.pr['merge_commit_sha'] = self.merge
        self.commit['sha'] = self.merge
        self.data[f'repos/{self.repo}/git/commits/{self.merge}'] = self.commit
        self.data[f'repos/{self.repo}/git/ref/pull/188/merge']['object']['sha'] = self.merge
        with self.assertRaises(ValueError):
            self.resolve(sha=previous)
        self.assertEqual(self.resolve()['head_sha'], self.head)

    def api(self, command, **kwargs):
        self.assertEqual(command[:2], ['gh', 'api'])
        return json.dumps(self.data[command[2]])

    def environment(self, directory):
        path = Path(directory) / 'event.json'
        path.write_text(json.dumps({'repository': {'full_name': self.repo, 'visibility': 'public'},
                                    'pull_request': self.pr}))
        return {'GITHUB_EVENT_NAME': 'pull_request', 'GITHUB_EVENT_PATH': str(path),
                'GITHUB_REPOSITORY': self.repo, 'GITHUB_REF': self.ref, 'GITHUB_SHA': self.merge,
                'GITHUB_RUN_ID': '123', 'INTEGRATION_RESULT': 'success'}

    def test_publication_uses_the_complete_pipeline_result_and_exact_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory)
            for conclusion in ('success', 'failure', 'cancelled', 'skipped'):
                with self.subTest(conclusion=conclusion), patch.dict(os.environ, env | {'INTEGRATION_RESULT': conclusion}), \
                     patch.object(finalizer.sys, 'argv', ['finalize_framework.py']), \
                     patch.object(finalizer.subprocess, 'check_output', side_effect=self.api), \
                     patch.object(finalizer.subprocess, 'run') as publish, redirect_stdout(io.StringIO()):
                    self.assertEqual(finalizer.main(), 0 if conclusion == 'success' else 1)
                    args, kwargs = publish.call_args
                    self.assertEqual(args[0], ['gh', 'api', '--method', 'POST',
                                             f'repos/{self.repo}/statuses/{self.merge}', '--input', '-'])
                    self.assertTrue(kwargs['check'])
                    status = json.loads(kwargs['input'])
                    self.assertEqual(status['context'], 'ci / finalize')
                    self.assertEqual(status['state'], 'success' if conclusion == 'success' else 'failure')
                    self.assertEqual(status['target_url'], f'https://github.com/{self.repo}/actions/runs/123')

    def test_invalid_result_event_or_drift_cannot_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self.environment(directory)
            event_path = Path(env['GITHUB_EVENT_PATH'])
            original = event_path.read_text()
            faults = ({'INTEGRATION_RESULT': 'pending'}, {'GITHUB_EVENT_NAME': 'push'},
                      {'GITHUB_EVENT_NAME': 'pull_request_target'}, {'GITHUB_RUN_ID': '0'},
                      {'GITHUB_SHA': self.head}, {'GITHUB_REPOSITORY': 'outside/fork'})
            for overrides in faults:
                with self.subTest(overrides=overrides), patch.dict(os.environ, env | overrides), \
                     patch.object(finalizer.sys, 'argv', ['finalize_framework.py']), \
                     patch.object(finalizer.subprocess, 'check_output', side_effect=self.api), \
                     patch.object(finalizer.subprocess, 'run') as publish, self.assertRaises(ValueError):
                    finalizer.main()
                publish.assert_not_called()
            for fault in ('private', 'head', 'base', 'fork', 'draft'):
                event = json.loads(original)
                if fault == 'private': event['repository']['visibility'] = 'private'
                elif fault == 'fork': event['pull_request']['head']['repo']['full_name'] = 'outside/fork'
                elif fault == 'draft': event['pull_request']['draft'] = True
                else: event['pull_request'][fault]['sha'] = '4' * 40
                event_path.write_text(json.dumps(event))
                with self.subTest(fault=fault), patch.dict(os.environ, env), \
                     patch.object(finalizer.sys, 'argv', ['finalize_framework.py']), \
                     patch.object(finalizer.subprocess, 'check_output', side_effect=self.api), \
                     patch.object(finalizer.subprocess, 'run') as publish, self.assertRaises(ValueError):
                    finalizer.main()
                publish.assert_not_called()

    def test_publication_api_failure_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, self.environment(directory)), \
             patch.object(finalizer.sys, 'argv', ['finalize_framework.py']), \
             patch.object(finalizer.subprocess, 'check_output', side_effect=self.api), \
             patch.object(finalizer.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['gh'])), \
             self.assertRaises(subprocess.CalledProcessError):
            finalizer.main()


if __name__ == '__main__':
    unittest.main()
