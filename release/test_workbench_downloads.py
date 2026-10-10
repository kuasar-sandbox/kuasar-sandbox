"""Execute the public quickstart's asset selection with release metadata fixtures."""
import hashlib
import io
import json
import re
import tarfile
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TAG = 'release-v1.2.3'


class ManifestRetrievalTests(unittest.TestCase):
    def retrieve(self, guide, *, tag=TAG, requested='', source='a' * 40, fail_manifest=False):
        text = (ROOT / 'docs' / guide).read_text()
        block = re.findall(r'```bash\n(.*?)\n```', text, re.S)[0]
        # Execute the real metadata/manifest download entrance, before asset selection.
        entrance = block.split('\nRELEASE_VERSION="$(python3 - ', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'metadata.json').write_text(json.dumps({'tag_name': tag, 'target_commitish': source}))
            transport = root / 'transport.py'
            transport.write_text('''import json, os, pathlib, sys
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
url = sys.argv[-1]
with (root / 'requests.jsonl').open('a') as stream:
    stream.write(json.dumps(url) + '\\n')
if url.startswith('https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox/releases/'):
    sys.stdout.write((root / 'metadata.json').read_text())
elif url.startswith('https://raw.githubusercontent.com/'):
    if os.environ['FAIL_MANIFEST'] == '1':
        raise SystemExit(22)
    print('manifest fixture')
else:
    raise SystemExit('unexpected network request: ' + url)
''')
            result = subprocess.run(['bash', '-c',
                'curl() { python3 -B "$FIXTURE_ROOT/transport.py" "$@"; }\n' +
                entrance + '\ncat "$SELECTION_MANIFEST"\n'],
                cwd=root, env=os.environ | {'RELEASE_VERSION': requested,
                    'FIXTURE_ROOT': str(root), 'FAIL_MANIFEST': str(int(fail_manifest)),
                    'TMPDIR': str(root), 'PYTHONDONTWRITEBYTECODE': '1'},
                capture_output=True, text=True, timeout=15)
            requests = root / 'requests.jsonl'
            urls = [json.loads(line) for line in requests.read_text().splitlines()] if requests.exists() else []
            return result, urls

    def test_stable_and_preview_fetch_manifest_by_validated_release_tag(self):
        preview = TAG + '-preview.20260101.2'
        for guide in ('download.md', 'download_zh.md'):
            for tag, requested, manifest in ((TAG, '', 'release.yaml'),
                                             (TAG, TAG, 'release.yaml'),
                                             (preview, preview, 'daily-preview.yaml')):
                with self.subTest(guide=guide, tag=tag, requested=requested):
                    result, urls = self.retrieve(guide, tag=tag, requested=requested)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, 'manifest fixture\n')
                    endpoint = 'tags/' + requested if requested else 'latest'
                    self.assertEqual(urls, [
                        'https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox/releases/' + endpoint,
                        f'https://raw.githubusercontent.com/kuasar-sandbox/kuasar-sandbox/{tag}/releases/{manifest}'])

    def test_malformed_release_tags_stop_before_manifest_retrieval(self):
        for guide in ('download.md', 'download_zh.md'):
            for tag in ('main', 'a' * 40, 'release-v01.2.3', TAG + '/../../main',
                        TAG + '\n', TAG + '-preview.20260101.0', '', None):
                with self.subTest(guide=guide, tag=tag):
                    result, urls = self.retrieve(guide, tag=tag)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(urls, ['https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox/releases/latest'])

    def test_explicit_version_mismatch_stops_before_manifest_retrieval(self):
        for guide in ('download.md', 'download_zh.md'):
            result, urls = self.retrieve(guide, requested='release-v2.0.0')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(urls, ['https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox/releases/tags/release-v2.0.0'])

    def test_source_provenance_is_still_required(self):
        for guide in ('download.md', 'download_zh.md'):
            for source in ('main', 'a' * 39, 'A' * 40, '', None):
                with self.subTest(guide=guide, source=source):
                    result, urls = self.retrieve(guide, source=source)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(urls, ['https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox/releases/latest'])

    def test_failed_tag_download_has_no_sha_or_main_fallback(self):
        for guide in ('download.md', 'download_zh.md'):
            result, urls = self.retrieve(guide, fail_manifest=True)
            self.assertEqual(result.returncode, 22)
            self.assertEqual(urls, [
                'https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox/releases/latest',
                f'https://raw.githubusercontent.com/kuasar-sandbox/kuasar-sandbox/{TAG}/releases/release.yaml'])


