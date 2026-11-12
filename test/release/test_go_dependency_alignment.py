#!/usr/bin/env python3
"""Release admission, idempotent alignment and protected merge boundaries."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'release'))
import go_dependency_alignment as alignment
import preview_coordinator as coordinator


ACC = 'github.com/kuasar-sandbox/accelerator'
CON = 'github.com/kuasar-sandbox/connector'
CORE, OLD = 'v0.2.0', 'v0.1.1'
PRE = CORE + '-preview.20261112.2'
BASE, HEAD, TRUSTED = 'b' * 40, 'a' * 40, 'c' * 40
CHECKSUM = 'h1:' + 'A' * 43 + '='


def sums(module, version):
    return {f'{module} {version} {CHECKSUM}', f'{module} {version}/go.mod {CHECKSUM}'}


def plan(unit, selected, sha, *, action='reuse'):
    repository = coordinator.UNIT_BY_NAME[unit]
    return coordinator.Plan(repository, selected, 'main', sha, selected, selected, action, sha)


def pr(bot='app[bot]'):
    return {'number': 4, 'state': 'open', 'html_url': 'pr/4', 'user': {'login': bot},
            'base': {'ref': 'main', 'sha': BASE}, 'head': {'sha': HEAD, 'ref': 'automation/align-go/main',
            'repo': {'full_name': 'kuasar-sandbox/sandboxer'}}, 'mergeable_state': 'clean', 'mergeable': True}


class AlignmentTests(unittest.TestCase):
    def test_new_stable_and_aggregate_check_actual_declarations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for unit in ('sandboxer', 'connector'):
                (root / unit).mkdir()
                (root / unit / 'go.mod').write_text('module github.com/kuasar-sandbox/' + unit + '\n')
            (root / 'sandboxer' / 'go.mod').write_text('module example.test/consumer\nrequire ' + CON + ' ' + OLD + ' // indirect\n')
            with self.assertRaisesRegex(ValueError, 'sandboxer.*connector.*v0.1.1 -> v0.2.0'):
                alignment.selected_files(root, {'sandboxer': PRE, 'connector': CORE})
            alignment.selected_files(root, {'sandboxer': PRE, 'connector': OLD})

    def test_one_daily_pr_aligns_both_edges_before_freeze(self):
        plans = {'accelerator': plan('accelerator', PRE, '1' * 40, action='publish'),
                 'connector': plan('connector', CORE, '2' * 40),
                 'sandboxer': plan('sandboxer', OLD, BASE)}
        old = f'module sandboxer\ngo 1.22\nrequire (\n {ACC} {OLD}\n {CON} {OLD} // indirect\n)\n'
        def contents(c, repo, sha, filename, **kwargs):
            if filename == 'go.sum':
                return ''
            return old if repo.endswith('/sandboxer') else 'module ' + repo + '\n'
        state = Mock()
        state.status.return_value.partial = False
        before = Mock()
        with (patch.object(alignment, 'read_file', side_effect=contents),
              patch.object(alignment, 'module_checksums', side_effect=sums) as download,
              patch.object(alignment, 'confirm_inputs'), patch.object(coordinator, 'PLATFORM_SHA', BASE),
              patch.object(coordinator, 'branch_sha', return_value=BASE),
              patch.object(coordinator, 'RepositoryState', return_value=state),
              patch.object(alignment, 'ensure_pr', side_effect=coordinator.Pending('waiting')) as submit,
              self.assertRaisesRegex(coordinator.Pending, 'waiting')):
            alignment.check_plans(coordinator, plans, {'sandboxer': CORE},
                                  date='20261112.2', before_alignment=before)
        self.assertEqual(1, submit.call_count)
        _, consumer, base, changed, version, dependencies, _ = submit.call_args.args
        self.assertEqual((consumer.unit.name, base, version), ('sandboxer', BASE, PRE))
        self.assertEqual(alignment.policy.managed_requires(changed['go.mod']), {ACC: CORE, CON: CORE})
        self.assertEqual(set(changed['go.sum'].splitlines()), sums(CON, CORE))
        self.assertTrue(all(call.args == (CON, CORE) for call in download.call_args_list))
        self.assertEqual(dependencies, {'accelerator': {'tag': PRE, 'sha': '1' * 40},
                                       'connector': {'tag': CORE, 'sha': '2' * 40}})
        before.assert_called_once()

    def test_missing_checksum_never_replaces_conflicting_published_content(self):
        text = f'{CON} {CORE} h1:bad\n'
        with patch.object(alignment, 'module_checksums', side_effect=sums), self.assertRaisesRegex(ValueError, 'conflicts'):
            alignment.update_sums(text, {CON: CORE})
        value = '\n'.join(sorted(sums(CON, CORE))) + '\n'
        with patch.object(alignment, 'module_checksums', side_effect=AssertionError('already recorded')):
            self.assertEqual(value, alignment.update_sums(value, {CON: CORE}))

    def test_immutable_source_drift_is_detected(self):
        with (patch.object(coordinator, 'tag_sha', return_value=HEAD),
              self.assertRaisesRegex(coordinator.Deferred, 'tag moved')):
            alignment.confirm_inputs(coordinator, {'sandboxer': plan('sandboxer', OLD, BASE)})
        with (patch.object(coordinator, 'branch_sha', return_value=HEAD),
              self.assertRaisesRegex(coordinator.Pending, 'changed during selection')):
            alignment.confirm_inputs(coordinator, {'sandboxer': plan('sandboxer', PRE, BASE, action='publish')})

    def test_exact_proof_reuses_only_the_current_workflow_and_candidate(self):
        p = pr()
        dependencies = {'connector': {'tag': CORE, 'sha': 'd' * 40}}
        v = plan('sandboxer', OLD, BASE)
        digest = hashlib.sha256(json.dumps(['sandboxer', 4, HEAD, BASE, PRE, dependencies],
                                           sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        run = {'id': 9, 'head_sha': TRUSTED, 'head_branch': 'main', 'head_repository': {'full_name': coordinator.PLATFORM_REPOSITORY},
               'display_title': 'Go selection ' + digest, 'status': 'completed', 'conclusion': 'success', 'html_url': 'run/9'}
        with (patch.object(coordinator, 'branch_sha', return_value=TRUSTED),
              patch.object(coordinator, 'paginated', return_value=[dict(run, head_sha=HEAD)]) as runs,
              patch.object(coordinator, 'gh') as gh):
            with self.assertRaisesRegex(coordinator.Pending, 'dispatched'):
                alignment.exact_combination(coordinator, v, p, PRE, dependencies)
            self.assertIn('binding=' + digest, gh.call_args.args)
            runs.return_value = [dict(run, status='in_progress')]
            with self.assertRaisesRegex(coordinator.Pending, 'waiting for run/9'):
                alignment.exact_combination(coordinator, v, p, PRE, dependencies)
            runs.return_value = [run]
            self.assertEqual(TRUSTED, alignment.exact_combination(coordinator, v, p, PRE, dependencies))
            self.assertEqual(1, gh.call_count)
            runs.return_value = [dict(run, conclusion='failure')]
            with self.assertRaisesRegex(coordinator.Deferred, 'failed'):
                alignment.exact_combination(coordinator, v, p, PRE, dependencies)

    def test_foreign_alignment_branch_or_pr_is_preserved(self):
        with (patch.dict(os.environ, GO_ALIGNMENT_TOKEN='test', GO_ALIGNMENT_BOT='app[bot]'),
              patch.object(coordinator, 'paginated', return_value=[pr('someone')]),
              patch.object(alignment, 'writer') as write,
              self.assertRaisesRegex(coordinator.Deferred, 'another contributor')):
            alignment.ensure_pr(coordinator, plan('sandboxer', OLD, BASE), BASE, {}, PRE, {}, Mock())
        write.assert_not_called()

    def test_exact_native_checks_and_reviews_precede_protected_squash(self):
        p = pr()
        owncommit = {'author': p['user'], 'commit': {'message': alignment.TITLE, 'tree': {'sha': 'tree'}},
                     'parents': [{'sha': BASE}]}
        def api(path):
            if '/branches/' in path: return {'protected': True}
            if '/git/commits/' in path: return {'tree': {'sha': 'base-tree'}}
            if '/commits/' in path: return owncommit
            if '/pulls/' in path: return p
            self.fail(path)
        check = {'name': 'ci / finalize', 'head_sha': HEAD, 'app': {'slug': 'github-actions'},
                 'id': 7, 'status': 'completed', 'conclusion': 'success', 'html_url': 'check/7'}
        def pages(path, key=None):
            return [check] if key else [p]
        def branch(repo, name):
            if repo == coordinator.PLATFORM_REPOSITORY: return TRUSTED
            return BASE if name == 'main' else HEAD
        def write(path, method, payload):
            if path.endswith('/git/trees'): return {'sha': 'tree'}
            if path.endswith('/merge'): return {'merged': True}
            self.fail(path)
        confirm = Mock()
        with (patch.dict(os.environ, GO_ALIGNMENT_TOKEN='test', GO_ALIGNMENT_BOT='app[bot]'),
              patch.object(coordinator, 'api', side_effect=api), patch.object(coordinator, 'branch_sha', side_effect=branch),
              patch.object(coordinator, 'paginated', side_effect=pages),
              patch.object(alignment, 'exact_combination', return_value=TRUSTED),
              patch.object(alignment, 'reviews_resolved', return_value=False) as reviews,
              patch.object(alignment, 'writer', side_effect=write) as writer):
            v = plan('sandboxer', OLD, BASE)
            with self.assertRaisesRegex(coordinator.Pending, 'reviews'):
                alignment.ensure_pr(coordinator, v, BASE, {'go.mod': 'new'}, PRE, {}, confirm)
            self.assertTrue(all(not a.args[0].endswith('/merge') for a in writer.call_args_list))
            reviews.return_value = True
            with self.assertRaisesRegex(coordinator.Pending, 'rescan actual component'):
                alignment.ensure_pr(coordinator, v, BASE, {'go.mod': 'new'}, PRE, {}, confirm)
            self.assertEqual(writer.call_args.args[1:], ('PUT', {'sha': HEAD, 'merge_method': 'squash'}))
            confirm.assert_called_once()

if __name__ == '__main__':
    unittest.main()
