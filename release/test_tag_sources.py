"""Real local Git tags exercise retrieval and provenance without network/builds."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import selection
import tag_sources


def fixture(root, unit, tag):
    subprocess.run(['git', 'init', '--quiet', '--template=', str(root)], check=True)
    for key, value in (('user.name', 'Release fixture'), ('user.email', 'release@example.invalid'),
                       ('remote.origin.url', f'https://github.com/{tag_sources.repository(unit)}.git')):
        tag_sources.git(root, 'config', key, value)
    for name, text in {'go.mod': 'module fixture\n', 'product.go': 'package fixture\n',
                       'README.md': 'guide from selected tag\n', 'docs/unit.md': 'unit guide\n',
                       'test/e2e/cases/basic.fixture.sh': 'echo selected tag\n',
                       'test/e2e/lib/helper.go': 'package helper\n',
                       'release/guide-inputs.txt': 'README.md\ndocs/\n'}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    tag_sources.git(root, 'add', '.')
    tag_sources.git(root, 'commit', '--quiet', '-m', 'tag source fixture')
    tag_sources.git(root, 'tag', tag)
    return tag_sources.git(root, 'rev-parse', 'HEAD')


class TagSourceTests(unittest.TestCase):
    def test_coordinator_clone_and_fetch_are_bounded_without_prompting(self):
        import preview_coordinator
        for command in (['gh', 'repo', 'clone', 'kuasar-sandbox/connector'],
                        ['git', 'fetch', 'origin', 'refs/heads/main']):
            with mock.patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired(command, 120)) as invoke, \
                 self.assertRaisesRegex(RuntimeError, 'Git source operation timed out'):
                preview_coordinator.run(command)
            self.assertEqual(invoke.call_args.kwargs['timeout'], 120)
            self.assertEqual(invoke.call_args.kwargs['env']['GIT_TERMINAL_PROMPT'], '0')
            self.assertEqual(invoke.call_args.kwargs['env']['GCM_INTERACTIVE'], 'never')

    def test_transport_is_bounded_noninteractive_and_fails_closed(self):
        for command in ('fetch', 'ls-remote'):
            for failure in (subprocess.TimeoutExpired(['git'], 120),
                            subprocess.CalledProcessError(128, ['git'])):
                with self.subTest(command=command, failure=type(failure).__name__), \
                     mock.patch.object(subprocess, 'check_output', side_effect=failure) as invoke, \
                     self.assertRaisesRegex(ValueError, f'git {command} (timed out|failed)'):
                    tag_sources.git(Path('.'), command, 'origin', 'refs/tags/v1.2.3')
                self.assertEqual(invoke.call_args.kwargs['timeout'], tag_sources.NETWORK_TIMEOUT)
                self.assertEqual(invoke.call_args.kwargs['env']['GIT_TERMINAL_PROMPT'], '0')
                self.assertEqual(invoke.call_args.kwargs['env']['GCM_INTERACTIVE'], 'never')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / 'remote'
        self.tag = 'v1.2.3'
        self.sha = fixture(self.remote, 'connector', self.tag)
        # The production fetch command still uses the owner URL and named tag;
        # Git redirects only transport to this real local fixture repository.
        self.environment = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0':
            f'url.{self.remote}.insteadOf', 'GIT_CONFIG_VALUE_0':
            'https://github.com/kuasar-sandbox/connector.git'}

    def fetch(self, expected=None):
        with mock.patch.dict(os.environ, self.environment):
            return tag_sources.fetch_unit(self.root / 'fetched', 'connector', self.tag, expected or self.sha)

    def test_named_tag_is_only_retrieval_key_and_all_content_stays_at_tag(self):
        for name in ('README.md', 'test/e2e/cases/basic.fixture.sh', 'test/e2e/lib/helper.go'):
            (self.remote / name).write_text('new HEAD content must not ship\n')
        tag_sources.git(self.remote, 'commit', '-qam', 'ordinary docs and test changes')
        with mock.patch.object(tag_sources, 'git', wraps=tag_sources.git) as git:
            record = self.fetch()
        self.assertEqual(record['sha'], self.sha)
        self.assertEqual(record['tag'], self.tag)
        fetches = [call.args[1:] for call in git.call_args_list if call.args[1] == 'fetch']
        self.assertEqual(fetches, [('fetch', '--quiet', '--no-tags', '--depth=1', 'origin',
                                   'refs/tags/v1.2.3:refs/tags/v1.2.3')])
        for name in ('README.md', 'test/e2e/cases/basic.fixture.sh', 'test/e2e/lib/helper.go'):
            expected = tag_sources.git(self.remote, 'show', f'{self.tag}:{name}')
            self.assertEqual((self.root / 'fetched' / name).read_text().strip(), expected)

    def test_sha_branch_and_head_cannot_be_release_source_keys(self):
        for bad in (self.sha, 'main', 'HEAD', '../v1.2.3', 'refs/tags/v1.2.3'):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'unit tag'):
                tag_sources.fetch_unit(self.root / 'forbidden', 'connector', bad, self.sha)
        self.assertFalse((self.root / 'forbidden').exists())

    def test_tag_moving_before_fetch_is_rejected(self):
        (self.remote / 'README.md').write_text('moved\n')
        tag_sources.git(self.remote, 'commit', '-qam', 'new content')
        tag_sources.git(self.remote, 'tag', '-f', self.tag)
        with self.assertRaisesRegex(ValueError, 'tag moved while fetching'):
            self.fetch()

    def test_tag_moving_after_fetch_is_rejected(self):
        original = tag_sources.git
        def moved(root, *args):
            if args[0] == 'ls-remote':
                (self.remote / 'README.md').write_text('moved after checkout\n')
                original(self.remote, 'commit', '-qam', 'moved')
                original(self.remote, 'tag', '-f', self.tag)
            return original(root, *args)
        with mock.patch.object(tag_sources, 'git', side_effect=moved), self.assertRaisesRegex(ValueError, 'tag moved after fetching'):
            self.fetch()

    def test_tracked_symbolic_link_and_wrong_owner_are_rejected(self):
        (self.remote / 'link').symlink_to('README.md')
        tag_sources.git(self.remote, 'add', 'link')
        tag_sources.git(self.remote, 'commit', '-qm', 'linked input')
        self.tag = 'v1.2.4'
        tag_sources.git(self.remote, 'tag', self.tag)
        with self.assertRaisesRegex(ValueError, 'contains a link or submodule'):
            self.fetch(tag_sources.git(self.remote, 'rev-parse', 'HEAD'))
        with self.assertRaisesRegex(ValueError, 'source repository differs from its owner'):
            tag_sources.inspect_unit(self.remote, 'orchestrator', self.tag)

    def test_modified_staged_untracked_ignored_and_link_inputs_fail(self):
        self.fetch()
        source = self.root / 'fetched'
        (source / 'README.md').write_text('modified\n')
        for staged in (False, True):
            if staged:
                tag_sources.git(source, 'add', 'README.md')
            with self.assertRaisesRegex(ValueError, 'modified tag inputs'):
                tag_sources.inspect_unit(source, 'connector', self.tag)
        tag_sources.git(source, 'reset', '--hard', 'HEAD')
        (source / '.git/info').mkdir(exist_ok=True)
        (source / '.git/info/exclude').write_text('extra.go\n')
        (source / 'extra.go').write_text('package extra\n')
        with self.assertRaisesRegex(ValueError, 'untracked tag inputs'):
            tag_sources.inspect_unit(source, 'connector', self.tag)
        (source / 'extra.go').unlink()
        (source / 'link').symlink_to('README.md')
        with self.assertRaisesRegex(ValueError, 'untracked tag inputs'):
            tag_sources.inspect_unit(source, 'connector', self.tag)

    def test_unified_owner_derivation_preserves_independent_vmlinux(self):
        records = {unit: {'repository': tag_sources.repository(unit), 'sha': str(index) * 40, 'tree': 'a' * 40,
                         'tag': ('runtime-' if unit == 'runtime' else 'vmlinux-' if unit == 'vmlinux' else '') + self.tag}
                   for index, unit in enumerate(selection.UNITS, 1)}
        pins = tag_sources.owner_revisions(records)
        self.assertEqual(pins['guest-runtime'], records['runtime']['sha'])
        self.assertNotEqual(pins['guest-runtime'], records['vmlinux']['sha'])
        self.assertEqual(set(pins), set(selection.TEST_OWNERS))

    def test_missing_guide_requires_explicit_new_tag_not_head_fallback(self):
        sources = self.root / 'sources'
        selected = {unit: ('runtime-' if unit == 'runtime' else 'vmlinux-' if unit == 'vmlinux' else '') + self.tag
                    for unit in selection.UNITS}
        for unit, tag in selected.items():
            fixture(sources / unit, unit, tag)
        old = sources / 'connector'
        (old / 'release/guide-inputs.txt').unlink()
        tag_sources.git(old, 'commit', '-qam', 'old tag without guide declaration')
        tag_sources.git(old, 'tag', '-f', self.tag)
        tag_sources.inspect_sources(sources, selected)
        with self.assertRaisesRegex(ValueError, 'first cutover requires a normal new component tag'):
            tag_sources.require_owner_guides(sources)
        (old / 'release/guide-inputs.txt').write_text('README.md\ndocs/\n')
        tag_sources.git(old, 'add', '.')
        tag_sources.git(old, 'commit', '-qm', 'new delivery declaration')
        tag_sources.git(old, 'tag', 'v1.2.4')
        with self.assertRaisesRegex(ValueError, 'source differs from selected tag'):
            tag_sources.inspect_sources(sources, selected)
        selected['connector'] = 'v1.2.4'
        tag_sources.inspect_sources(sources, selected)
        tag_sources.require_owner_guides(sources)
        self.assertEqual(tag_sources.git(old, 'diff', self.tag, 'v1.2.4', '--', 'product.go'), '')


if __name__ == '__main__':
    unittest.main()
