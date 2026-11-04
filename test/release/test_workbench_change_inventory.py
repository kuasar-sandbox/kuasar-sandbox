"""Offline, fail-closed component history and Markdown inventory coverage."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'release'))
import change_inventory as inventory


OLD = 'a' * 40
NEW = 'b' * 40


def commit(number, message=None):
    return {'sha': f'{number:040x}', 'commit': {'message': message or f'Change {number}'}}


def comparison_pages(commits, status='ahead', page_size=100):
    chunks = [commits[i:i + page_size] for i in range(0, len(commits), page_size)] or [[]]
    return [{'status': status, 'total_commits': len(commits), 'base_commit': {'sha': OLD},
             'commits': chunk} for chunk in chunks]


class ChangeInventoryTests(unittest.TestCase):
    def api(self, responses):
        """Mock the process boundary, including pagination and endpoint selection."""
        def run(args, **kwargs):
            self.assertEqual(args[:4], ['gh', 'api', '--paginate', '--slurp'])
            self.assertEqual(kwargs, dict(text=True, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, check=False))
            endpoint = args[4]
            self.assertIn(endpoint, responses, f'Unexpected API request: {endpoint}')
            return subprocess.CompletedProcess(args, 0, json.dumps(responses[endpoint]), '')
        return patch.object(inventory.subprocess, 'run', side_effect=run)

    def responses(self, repo, commits, status='ahead'):
        return {f'repos/{repo}/git/ref/tags/old': [{'object': {'type': 'commit', 'sha': OLD}}],
                f'repos/{repo}/git/ref/tags/new': [{'object': {'type': 'commit', 'sha': NEW}}],
                f'repos/{repo}/compare/{OLD}...{NEW}?per_page=100': comparison_pages(commits, status)}

    def test_complete_paginated_comparison_keeps_every_commit_beyond_one_hundred(self):
        repo = 'kuasar-sandbox/accelerator'
        rows = [commit(n) for n in range(1, 252)]
        with self.api(self.responses(repo, rows)) as run:
            text = inventory.Inventory().render('accelerator', 'old', 'new')
        self.assertEqual(len([line for line in text.splitlines() if line.startswith('- ')]), 251)
        for row in rows:
            self.assertIn(f'https://github.com/{repo}/commit/{row["sha"]}', text)
        self.assertEqual(run.call_count, 3)
        self.assertIn(f'https://github.com/{repo}/compare/{OLD}...{NEW}', text)

    def test_incomplete_duplicate_or_inconsistent_comparisons_fail_closed(self):
        base = comparison_pages([commit(1), commit(2)], page_size=1)
        bad = []
        bad.append(base[:1])
        duplicated = copy.deepcopy(base)
        duplicated[1]['commits'] = [commit(1)]
        bad.append(duplicated)
        for key, value in [('status', 'behind'), ('total_commits', 3),
                           ('base_commit', {'sha': NEW}), ('commits', None)]:
            changed = copy.deepcopy(base)
            changed[1][key] = value
            bad.append(changed)
        for pages in bad:
            with self.subTest(pages=pages), patch.object(inventory.Inventory, 'pages', return_value=pages):
                with self.assertRaises(ValueError):
                    inventory.Inventory().comparison('kuasar-sandbox/connector', OLD, NEW)

    def test_invalid_comparison_summary_or_commit_sha_fails(self):
        for status, total in [('unknown', 0), ('ahead', -1), ('ahead', True), ('ahead', '1')]:
            pages = comparison_pages([])
            pages[0].update(status=status, total_commits=total)
            with self.subTest(status=status, total=total), patch.object(inventory.Inventory, 'pages', return_value=pages):
                with self.assertRaises(ValueError):
                    inventory.Inventory().comparison('repo', OLD, NEW)
        pages = comparison_pages([{'sha': 'not-a-sha', 'commit': {'message': 'broken'}}])
        with patch.object(inventory.Inventory, 'pages', return_value=pages):
            with self.assertRaises(ValueError):
                inventory.Inventory().comparison('repo', OLD, NEW)

    def test_future_selected_tag_uses_pinned_source_without_resolving_future_tag(self):
        repo = 'kuasar-sandbox/sandboxer'
        responses = self.responses(repo, [commit(1)])
        del responses[f'repos/{repo}/git/ref/tags/new']
        with self.api(responses) as run:
            text = inventory.Inventory().render('sandboxer', 'old', 'future-v9.9.9', NEW)
        self.assertIn('`old` → `future-v9.9.9`', text)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(any('future-v9.9.9' in call.args[0][-1] for call in run.call_args_list))

    def test_existing_tag_resolution_url_encodes_tag_names(self):
        endpoint = 'repos/kuasar-sandbox/connector/git/ref/tags/release%2Fv1.2.3'
        with self.api({endpoint: [{'object': {'type': 'commit', 'sha': NEW}}]}):
            self.assertEqual(inventory.Inventory().tag_sha('kuasar-sandbox/connector', 'release/v1.2.3'), NEW)

    def test_invalid_source_sha_and_unresolved_tags_fail(self):
        for target in [{'type': 'tag', 'sha': NEW}, {'type': 'commit', 'sha': 'bad'}, {}]:
            with self.subTest(target=target), patch.object(inventory.Inventory, 'pages', return_value=[{'object': target}]):
                with self.assertRaises(ValueError):
                    inventory.Inventory().tag_sha('repo', 'new')
        with patch.object(inventory.Inventory, 'tag_sha', return_value=OLD):
            with self.assertRaisesRegex(ValueError, 'invalid selected source SHA'):
                inventory.Inventory().render('connector', 'old', 'new', 'bad')

    def test_equal_tags_and_initial_selection_never_query_api(self):
        with patch.object(inventory.subprocess, 'run') as run:
            self.assertEqual(inventory.Inventory().render('runtime', 'same', 'same'),
                             'Selected version unchanged: `same`.\n')
            self.assertEqual(inventory.Inventory().render('runtime', None, 'new'),
                             'Initial selected version: `new`.\n')
        run.assert_not_called()

    def test_identical_behind_and_diverged_have_explicit_semantics(self):
        repo = 'kuasar-sandbox/orchestrator'
        for status, expected in [('identical', 'without additional repository commits'),
                                 ('behind', 'version rollback, not a forward update'),
                                 ('diverged', 'commits unique to the selected source')]:
            rows = [commit(1)] if status == 'diverged' else []
            with self.subTest(status=status), self.api(self.responses(repo, rows, status)):
                text = inventory.Inventory().render('orchestrator', 'old', 'new')
            self.assertIn(expected, text)
            self.assertIn('[Full comparison]', text)
            self.assertEqual('- Change 1 ' in text, status == 'diverged')

    def test_squash_merge_and_direct_commit_titles_and_links_use_component_repository(self):
        for unit in sorted(inventory.UNITS):
            repo = 'kuasar-sandbox/' + ('guest-runtime' if unit in ('runtime', 'vmlinux') else unit)
            with self.subTest(unit=unit):
                self.assertEqual(inventory.repository(unit), repo)
                squash = inventory.commit_row(repo, commit(1, 'Fix startup (#314)'))
                merge = inventory.commit_row(repo, commit(2, 'Merge pull request #271 from org/feature\n\nImprove isolation\n\nBody text'))
                direct = inventory.commit_row(repo, commit(3, 'Document usage\n\nNot the title'))
                self.assertIn(f'- Fix startup ([#314](https://github.com/{repo}/pull/314)', squash)
                self.assertIn(f'- Improve isolation ([#271](https://github.com/{repo}/pull/271)', merge)
                self.assertNotIn('Merge pull request', merge)
                self.assertNotIn('Body text', merge)
                self.assertIn('- Document usage ([commit ', direct)
                self.assertNotIn('/pull/', direct)
                self.assertNotIn('Not the title', direct)
                for row, number in [(squash, 1), (merge, 2), (direct, 3)]:
                    self.assertIn(f'https://github.com/{repo}/commit/{number:040x}', row)

    def test_markdown_titles_are_escaped_without_changing_generated_links(self):
        text = inventory.commit_row('kuasar-sandbox/runtime', commit(1, 'Fix [link](x) <tag> *bold* _name_ `code` {x} \\ path (#9)'))
        for escaped in ['\\[link\\]', '\\<tag\\>', '\\*bold\\*', '\\_name\\_', '\\`code\\`', '\\{x\\}', '\\\\ path']:
            self.assertIn(escaped, text)
        self.assertIn('[#9](https://github.com/kuasar-sandbox/runtime/pull/9)', text)
        self.assertEqual(inventory.escape(' one\n  two\tthree '), 'one two three')

    def test_kernel_path_filter_exact_scope(self):
        for path in ['native-deps/deps/vmlinux/x86_64.config',
                     'native-deps/deps/linux-patches/fix.patch',
                     'native-deps/deps/linux-patches/nested/fix.patch']:
            self.assertTrue(inventory.kernel_path(path), path)
        for path in [None, 'src/runtime.rs', 'native-deps/deps/vmlinux/README',
                     'native-deps/deps/vmlinux/nested/x.config', 'native-deps/deps/vmlinux/foo.config.bak',
                     'native-deps/deps/linux-patches-other/fix.patch']:
            self.assertFalse(inventory.kernel_path(path), path)

    def test_kernel_filter_includes_renames_and_later_file_pages(self):
        repo = 'kuasar-sandbox/guest-runtime'
        rows = [commit(n) for n in range(1, 5)]
        responses = self.responses(repo, rows)
        files = [[{'filename': 'src/runtime.rs'}],
                 [{'filename': 'native-deps/deps/vmlinux/x.config'}],
                 [{'filename': 'archive/old.patch', 'previous_filename': 'native-deps/deps/linux-patches/old.patch'}],
                 [{'filename': f'src/file{i}'} for i in range(100)] + [{'filename': 'native-deps/deps/linux-patches/new.patch'}]]
        for row, changed in zip(rows, files):
            responses[f'repos/{repo}/commits/{row["sha"]}?per_page=100'] = [
                {'sha': row['sha'], 'files': changed[i:i + 100]} for i in range(0, len(changed), 100)]
        with self.api(responses):
            text = inventory.Inventory().render('vmlinux', 'old', 'new')
        self.assertNotIn('- Change 1 ', text)
        for number in (2, 3, 4):
            self.assertIn(f'- Change {number} ', text)
        self.assertIn('Kernel release inputs:', text)

    def test_kernel_no_matching_commits_is_reported(self):
        repo = 'kuasar-sandbox/guest-runtime'
        row = commit(1)
        responses = self.responses(repo, [row])
        responses[f'repos/{repo}/commits/{row["sha"]}?per_page=100'] = [{'sha': row['sha'], 'files': [{'filename': 'src/runtime.rs'}]}]
        with self.api(responses):
            text = inventory.Inventory().render('vmlinux', 'old', 'new')
        self.assertIn('No commits affecting kernel release inputs', text)
        self.assertNotIn('- Change 1 ', text)

    def test_kernel_file_limit_is_fail_closed_even_if_a_match_is_present(self):
        sha = commit(1)['sha']
        for count in (3000, 3001):
            files = [{'filename': 'native-deps/deps/vmlinux/x.config'}] * count
            pages = [{'sha': sha, 'files': files[i:i + 100]} for i in range(0, count, 100)]
            with self.subTest(count=count), patch.object(inventory.Inventory, 'pages', return_value=pages):
                with self.assertRaisesRegex(ValueError, 'may be truncated'):
                    inventory.Inventory().affects_kernel('repo', sha)
        with patch.object(inventory.Inventory, 'pages', return_value=[{'sha': sha, 'files': [{'filename': 'src/file'}] * 2999}]):
            self.assertFalse(inventory.Inventory().affects_kernel('repo', sha))

    def test_invalid_kernel_file_inventory_fails(self):
        for page in [{'sha': NEW, 'files': []}, {'sha': OLD, 'files': None}]:
            with self.subTest(page=page), patch.object(inventory.Inventory, 'pages', return_value=[page]):
                with self.assertRaisesRegex(ValueError, 'invalid kernel commit file inventory'):
                    inventory.Inventory().affects_kernel('repo', OLD)

    def test_shared_runtime_kernel_range_and_sources_are_cached(self):
        repo = 'kuasar-sandbox/guest-runtime'
        row = commit(1)
        responses = self.responses(repo, [row])
        file_endpoint = f'repos/{repo}/commits/{row["sha"]}?per_page=100'
        responses[file_endpoint] = [{'sha': row['sha'], 'files': [{'filename': 'native-deps/deps/vmlinux/x.config'}]}]
        subject = inventory.Inventory()
        with self.api(responses) as run:
            runtime = subject.render('runtime', 'old', 'new')
            kernel = subject.render('vmlinux', 'old', 'new')
            self.assertEqual(kernel, subject.render('vmlinux', 'old', 'new'))
        self.assertIn('also includes shared guest-runtime and kernel changes', runtime)
        self.assertIn('not limited to runtime-specific source changes', runtime)
        self.assertIn('- Change 1 ', runtime)
        self.assertIn('- Change 1 ', kernel)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(len({call.args[0][-1] for call in run.call_args_list}), 4)

    def test_api_failure_and_missing_pages_are_not_treated_as_empty_history(self):
        with patch.object(inventory.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'access denied')):
            with self.assertRaisesRegex(RuntimeError, 'access denied'):
                inventory.Inventory().pages('endpoint')
        for data in [[], {}, None]:
            with self.subTest(data=data), self.api({'endpoint': data}):
                with self.assertRaisesRegex(ValueError, 'missing API pages'):
                    inventory.Inventory().pages('endpoint')


if __name__ == '__main__':
    unittest.main()
