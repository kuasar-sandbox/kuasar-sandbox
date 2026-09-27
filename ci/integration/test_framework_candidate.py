"""Exercise exact merge-ref admission and final revalidation with API snapshots."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import framework_candidate as candidate


class FrameworkCandidateTests(unittest.TestCase):
    def setUp(self):
        self.base, self.head, self.merge = ('1' * 40, '2' * 40, '3' * 40)
        self.repo = candidate.PLATFORM
        self.ref = f'refs/heads/ci/framework/pr-188/{self.merge}'
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
        return candidate.resolve(changes.get('repository', self.repo), changes.get('ref', self.ref),
                                 changes.get('sha', self.merge), self.data.__getitem__)

    def test_exact_candidate_reads_all_live_refs(self):
        reads = []
        def fetch(path):
            reads.append(path)
            return self.data[path]
        record = candidate.resolve(self.repo, self.ref, self.merge, fetch)
        self.assertEqual(set(reads), set(self.data))
        self.assertEqual(record, {'repository': self.repo, 'pull_request_number': '188',
                                 'candidate_sha': self.merge, 'base_sha': self.base,
                                 'base_ref': 'main', 'head_sha': self.head})

    def test_unrelated_or_malformed_push_refs_are_rejected(self):
        for changes in [{'repository': 'outside/fork'}, {'ref': 'refs/heads/main'},
                        {'ref': self.ref + '/extra'}, {'ref': self.ref.replace('pr-188', 'pr-0188')},
                        {'ref': self.ref.replace(self.merge, self.merge[:7])}, {'sha': self.merge[:7]},
                        {'ref': self.ref.replace(self.merge, self.head)}]:
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

    def test_regenerated_merge_uses_a_new_ref_without_changing_head(self):
        previous = self.merge
        self.merge = '4' * 40
        self.pr['merge_commit_sha'] = self.merge
        self.commit['sha'] = self.merge
        self.data[f'repos/{self.repo}/git/commits/{self.merge}'] = self.commit
        self.data[f'repos/{self.repo}/git/ref/pull/188/merge']['object']['sha'] = self.merge
        with self.assertRaises(ValueError):
            self.resolve(sha=previous)
        with self.assertRaises(ValueError):
            self.resolve()
        record = self.resolve(ref=self.ref.replace(previous, self.merge))
        self.assertEqual(record['head_sha'], self.head)
        self.assertEqual(record['candidate_sha'], self.merge)

    def test_entry_outputs_and_final_input_revalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event = {'repository': {'full_name': self.repo, 'visibility': 'public'},
                     'ref': self.ref, 'after': self.merge, 'deleted': False}
            path = root / 'event.json'; path.write_text(json.dumps(event))
            env = {'GITHUB_EVENT_NAME': 'push', 'GITHUB_EVENT_PATH': str(path),
                   'GITHUB_REPOSITORY': self.repo, 'GITHUB_REF': self.ref, 'GITHUB_SHA': self.merge,
                   'GITHUB_OUTPUT': str(root / 'outputs'), 'CANDIDATE_REPOSITORY': self.repo,
                   'CANDIDATE_PR': '188', 'CANDIDATE_SHA': self.merge, 'CANDIDATE_BASE_SHA': self.base,
                   'CANDIDATE_BASE_REF': 'main', 'CANDIDATE_HEAD_SHA': self.head, 'COMPANION_CANDIDATES': '[]'}
            def api(command, **kwargs):
                self.assertEqual(command[:2], ['gh', 'api'])
                return json.dumps(self.data[command[2]])
            with patch.dict(os.environ, env), patch.object(candidate.subprocess, 'check_output', side_effect=api), redirect_stdout(io.StringIO()):
                with patch.object(candidate.sys, 'argv', ['framework_candidate.py']):
                    candidate.main()
                self.assertIn('candidate_sha=' + self.merge, (root / 'outputs').read_text())
                with patch.object(candidate.sys, 'argv', ['framework_candidate.py', '--verify-inputs']):
                    candidate.main()
                    with patch.dict(os.environ, {'CANDIDATE_BASE_SHA': '4' * 40}), self.assertRaises(ValueError):
                        candidate.main()
                    self.pr['draft'] = True
                    with self.assertRaises(ValueError): candidate.main()
                    self.pr['draft'] = False
                for changes in ({'deleted': True}, {'after': self.head}, {'ref': 'refs/heads/main'},
                                {'repository': {'full_name': self.repo, 'visibility': 'private'}}):
                    path.write_text(json.dumps(event | changes))
                    with patch.object(candidate.sys, 'argv', ['framework_candidate.py']), self.assertRaises(ValueError):
                        candidate.main()


if __name__ == '__main__':
    unittest.main()
