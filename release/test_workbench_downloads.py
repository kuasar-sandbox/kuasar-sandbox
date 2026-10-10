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

# Original release-v0.1.5 manifest; retain its historical test provenance verbatim.
LEGACY_STABLE_MANIFEST = b'''version: release-v0.1.5
previous_version: release-v0.1.4
components:
  accelerator: v0.1.5
  connector: v0.1.4
  sandboxer: v0.1.5
  orchestrator: v0.1.5
  runtime: runtime-v0.1.5
  vmlinux: vmlinux-v0.1.3
test_revisions:
  accelerator: 1069c605f2f3d116c1873eb3f2179810e1e8a341
  connector: f7f11917096f7a40e6ad1601179ff53429052c92
  guest-runtime: 745ba89c776eed7b9d404a8e3fc19cb60022518b
  sandboxer: a48c688ece56a4e9d02620f320f8ee98bc60885c
  orchestrator: 9f7e54f6169f0070ecc864486764023127f55e72
'''


class ManifestRetrievalTests(unittest.TestCase):
    def retrieve(self, guide, *, tag=TAG, requested='', source='a' * 40,
                 fail_manifest=False, mutate=None, legacy=False, annotated=False, metadata_failure=None):
        text = (ROOT / 'docs' / guide).read_text()
        block = re.findall(r'```bash\n(.*?)\n```', text, re.S)[0]
        entrance = block.split('\nRELEASE_VERSION="$(python3 - ', 1)[0]
        content = f'version: {TAG}\ndelivery: workbench-v1\n# original\n'.encode()
        root_sha, releases_sha = 'b' * 40, 'c' * 40
        if legacy == 'stable':
            tag = 'release-v0.1.5'
            source = 'bd773ff2f7d69266cad6840ad1ab69f78f00f7e0'
            root_sha = '947eacd8a6cbe7f6a215a7773aa1956a6ab65a6b'
            releases_sha = 'f29c1d277cc9fa78c58342934575b23d80ef4433'
            content = LEGACY_STABLE_MANIFEST
        elif legacy:
            tag = 'release-v0.1.6-preview.20261010.1'
            source = '210fed3a1f7dd5899e09cda562c0bb66520dee5e'
            root_sha = 'b7dcb7b85147c9d09d21b09cb3ea06605649b7fa'
            releases_sha = '553d65de83b4b9db694a722c5e4deef82573126d'
            content = (ROOT / 'ci/integration/fixtures/pre-cutover-preview.yaml').read_bytes()
        blob_sha = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
        if legacy == 'stable':
            self.assertEqual((len(content), blob_sha), (501, 'b1677c6bcdda6c17d980738cf2299162cd144699'))
        elif legacy:
            self.assertEqual((len(content), blob_sha), (717, 'cc34e1f07745778e36e28f182bd71c012f2c52f7'))
        manifest = 'daily-preview.yaml' if '-preview.' in str(tag) else 'release.yaml'
        api = 'https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox'
        raw = f'https://raw.githubusercontent.com/kuasar-sandbox/kuasar-sandbox/refs/tags/{tag}/releases/{manifest}'
        ref = {'ref': 'refs/tags/' + str(tag), 'object': {'type': 'commit', 'sha': source}}
        fixture = {
            'metadata': {'tag_name': tag, 'target_commitish': source},
            'refs': [ref, json.loads(json.dumps(ref))],
            'commit': {'sha': source, 'commit': {'tree': {'sha': root_sha}}},
            'root': {'sha': root_sha, 'truncated': False, 'tree': [
                {'path': 'releases', 'type': 'tree', 'mode': '040000', 'sha': releases_sha}]},
            'releases': {'sha': releases_sha, 'truncated': False, 'tree': [
                {'path': manifest, 'type': 'blob', 'mode': '100644', 'sha': blob_sha, 'size': len(content)}]},
            'content': content.decode(), 'fail_manifest': fail_manifest, 'metadata_failure': metadata_failure,
            'api': api, 'tag': tag, 'root_sha': root_sha, 'releases_sha': releases_sha, 'raw_url': raw,
            'annotated': {'sha': 'd' * 40, 'object': {'type': 'commit', 'sha': source}},
        }
        if annotated:
            for value in fixture['refs']:
                value['object'] = {'type': 'tag', 'sha': 'd' * 40}
        if mutate:
            mutate(fixture)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'fixture.json').write_text(json.dumps(fixture))
            # Both curl and Python HTTPS calls use one deterministic transport.
            # The real documented Bash/Python entry is executed in a fresh process.
            (root / 'transport.py').write_text('''import io, json, os, pathlib, sys
from urllib.error import HTTPError
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
def read_url(url):
    fixture = json.loads((root / 'fixture.json').read_text())
    requests = root / 'requests.jsonl'
    prior = [json.loads(line) for line in requests.read_text().splitlines()] if requests.exists() else []
    with requests.open('a') as stream:
        stream.write(json.dumps(url) + '\\n')
    api, tag = fixture['api'], str(fixture['tag'])
    failure = fixture['metadata_failure']
    if failure and url == api + failure['path'] and prior.count(url) + 1 == failure['occurrence']:
        if failure['kind'] == 'timeout':
            raise TimeoutError('metadata timeout fixture')
        raise HTTPError(url, 503, 'metadata unavailable fixture', {}, None)
    if url.startswith(api + '/releases/'):
        value = fixture['metadata']
    elif url == api + '/git/ref/tags/' + tag:
        value = fixture['refs'][min(prior.count(url), 1)]
    elif url == api + '/commits/refs/tags/' + tag:
        value = fixture['commit']
    elif url == api + '/git/tags/' + 'd' * 40:
        value = fixture['annotated']
    elif url == api + '/git/trees/' + fixture['root_sha']:
        value = fixture['root']
    elif url == api + '/git/trees/' + fixture['releases_sha']:
        value = fixture['releases']
    elif url.startswith('https://raw.githubusercontent.com/'):
        if fixture['fail_manifest']:
            raise HTTPError(url, 404, 'missing named-tag manifest', {}, None)
        return fixture['content'].encode()
    else:
        raise AssertionError('unexpected request: ' + url)
    return json.dumps(value).encode()
def urlopen(request, timeout):
    assert timeout > 0
    return io.BytesIO(read_url(request.full_url))
if __name__ == '__main__':
    try:
        sys.stdout.buffer.write(read_url(sys.argv[-1]))
    except HTTPError:
        raise SystemExit(22)
''')
            (root / 'python.py').write_text('''import sys, urllib.request
import transport
urllib.request.urlopen = transport.urlopen
args = sys.argv[1:]
if args[0] == '-c':
    code = args[1]
    sys.argv = ['-c'] + args[2:]
else:
    assert args[0] == '-'
    code = sys.stdin.read()
    sys.argv = args
exec(compile(code, '<documented download>', 'exec'))
''')
            result = subprocess.run(['bash', '-c',
                'curl() { command python3 -B "$FIXTURE_ROOT/transport.py" "$@"; }\n'
                'python3() { command python3 -B "$FIXTURE_ROOT/python.py" "$@"; }\n' +
                entrance + '\ncat "$SELECTION_MANIFEST"\n'
                'if [ -n "${SOURCE_EVIDENCE:-}" ]; then cp "$SOURCE_EVIDENCE" "$FIXTURE_ROOT/evidence.json"; fi\n'],
                cwd=root, env=os.environ | {'RELEASE_VERSION': requested,
                    'FIXTURE_ROOT': str(root), 'TMPDIR': str(root), 'PYTHONDONTWRITEBYTECODE': '1'},
                capture_output=True, text=True, timeout=15)
            requests = root / 'requests.jsonl'
            urls = [json.loads(line) for line in requests.read_text().splitlines()] if requests.exists() else []
            # Applies to every positive and rejected fixture, including transport failures.
            for url in urls:
                if 'raw.githubusercontent.com' in url:
                    self.assertIn(url, (raw, raw.replace('/refs/tags/', '/')))
                if 'api.github.com' in url:
                    self.assertNotIn('/contents/', url)
                    self.assertNotIn('/git/blobs/', url)
            evidence = root / 'evidence.json'
            if result.returncode == 0:
                self.assertTrue(evidence.is_file(), 'successful admission must retain its evidence')
                self.assertEqual(result.stdout, content.decode())
                proof = json.loads(evidence.read_text())
                self.assertEqual(proof['source_commit'], source)
                self.assertEqual(proof['manifest_blob'], blob_sha)
                self.assertEqual(proof['manifest_size'], len(content))
                self.assertEqual(proof['tag'], tag)
                self.assertEqual(proof['manifest_path'], 'releases/' + manifest)
                # Retain the actual proof chain, including both observations and
                # annotation objects, in exactly the order it was validated.
                ref_url = api + '/git/ref/tags/' + tag
                observations = [{'url': ref_url, 'response': fixture['refs'][0]}]
                annotation = {'url': api + '/git/tags/' + 'd' * 40, 'response': fixture['annotated']}
                if annotated: observations.append(annotation)
                observations.extend([
                    {'url': api + '/commits/refs/tags/' + tag, 'response': fixture['commit']},
                    {'url': api + '/git/trees/' + root_sha, 'response': fixture['root']},
                    {'url': api + '/git/trees/' + releases_sha, 'response': fixture['releases']},
                    {'url': ref_url, 'response': fixture['refs'][1]},
                ])
                if annotated: observations.append(annotation)
                self.assertEqual(proof['metadata'], observations)
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
                    endpoint = 'tags/' + requested if requested else 'latest'
                    self.assertEqual(urls[0], 'https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox/releases/' + endpoint)
                    self.assertEqual(urls.count(urls[0]), 1)
                    self.assertEqual([u for u in urls if 'raw.githubusercontent.com' in u], [
                        f'https://raw.githubusercontent.com/kuasar-sandbox/kuasar-sandbox/refs/tags/{tag}/releases/{manifest}'])
                    self.assertEqual(sum('/git/ref/tags/' in u for u in urls), 2)

    def test_authentic_legacy_manifest_and_annotated_tags_remain_readable(self):
        for guide in ('download.md', 'download_zh.md'):
            for change in ({'legacy': True}, {'legacy': 'stable'}, {'annotated': True}):
                with self.subTest(guide=guide, change=change):
                    result, urls = self.retrieve(guide, **change)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if change.get('legacy'):
                        self.assertIn('test_revisions:', result.stdout)
                        expected = (LEGACY_STABLE_MANIFEST.decode() if change['legacy'] == 'stable' else
                                    (ROOT / 'ci/integration/fixtures/pre-cutover-preview.yaml').read_text())
                        self.assertEqual(result.stdout, expected)

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

    def test_repointed_tag_before_or_after_content_is_rejected(self):
        for guide in ('download.md', 'download_zh.md'):
            for index in (0, 1):
                with self.subTest(guide=guide, index=index):
                    def mutate(f):
                        f['refs'][index]['object']['sha'] = 'e' * 40
                    result, urls = self.retrieve(guide, mutate=mutate)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('release tag moved', result.stderr)
                    self.assertEqual(sum('raw.githubusercontent.com' in u for u in urls), index)

    def test_wrong_blob_fails_even_when_both_tag_reads_match_original(self):
        for guide in ('download.md', 'download_zh.md'):
            def mutate(f):
                # Same version/delivery and byte count; an A->B->A or stale raw response.
                f['content'] = f['content'].replace('# original', '# tampered')
            result, urls = self.retrieve(guide, mutate=mutate)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('manifest blob differs', result.stderr)
            self.assertEqual(sum('/git/ref/tags/' in u for u in urls), 2)

    def test_missing_ambiguous_malformed_or_mismatched_metadata_is_rejected(self):
        changes = {
            'missing ref': lambda f: f['refs'][0].pop('ref'),
            'wrong ref': lambda f: f['refs'][0].update(ref='refs/heads/' + TAG),
            'wrong ref type': lambda f: f['refs'][0]['object'].update(type='blob'),
            'wrong commit': lambda f: f['commit'].update(sha='e' * 40),
            'missing commit tree': lambda f: f['commit']['commit'].pop('tree'),
            'invalid tree id': lambda f: f['commit']['commit']['tree'].update(sha='main'),
            'wrong root identity': lambda f: f['root'].update(sha='e' * 40),
            'truncated root': lambda f: f['root'].update(truncated=True),
            'missing root entries': lambda f: f['root'].pop('tree'),
            'missing releases': lambda f: f['root'].update(tree=[]),
            'duplicate releases': lambda f: f['root']['tree'].append(f['root']['tree'][0].copy()),
            'wrong directory mode': lambda f: f['root']['tree'][0].update(mode='100644'),
            'wrong child identity': lambda f: f['releases'].update(sha='e' * 40),
            'truncated child': lambda f: f['releases'].update(truncated=True),
            'missing truncation field': lambda f: f['releases'].pop('truncated'),
            'missing manifest': lambda f: f['releases'].update(tree=[]),
            'duplicate manifest': lambda f: f['releases']['tree'].append(f['releases']['tree'][0].copy()),
            'symlink manifest': lambda f: f['releases']['tree'][0].update(mode='120000'),
            'tree manifest': lambda f: f['releases']['tree'][0].update(type='tree', mode='040000'),
            'invalid blob id': lambda f: f['releases']['tree'][0].update(sha='invalid'),
            'wrong blob id': lambda f: f['releases']['tree'][0].update(sha='e' * 40),
            'wrong size': lambda f: f['releases']['tree'][0].update(size=1),
            'boolean size': lambda f: f['releases']['tree'][0].update(size=True),
        }
        for guide in ('download.md', 'download_zh.md'):
            for name, mutate in changes.items():
                with self.subTest(guide=guide, change=name):
                    result, urls = self.retrieve(guide, mutate=mutate)
                    self.assertNotEqual(result.returncode, 0, urls)

    def test_metadata_transport_failures_stop_at_the_failed_request(self):
        api = 'https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox'
        ref = '/git/ref/tags/' + TAG
        annotation = '/git/tags/' + 'd' * 40
        root = '/git/trees/' + 'b' * 40
        child = '/git/trees/' + 'c' * 40
        commit = '/commits/refs/tags/' + TAG
        raw = f'https://raw.githubusercontent.com/kuasar-sandbox/kuasar-sandbox/refs/tags/{TAG}/releases/release.yaml'
        for guide in ('download.md', 'download_zh.md'):
            for annotated in (False, True):
                paths = [ref] + ([annotation] if annotated else []) + [commit, root, child]
                urls = [api + '/releases/latest', *(api + path for path in paths), raw, api + ref]
                if annotated: urls.append(api + annotation)
                for index, url in enumerate(urls):
                    if index == 0 or url == raw:
                        continue
                    # Fail both repeated ref/annotation reads, plus a timeout at
                    # each metadata boundary. Earlier raw bytes are never admitted.
                    for kind in ('http', 'timeout'):
                        with self.subTest(guide=guide, annotated=annotated, index=index, kind=kind):
                            failure = {'path': url.removeprefix(api), 'kind': kind,
                                       'occurrence': urls[:index + 1].count(url)}
                            result, requests = self.retrieve(guide, annotated=annotated, metadata_failure=failure)
                            self.assertNotEqual(result.returncode, 0)
                            self.assertIn('metadata ' + ('timeout' if kind == 'timeout' else 'unavailable') + ' fixture', result.stderr)
                            self.assertEqual(requests, urls[:index + 1])
                            self.assertEqual(result.stdout, '')

    def test_failed_tag_download_has_no_sha_or_main_fallback(self):
        for guide in ('download.md', 'download_zh.md'):
            result, urls = self.retrieve(guide, fail_manifest=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual([u for u in urls if 'raw.githubusercontent.com' in u], [
                f'https://raw.githubusercontent.com/kuasar-sandbox/kuasar-sandbox/refs/tags/{TAG}/releases/release.yaml'])


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
        sources = [(ROOT / 'docs' / name).read_text().split('RELEASE_VERSION="$(python3 - ', 1)[1].split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
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
