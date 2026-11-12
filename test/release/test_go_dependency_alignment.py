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
            'base': {'ref': 'main', 'sha': BASE, 'repo': {'full_name': 'kuasar-sandbox/sandboxer'}}, 'head': {'sha': HEAD, 'ref': 'automation/align-go/main',
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

    def test_actual_v0113_v0114_and_daily_six_edge_selections(self):
        # Captured from go.mod at the component tags of each named aggregate.
        pseudo = 'v0.1.14-preview.20261107.1.0.20261107022758-3b3bff611621'
        sand = 'github.com/kuasar-sandbox/sandboxer'
        erofs = 'github.com/kuasar-sandbox/go-erofs v0.0.0-20261014160540-827da6f90114 // indirect'
        release_args = [('release-v0.1.13', 'v0.1.14', 'v0.1.13', 'runtime-v0.1.13', 'v0.1.10', 'v0.1.6', pseudo),
                        ('release-v0.1.14', 'v0.1.15', 'v0.1.14', 'runtime-v0.1.14', 'v0.1.12', 'v0.1.8', 'v0.1.15'),
                        ('release-v0.2.0-preview.20261112', 'v0.1.15', 'v0.2.0-preview.20261112',
                         'runtime-v0.2.0-preview.20261112', 'v0.1.12', 'v0.1.8', 'v0.1.15')]
        for release, sandboxer, orchestrator, runtime, accel_req, conn_req, sand_req in release_args:
            with self.subTest(release=release):
                selected = dict(accelerator='v0.1.12', connector='v0.1.8', sandboxer=sandboxer,
                                orchestrator=orchestrator, runtime=runtime, vmlinux='vmlinux-v0.1.6')
                expected = alignment.policy.expected_versions(selected)
                go_deps = {'sandboxer': {ACC: accel_req, CON: conn_req},
                           'orchestrator': {ACC: accel_req, CON: conn_req, sand: sand_req},
                           'runtime': {ACC: accel_req}}
                differences = {}
                for unit, requires in go_deps.items():
                    lines = [f'  {name} {version}' for name, version in requires.items()]
                    if unit in ('runtime', 'orchestrator'):
                        lines.append('  ' + erofs)
                    text = 'module example.test/' + unit + '\nrequire (\n' + '\n'.join(lines) + '\n)\n'
                    changes = alignment.policy.declaration_changes(text, expected)
                    for path, pair in changes.items():
                        differences[unit, path] = pair
                wanted = ({(unit, name): (before, expected[name]) for unit, requirements in go_deps.items()
                           for name, before in requirements.items()} if release == 'release-v0.1.13' else {})
                self.assertEqual(differences, wanted)
                self.assertEqual(len(differences), 6 if release == 'release-v0.1.13' else 0)

    def test_no_writer_grant_does_not_block_clean_selection_or_publish_drift(self):
        plans = {'connector': plan('connector', CORE, HEAD), 'sandboxer': plan('sandboxer', CORE, BASE)}
        contents = f'module example.test/consumer\nrequire {CON} {CORE}\n'
        checksum = '\n'.join(sorted(sums(CON, CORE))) + '\n'
        def read(c, repo, sha, filename, **kwargs):
            if filename == 'go.sum': return checksum
            return contents if repo.endswith('/sandboxer') else 'module connector\n'
        with (patch.dict(os.environ, GO_ALIGNMENT_TOKEN='', GO_ALIGNMENT_BOT=''),
              patch.object(alignment, 'read_file', side_effect=read),
              patch.object(alignment, 'writer', side_effect=AssertionError('unexpected mutation'))):
            alignment.check_plans(coordinator, plans, date='20261112')
            with self.assertRaisesRegex(coordinator.Deferred, 'Pull requests: Read and write'):
                alignment.ensure_pr(coordinator, plans['sandboxer'], BASE, {'go.mod': 'new'},
                                    PRE, {}, Mock())

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

    def test_open_pr_for_other_base_is_not_changed_by_writing_its_head(self):
        other = pr()
        other['base']['ref'] = 'release/v0.1.x'
        with (patch.dict(os.environ, GO_ALIGNMENT_TOKEN='test', GO_ALIGNMENT_BOT='app[bot]'),
              patch.object(coordinator, 'paginated', return_value=[other]) as query,
              patch.object(coordinator, 'branch_sha') as get_branch,
              patch.object(alignment, 'writer') as write,
              self.assertRaisesRegex(coordinator.Deferred, 'another base')):
            alignment.ensure_pr(coordinator, plan('sandboxer', OLD, BASE), BASE, {}, PRE, {}, Mock())
        self.assertNotIn('base=', query.call_args.args[0])
        get_branch.assert_not_called()
        write.assert_not_called()

    def test_new_pr_preserves_executable_file_modes(self):
        def write(path, method, payload):
            if path.endswith('/git/trees'):
                self.assertEqual(payload['tree'], [dict(path='go.mod', mode='100755', type='blob', content='new')])
                return {'sha': 'new-tree'}
            if path.endswith('/git/commits'):
                self.assertEqual(payload['parents'], [BASE])
                return {'sha': HEAD}
            if path.endswith('/git/refs'):
                self.assertEqual(payload, {'ref': 'refs/heads/automation/align-go/main', 'sha': HEAD})
                return {}
            if path.endswith('/pulls'):
                self.assertEqual(payload['base'], 'main')
                return {'html_url': 'pr/4'}
            self.fail(path)
        with (patch.dict(os.environ, GO_ALIGNMENT_TOKEN='test', GO_ALIGNMENT_BOT='app[bot]'),
              patch.object(coordinator, 'paginated', return_value=[]),
              patch.object(coordinator, 'branch_sha', side_effect=lambda repo, name: BASE if name=='main' else None),
              patch.object(coordinator, 'api', return_value={'tree': {'sha': 'base-tree'}}),
              patch.object(alignment, 'source_files', return_value={'go.mod': ('100755', 'old')}),
              patch.object(alignment, 'writer', side_effect=write) as writer,
              self.assertRaisesRegex(coordinator.Pending, 'opened pr/4')):
            alignment.ensure_pr(coordinator, plan('sandboxer', OLD, BASE), BASE, {'go.mod':'new'}, PRE, {}, Mock())
        self.assertEqual(writer.call_count, 4)

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
        check = {'name': 'ci / finalize', 'head_sha': HEAD,
                 'id': 7, 'status': 'completed', 'conclusion': 'success', 'html_url': 'check/7'}
        def pages(path, key=None):
            if key == 'workflow_runs':
                return [{'id': 8, 'event': 'pull_request_target', 'head_sha': HEAD, 'head_repository': p['head']['repo'],
                         'pull_requests': [p], 'status': 'completed', 'conclusion': 'success', 'html_url': 'run/8'}]
            return [check] if key else [p]
        def branch(repo, name):
            if repo == coordinator.PLATFORM_REPOSITORY: return TRUSTED
            return BASE if name == 'main' else HEAD
        newblob = hashlib.sha1(b'blob 3\0new').hexdigest()
        def files(c, repo, sha):
            return {'go.mod': ('100755', newblob if sha == HEAD else 'previous')}
        def write(path, method, payload):
            if path.endswith('/merge'): return {'merged': True}
            self.fail('unchanged PR must have no other writes: ' + path)
        confirm = Mock()
        with (patch.dict(os.environ, GO_ALIGNMENT_TOKEN='test', GO_ALIGNMENT_BOT='app[bot]'),
              patch.object(coordinator, 'api', side_effect=api), patch.object(coordinator, 'branch_sha', side_effect=branch),
              patch.object(coordinator, 'paginated', side_effect=pages),
              patch.object(alignment, 'source_files', side_effect=files),
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