class DownloadTests(unittest.TestCase):
    def select(self, *, contract='workbench-v1', omit=(), extra=(), download=False, missing_binding=False, arch='x86_64'):
        names = ['SHA256SUMS', f'platform-{TAG}.tar.gz']
        for asset_arch in ('x86_64', 'aarch64'):
            names += [f'{unit}-v1.2.3-linux-{asset_arch}.tar.gz' for unit in ('accelerator', 'connector', 'orchestrator', 'sandboxer')]
            names += [f'sandbox-runtime-{asset_arch}-v1.2.3.tar.gz', f'vmlinux-{asset_arch}-v1.2.3.tar.gz']
            if contract == 'workbench-v1':
                names += [f'workbench-{asset_arch}-v1.2.3.tar.gz']
        names = [name for name in names if name not in omit] + list(extra)
        metadata = {'tag_name': TAG, 'draft': False, 'prerelease': False,
                    'body': '<!-- kuasar-integration-validation ' + json.dumps({'delivery': contract}) + ' -->' if contract != 'historical' else '',
                    'assets': [{'name': name, 'size': 100, 'digest': 'sha256:' + 'a'*64,
                        'browser_download_url': f'https://github.com/kuasar-sandbox/kuasar-sandbox/releases/download/{TAG}/{name}'} for name in names]}
        if missing_binding:
            metadata['body'] = ''
        sources = [(ROOT / 'docs' / name).read_text().split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
                   for name in ('download.md', 'download_zh.md')]
        self.assertEqual(*sources)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'release.json').write_text(json.dumps(metadata))
            (root / 'selection.yaml').write_text('version: ' + TAG + '\n' +
                ('delivery: ' + contract + '\n' if contract != 'historical' else ''))
            result = subprocess.run([sys.executable, '-c', sources[0], str(root / 'release.json'), TAG, str(root / 'assets.tsv'), str(root / 'selection.yaml')],
                                    env=os.environ | {'DOWNLOAD_WORKBENCH': str(int(download)), 'ARCH': arch}, capture_output=True, text=True)
            return result.returncode, (root / 'assets.tsv').read_text().splitlines() if (root / 'assets.tsv').exists() else []

    def test_native_product_and_optional_image_roles(self):
        for download in (False, True):
            code, rows = self.select(download=download)
            self.assertEqual(code, 0)
            roles = [row.split('\t')[-1] for row in rows]
            self.assertEqual(roles.count('product'), 6)
            self.assertEqual(roles.count('platform'), 1)
            self.assertEqual(roles.count('checksum'), 1)
            self.assertEqual(roles.count('workbench'), int(download))
            self.assertFalse(any('aarch64' in row for row in rows))

    def test_missing_or_unknown_assets_cannot_downgrade_contract(self):
        for missing in [('workbench-aarch64-v1.2.3.tar.gz',),
                        ('workbench-aarch64-v1.2.3.tar.gz', 'workbench-x86_64-v1.2.3.tar.gz'),
                        ('connector-v1.2.3-linux-aarch64.tar.gz',)]:
            self.assertNotEqual(self.select(omit=missing)[0], 0)
        self.assertNotEqual(self.select(extra=('unknown.tar.gz',))[0], 0)
        self.assertNotEqual(self.select(contract='unknown')[0], 0)
        self.assertNotEqual(self.select(extra=('SHA256SUMS',))[0], 0)
        self.assertNotEqual(self.select(missing_binding=True, omit=('workbench-aarch64-v1.2.3.tar.gz', 'workbench-x86_64-v1.2.3.tar.gz'))[0], 0)

    def test_arm_selects_native_products_and_image(self):
        code, rows = self.select(download=True, arch='aarch64')
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 9)
        self.assertFalse(any('x86_64' in row for row in rows))
        self.assertTrue(any('workbench-aarch64-' in row for row in rows))

    def test_historical_cannot_silently_supply_workbench(self):
        self.assertNotEqual(self.select(contract='historical', download=True)[0], 0)
        self.assertNotEqual(self.select(arch='unsupported')[0], 0)

    def test_historical_contract_stays_readable(self):
        code, rows = self.select(contract='historical')
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 8)


