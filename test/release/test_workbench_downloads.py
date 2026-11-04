"""Execute the published download examples, including native asset selection."""
import base64
import copy
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

ROOT = Path(__file__).resolve().parents[2]
TAG = 'release-v1.2.3'
API = 'https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox'


def fixture(tag=TAG, delivery='workbench-v1', dual=True):
    units = dict(accelerator='v1.1.1', connector='v1.2.3', sandboxer='v1.3.1', orchestrator='v1.4.2',
                 runtime='runtime-v1.5.1', vmlinux='vmlinux-v1.0.1')
    version, _, preview = tag.partition('-preview.')
    text = 'version: ' + version + '\n'
    if preview: text += 'preview_version: preview.' + preview + '\n'
    if delivery: text += 'delivery: ' + delivery + '\n'
    text += 'components:\n' + ''.join('  ' + u + ': ' + v + '\n' for u,v in units.items())
    names = [f'platform-{tag}.tar.gz']
    for arch in ('x86_64', 'aarch64') if dual else ('x86_64',):
        for unit, version in units.items():
            names.append(f'sandbox-runtime-{arch}-{version.removeprefix("runtime-")}.tar.gz' if unit == 'runtime' else
                         f'vmlinux-{arch}-{version.removeprefix("vmlinux-")}.tar.gz' if unit == 'vmlinux' else
                         f'{unit}-{version}-linux-{arch}.tar.gz')
        if delivery == 'workbench-v1': names.append(f'workbench-{arch}-{tag.removeprefix("release-")}.tar.gz')
    return dict(release=dict(tag_name=tag, draft=False, prerelease='-preview.' in tag, body='Human notes only',
        assets=[dict(name=n, id=i+1, size=100, digest='sha256:'+'a'*64, state='uploaded') for i,n in enumerate(names)]),
        manifest=text, commits=['b'*40, 'b'*40])


