"""Keep revised online-guide checks explicit and bound to committed source."""
import importlib.util
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'published_download', ROOT / 'ci/integration/check-published-download.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
FENCE = chr(96) * 3


def guide(label):
    return ''.join(f'{FENCE}bash\nprintf "{label}-{step}\\n"\n{FENCE}\n'
                   for step in ('download', 'install', 'image'))


class GuideSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.checkout = self.root / 'checkout'
        self.checkout.mkdir()
        self.git('init', '--quiet')
        (self.checkout / 'docs').mkdir()
        for suffix in ('', '_zh'):
            (self.checkout / f'docs/installation{suffix}.md').write_text(guide('committed' + suffix))
        self.commit()
        self.revision = self.git('rev-parse', 'HEAD').strip()
        self.archive = self.root / 'platform.tar.gz'
        data = guide('bundled').encode()
        with tarfile.open(self.archive, 'w:gz') as stream:
            member = tarfile.TarInfo('guide/installation.md')
            member.mode, member.size = 0o644, len(data)
            stream.addfile(member, io.BytesIO(data))

    def git(self, *args):
        return subprocess.check_output(
            ['git', '-C', str(self.checkout), *args], text=True, stderr=subprocess.PIPE)

    def commit(self):
        self.git('add', '.')
        self.git('-c', 'user.name=Guide test', '-c', 'user.email=guide-test@example.invalid',
                 'commit', '--quiet', '-m', 'guide fixture')

    def select_current(self, *, revision=None, language='en'):
        return check.selected_guide_script(
            self.archive, language, source='current-checkout',
            revision=self.revision if revision is None else revision, checkout=self.checkout)

    def test_default_uses_bundled_guide_without_consulting_checkout(self):
        args = check.argument_parser().parse_args(
            ['--version', 'release-v1.2.3', '--root', str(self.root / 'output')])
        self.assertEqual(args.guide_source, 'bundled')
        self.assertIsNone(args.guide_revision)
        with patch.object(check, 'checkout_guide_script', side_effect=AssertionError('unexpected online guide')):
            commands = check.selected_guide_script(self.archive, 'en', checkout=self.checkout)
        self.assertIn('bundled-download', commands)
        self.assertNotIn('committed-download', commands)

    def test_explicit_current_checkout_uses_exact_committed_guide_for_each_language(self):
        with patch.object(check, 'guide_script', side_effect=AssertionError('unexpected bundled fallback')):
            for language, suffix in (('en', ''), ('zh', '_zh')):
                with self.subTest(language=language):
                    commands = self.select_current(language=language)
                    self.assertEqual(commands, check.guide_commands(guide('committed' + suffix)))

    def test_requires_full_revision_and_exact_head(self):
        for revision in (None, 'HEAD', 'main', self.revision[:12], self.revision.upper(), 'f' * 39):
            with self.subTest(revision=revision), self.assertRaisesRegex(ValueError, 'full lowercase'):
                check.selected_guide_script(self.archive, 'en', source='current-checkout',
                                            revision=revision, checkout=self.checkout)
        (self.checkout / 'other.txt').write_text('a later checkout\n')
        self.commit()
        with self.assertRaisesRegex(ValueError, 'exact current checkout HEAD'):
            self.select_current()

    def test_bundled_mode_rejects_revision_instead_of_ignoring_it(self):
        with self.assertRaisesRegex(ValueError, 'bundled guides do not accept'):
            check.selected_guide_script(self.archive, 'en', revision=self.revision)

    def test_rejects_staged_unstaged_and_hidden_worktree_guide_changes(self):
        path = self.checkout / 'docs/installation.md'
        original = path.read_text()
        for change in ('unstaged', 'staged', 'index-only', 'assume-unchanged'):
            with self.subTest(change=change):
                self.git('update-index', '--no-assume-unchanged', 'docs/installation.md')
                self.git('reset', '--hard', '--quiet', self.revision)
                if change == 'assume-unchanged':
                    self.git('update-index', '--assume-unchanged', 'docs/installation.md')
                path.write_text(guide('uncommitted'))
                if change in ('staged', 'index-only'):
                    self.git('add', 'docs/installation.md')
                if change == 'index-only':
                    path.write_text(original)
                with self.assertRaisesRegex(ValueError, 'guide (checkout|worktree)'):
                    self.select_current()

    def test_malformed_committed_online_guide_never_falls_back_to_bundle(self):
        (self.checkout / 'docs/installation.md').write_text('No executable guide\n')
        self.commit()
        revision = self.git('rev-parse', 'HEAD').strip()
        with patch.object(check, 'guide_script', side_effect=AssertionError('unexpected fallback')):
            with self.assertRaisesRegex(ValueError, 'expected download, installation and image-import'):
                self.select_current(revision=revision)

    def test_invalid_bundle_never_falls_back_to_valid_checkout(self):
        with tarfile.open(self.archive, 'w:gz'):
            pass
        with patch.object(check, 'checkout_guide_script', side_effect=AssertionError('unexpected fallback')):
            with self.assertRaisesRegex(ValueError, 'expected one bundled download guide'):
                check.selected_guide_script(self.archive, 'en', checkout=self.checkout)


if __name__ == '__main__':
    unittest.main()
