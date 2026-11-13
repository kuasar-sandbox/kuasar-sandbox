import copy
import importlib.util
import io
import hashlib
import os
import re
import subprocess
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

    def archive(self, directory, *, language='en', guide='installation', alias=False,
                mode=0o644, kind=tarfile.REGTYPE, duplicate=False, text=None, members=None):
        suffix = '_zh' if language == 'zh' else ''
        name = ('./' if alias else '') + f'guide/{guide}{suffix}.md'
        content = (ROOT / 'docs' / f'installation{suffix}.md').read_bytes() if text is None else text
        if members is None:
            members = [(name, mode, kind)] * (2 if duplicate else 1)
        path = Path(directory) / 'platform.tar.gz'
        with tarfile.open(path, 'w:gz') as stream:
            for name, mode, kind in members:
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
        for guide in ('installation', 'download'):
            for alias in (False, True):
                with self.subTest(guide=guide, alias=alias), tempfile.TemporaryDirectory() as directory:
                    en = check.guide_script(self.archive(directory, guide=guide, alias=alias), 'en')
                    zh = check.guide_script(self.archive(directory, language='zh', guide=guide, alias=alias), 'zh')
                    self.assertEqual(en, zh)
                    self.assertIn('docker load --input', en)
                    self.assertIn('RELEASE_VERSION', en)
                    self.assertIn('files.tsv', en)

    def test_installation_guide_only_installs_a_complete_executable_init(self):
        for suffix in ('', '_zh'):
            doc = (ROOT / 'docs' / f'installation{suffix}.md').read_text()
            install = re.findall(r'(?ms)^```bash\n(.*?)^```', doc)[1]
            for mode in (None, 0o644, 0o755):
                with self.subTest(language=suffix, mode=mode), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp); source = root / 'download'; source.mkdir()
                    target = root / 'installed'
                    archive = source / 'products.tar.gz'
                    with tarfile.open(archive, 'w:gz') as out:
                        items = [('test/demo/demo_common.sh', 0o644), ('test/demo/requirements.txt', 0o644),
                                 ('bin/other', 0o755)]
                        if mode is not None: items.append(('bin/sandbox-init', mode))
                        for name, perm in items:
                            entry = tarfile.TarInfo(name); entry.mode = perm
                            out.addfile(entry)
                    payload = archive.read_bytes()
                    (source / 'files.tsv').write_text(
                        f'{archive.name}\t{len(payload)}\tsha256:{hashlib.sha256(payload).hexdigest()}\n')
                    result = subprocess.run(['bash','-c',install], cwd=root, capture_output=True,
                        env={**os.environ, 'INSTALL_DIR':str(target), 'DOWNLOAD_DIR':str(source),
                             'DOWNLOAD_WORKBENCH':'0'})
                    self.assertEqual(result.returncode == 0, mode == 0o755, result.stderr)
                    self.assertEqual(target.exists(), mode == 0o755)

    def test_reject_dangerous_or_duplicate_guide(self):
        for guide in ('installation', 'download'):
            for change in [dict(mode=0o666), dict(mode=0o4644), dict(kind=tarfile.SYMTYPE),
                           dict(kind=tarfile.DIRTYPE), dict(duplicate=True), dict(text=b''),
                           dict(text=b'x' * (1024 * 1024 + 1)), dict(text=b'no commands')]:
                with self.subTest(guide=guide, fields=list(change), text_size=len(change.get('text', b''))), tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(ValueError):
                        check.guide_script(self.archive(directory, guide=guide, **change), 'en')

    def test_reject_alias_duplicates_and_current_legacy_ambiguity(self):
        for language, suffix in (('en', ''), ('zh', '_zh')):
            current, legacy = f'guide/installation{suffix}.md', f'guide/download{suffix}.md'
            for names in ((current, legacy), (current, './' + current),
                          (legacy, './' + legacy), ('./' + current, './' + legacy)):
                with self.subTest(language=language, names=names), tempfile.TemporaryDirectory() as directory:
                    members = [(name, 0o644, tarfile.REGTYPE) for name in names]
                    with self.assertRaisesRegex(ValueError, 'expected one bundled download guide'):
                        check.guide_script(self.archive(directory, language=language, members=members), language)

    def test_unsafe_current_guide_never_falls_back_to_legacy(self):
        for language, suffix in (('en', ''), ('zh', '_zh')):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as directory:
                members = [(f'guide/installation{suffix}.md', 0o666, tarfile.REGTYPE),
                           (f'guide/download{suffix}.md', 0o644, tarfile.REGTYPE)]
                with self.assertRaisesRegex(ValueError, 'expected one bundled download guide'):
                    check.guide_script(self.archive(directory, language=language, members=members), language)

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