class DownloadTests(unittest.TestCase):
    def select(self, data=None, *, tag=TAG, requested=None, arch='x86_64', download=True, failure=None):
        data = fixture(tag) if data is None else data
        requested = tag if requested is None else requested
        sources = [(ROOT/'docs'/n).read_text().split("<<'PY'\n",1)[1].split('\nPY\n',1)[0]
                   for n in ('download.md','download_zh.md')]
        self.assertEqual(*sources)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            code = root/'example.py'; code.write_text(sources[0])
            (root/'fixture.json').write_text(json.dumps(dict(data=data, failure=failure)))
            wrapper = r"""import base64, io, json, pathlib, sys, urllib.request
root=pathlib.Path(sys.argv[1]); fixture=json.loads((root/'fixture.json').read_text()); data=fixture['data']
api='https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox'; urls=[]; tag=data['release']['tag_name']
def read(request, timeout):
    url=request.full_url; urls.append(url)
    (root/'requests.json').write_text(json.dumps(urls))
    if fixture['failure'] == len(urls): raise TimeoutError('metadata unavailable')
    if url in (api+'/releases/latest',api+'/releases/tags/'+tag): value=data['release']
    elif url==api+'/commits/refs/tags/'+tag:
        value={'sha': data['commits'][urls.count(url)-1]}
    elif url==api+'/contents/releases/'+('daily-preview.yaml' if '-preview.' in tag else 'release.yaml')+'?ref='+tag:
        value={'type':'file','encoding':'base64','content':base64.b64encode(data['manifest'].encode()).decode()}
    else: raise AssertionError('unexpected request '+url)
    return io.BytesIO(json.dumps(value).encode())
urllib.request.urlopen=read
sys.argv=['-',sys.argv[2],str(root/'assets.tsv')]
exec(compile((root/'example.py').read_text(),'<documented selector>','exec'))
"""
            result=subprocess.run([sys.executable,'-c',wrapper,str(root),requested],capture_output=True,text=True,
                env=os.environ|{'ARCH':arch,'DOWNLOAD_WORKBENCH':str(int(download))},timeout=15)
            urls=json.loads((root/'requests.json').read_text()) if (root/'requests.json').exists() else []
            rows=(root/'assets.tsv').read_text().splitlines() if (root/'assets.tsv').exists() else []
            self.assertFalse(any('/actions/' in u or 'main' in u for u in urls))
            if requested: self.assertFalse(any('/latest' in u for u in urls))
            self.assertFalse((root/'evidence.json').exists())
            return result,rows,urls

    def test_tag_manifest_native_subset_and_optional_workbench(self):
        for arch in ('x86_64','aarch64'):
            for download in (True,False):
                result,rows,urls=self.select(arch=arch,download=download)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(len(rows),8 if download else 7)
                self.assertTrue(all(('aarch64' if arch=='x86_64' else 'x86_64') not in row for row in rows))
                self.assertTrue(any(f'sandbox-runtime-{arch}-v1.5.1' in row for row in rows))
                self.assertTrue(any(f'vmlinux-{arch}-v1.0.1' in row for row in rows))
                for row in rows:
                    name,size,digest,url=row.split('\t')
                    self.assertEqual(size,'100'); self.assertEqual(digest,'sha256:'+'a'*64)
                    self.assertRegex(url,re.escape(API)+'/releases/assets/[1-9][0-9]*$')

    def test_latest_resolves_once_and_preview_uses_its_manifest(self):
        result,_,urls=self.select(requested='')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(sum('/latest' in u for u in urls),1)
        result,_,urls=self.select(tag=TAG+'-preview.20261104.2')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(any('daily-preview.yaml?ref='+TAG+'-preview.20261104.2' in u for u in urls))

    def test_old_markers_checksums_and_test_records_are_not_audited(self):
        data=fixture(); data['release']['body']='<!-- kuasar-integration-validation invalid -->'
        data['release']['assets'].append(dict(name='SHA256SUMS'))
        data['manifest']+='test_revisions:\n  runtime: stale\n'
        result,rows,_=self.select(data)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(rows),8)

    def test_historical_single_arch_and_no_workbench(self):
        data=fixture(delivery=None,dual=False)
        self.assertEqual(self.select(data,download=False)[0].returncode,0)
        self.assertNotEqual(self.select(data)[0].returncode,0)
        self.assertNotEqual(self.select(data,download=False,arch='aarch64')[0].returncode,0)

    def test_missing_duplicate_extra_assets_and_bad_native_metadata_fail(self):
        mutations=[lambda d:d['release']['assets'].pop(),
            lambda d:d['release']['assets'].append(dict(d['release']['assets'][0])),
            lambda d:d['release']['assets'].append(dict(name='unexpected.tar.gz')),
            lambda d:d['release']['assets'][0].update(digest=None),
            lambda d:d['release']['assets'][0].update(size=0),
            lambda d:d['release']['assets'][0].update(id=True),
            lambda d:d['release']['assets'][0].update(state='new'),
            lambda d:d.update(manifest=d['manifest'].replace('version: '+TAG,'version: release-v9.9.9')),
            lambda d:d.update(commits=['b'*40,'c'*40]),
            lambda d:d.update(manifest=d['manifest']+'  runtime: runtime-v9.9.9\n')]
        for mutate in mutations:
            data=fixture();mutate(data)
            self.assertNotEqual(self.select(data)[0].returncode,0)
        self.assertNotEqual(self.select(arch='unsupported')[0].returncode,0)

    def test_metadata_failure_stops_without_fallback(self):
        for failure in range(1,5):
            result,rows,urls=self.select(failure=failure)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(len(urls),failure)
            self.assertEqual(rows,[])


class DownloadBytesTests(unittest.TestCase):
    def validate(self, *, missing=False, corrupt=False, entry='bin/tool', symlink=False, collision=False,
                 root_entry=None, root_type=tarfile.DIRTYPE, root_mode=0o755):
        text = (ROOT / 'docs/download.md').read_text()
        source = text.split('python3 - "$DOWNLOAD_DIR" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
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
                pass
                assets.append({'name': filename})
                rows.append(f'{filename}\t{target.stat().st_size}\tsha256:{digest}\n')
            (root / 'files.tsv').write_text(''.join(rows))
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