class DownloadBytesTests(unittest.TestCase):
    def validate(self, *, missing=False, corrupt=False, entry='bin/tool', symlink=False, collision=False,
                 root_entry=None, root_type=tarfile.DIRTYPE, root_mode=0o755):
        text = (ROOT / 'docs/download.md').read_text()
        source = text.split("<<'PY'\n")[2].split('\nPY\n', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows, sums, assets = [], [], []
            for filename, role, path in [('platform.tar.gz', 'platform', 'guide/README.md'),
                                         ('product.tar.gz', 'product', 'guide/README.md' if collision else entry)]:
                target = root / filename
                with tarfile.open(target, 'w:gz') as archive:
                    if root_entry is not None:
                        root_info = tarfile.TarInfo(root_entry)
                        root_info.type = root_type
                        root_info.mode = root_mode
                        if root_type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                            root_info.linkname = '/outside'
                        archive.addfile(root_info)
                    info = tarfile.TarInfo(path)
                    info.mode = 0o644
                    if symlink and role == 'product':
                        info.type, info.linkname = tarfile.SYMTYPE, '/outside'
                        archive.addfile(info)
                    else:
                        content = b'fixture\n'
                        info.size = len(content)
                        archive.addfile(info, io.BytesIO(content))
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                sums.append(f'{digest}  {filename}\n')
                assets.append({'name': filename})
                rows.append(f'{filename}\tunused\tsha256:{digest}\t{target.stat().st_size}\t{role}\n')
            # A native-only download need not contain an unselected architecture.
            sums.append('c' * 64 + '  unselected-other-architecture.tar.gz\n')
            assets += [{'name': 'unselected-other-architecture.tar.gz'}, {'name': 'SHA256SUMS'}]
            checksum = root / 'SHA256SUMS'
            checksum.write_text(''.join(sums))
            digest = hashlib.sha256(checksum.read_bytes()).hexdigest()
            rows.append(f'SHA256SUMS\tunused\tsha256:{digest}\t{checksum.stat().st_size}\tchecksum\n')
            (root / 'assets.tsv').write_text(''.join(rows))
            (root / 'release.json').write_text(json.dumps({'assets': assets}))
            if missing:
                (root / 'product.tar.gz').unlink()
            if corrupt:
                with (root / 'product.tar.gz').open('ab') as stream:
                    stream.write(b'corrupt')
            return subprocess.run([sys.executable, '-c', source, str(root)], capture_output=True).returncode

    def test_native_subset_accepts_missing_unselected_assets(self):
        self.assertEqual(self.validate(), 0)

    def test_standard_archive_root_directories_are_accepted(self):
        for root in ('.', './', '././'):
            with self.subTest(root=root):
                self.assertEqual(self.validate(root_entry=root), 0)

    def test_root_exception_rejects_unsafe_names_types_and_modes(self):
        for root in ('', '/', '//', '../'):
            with self.subTest(root=root):
                self.assertNotEqual(self.validate(root_entry=root), 0)
        for kind in (tarfile.REGTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                self.assertNotEqual(self.validate(root_entry='.', root_type=kind), 0)
        for mode in (0o4755, 0o2755):
            with self.subTest(mode=mode):
                self.assertNotEqual(self.validate(root_entry='.', root_mode=mode), 0)

    def test_missing_selected_asset_is_not_hidden(self):
        self.assertNotEqual(self.validate(missing=True), 0)

    def test_changed_selected_asset_is_rejected(self):
        self.assertNotEqual(self.validate(corrupt=True), 0)

    def test_unsafe_members_and_cross_archive_collisions_are_rejected(self):
        for change in ({'entry': '../outside'}, {'entry': '/outside'}, {'symlink': True}, {'collision': True}):
            with self.subTest(change=change):
                self.assertNotEqual(self.validate(**change), 0)


class DocumentedCommandTests(unittest.TestCase):
    def test_primary_guides_keep_equivalent_executable_examples(self):
        for stem in ('docs/quickstart', 'docs/download', 'test/demo/DEMO', 'test/QUICKSTART'):
            blocks = []
            for suffix in ('', '_zh'):
                text = (ROOT / (stem + suffix + '.md')).read_text()
                code = re.findall(r'```(?:bash|sh)\n(.*?)\n```', text, re.S)
                for snippet in code:
                    result = subprocess.run(['bash', '-n'], input=snippet, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, f'{stem}{suffix}: {result.stderr}')
                blocks.append(['\n'.join(line for line in snippet.splitlines()
                                           if line.strip() and not line.lstrip().startswith('#')) for snippet in code])
            self.assertEqual(*blocks, stem)

    def test_quickstart_uses_readonly_inputs_and_the_prepared_adapter(self):
        for suffix in ('', '_zh'):
            text = (ROOT / f'docs/quickstart{suffix}.md').read_text()
            self.assertIn('--network bridge', text)
            self.assertIn('--include basic.demo.sh', text)
            self.assertIn('prepared.py run', text)
            self.assertIn('--data-dir /work/kuasar-demo-first --quick', text)
            self.assertNotIn('pip install', text)
            self.assertNotIn('--network host', text)
            self.assertNotIn('test "$(uname -m)" = x86_64', text)


if __name__ == '__main__':
    unittest.main()
