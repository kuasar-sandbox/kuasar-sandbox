import copy
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('published_download', ROOT / 'ci/integration/check-published-download.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
VERSION = 'release-v1.2.3-preview.20261104.2'
ASSET = dict(id=7, name=f'platform-{VERSION}.tar.gz', size=123,
             digest='sha256:' + 'a' * 64, state='uploaded')


class PublishedDownloadTests(unittest.TestCase):
    def release(self):
        return dict(tag_name=VERSION, draft=False, prerelease=True, assets=[copy.deepcopy(ASSET)])

    def archive(self, directory, *, language='en', mode=0o644, kind=tarfile.REGTYPE, duplicate=False, text=None):
        name = 'guide/download' + ('_zh' if language == 'zh' else '') + '.md'
        content = (ROOT / 'docs' / Path(name).name).read_bytes() if text is None else text
        path = Path(directory) / 'platform.tar.gz'
        with tarfile.open(path, 'w:gz') as stream:
            for _ in range(2 if duplicate else 1):
                info = tarfile.TarInfo(name)
                info.mode, info.type = mode, kind
                info.linkname = '/outside' if kind == tarfile.SYMTYPE else ''
                info.size = len(content) if kind == tarfile.REGTYPE else 0
                stream.addfile(info, io.BytesIO(content) if info.size else None)
        return path

    def test_human_release_without_checksum_or_history(self):
        release = self.release()
        release['body'] = 'Human release notes only.'
        self.assertEqual(check.platform_asset(release, VERSION), ASSET)
        release['body'] = 'ignored historical marker'
        self.assertEqual(check.platform_asset(release, VERSION), ASSET)

    def test_reject_missing_or_invalid_native_metadata(self):
        for key, value in [('id', None), ('id', True), ('size', 0), ('size', True),
                           ('digest', None), ('digest', 'sha256:bad'), ('state', 'new')]:
            with self.subTest(key=key, value=value):
                release = self.release(); release['assets'][0][key] = value
                with self.assertRaises(ValueError): check.platform_asset(release, VERSION)

    def test_reject_wrong_release_and_missing_or_duplicate_platform(self):
        for key, value in [('tag_name', 'release-v1.2.3'), ('draft', True), ('prerelease', False),
                           ('assets', []), ('assets', [ASSET, ASSET])]:
            with self.subTest(key=key, value=value):
                release = self.release(); release[key] = value
                with self.assertRaises(ValueError): check.platform_asset(release, VERSION)
        with self.assertRaises(ValueError): check.platform_asset(self.release(), 'main')

    def test_real_bilingual_guides_have_identical_executable_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            en = check.guide_script(self.archive(directory), 'en')
            zh = check.guide_script(self.archive(directory, language='zh'), 'zh')
        self.assertEqual(en, zh)
        self.assertIn('docker load --input', en)
        self.assertIn('RELEASE_VERSION', en)
        self.assertIn('files.tsv', en)

    def test_reject_dangerous_or_duplicate_guide(self):
        for change in [dict(mode=0o666), dict(mode=0o4644), dict(kind=tarfile.SYMTYPE), dict(duplicate=True), dict(text=b'no commands')]:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    check.guide_script(self.archive(directory, **change), 'en')

    def test_post_publication_check_has_two_native_runners_and_no_write_permissions(self):
        workflow = (ROOT / '.github/workflows/aggregate-release.yml').read_text()
        job = workflow.split('  published-download:', 1)[1].split('  cleanup:', 1)[0]
        self.assertIn('needs: [prepare, publish]', job)
        self.assertIn('runner: [ubuntu-24.04, ubuntu-24.04-arm]', job)
        self.assertIn('inputs.validation_only != true', job)
        self.assertIn('contents: read', job)
        self.assertNotIn(': write', job)
        self.assertIn('needs: [publish, published-download]', workflow)


if __name__ == '__main__':
    unittest.main()
