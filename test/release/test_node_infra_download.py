#!/usr/bin/env python3
"""Release-download selection and integrity regressions; no runtime is started."""
import contextlib
import copy
import hashlib
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
loader = importlib.machinery.SourceFileLoader('node_infra_download', str(ROOT / 'node-infra/deploy/bin/node-infra-ctl'))
spec = importlib.util.spec_from_loader(loader.name, loader)
ctl = importlib.util.module_from_spec(spec)
loader.exec_module(ctl)
VERSION = 'release-v0.2.0-preview.20261127'


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / 'download'
        self.bytes = {}
        self.release = {'tag_name': VERSION, 'draft': False, 'prerelease': True, 'assets': []}
        names = [f'node-infra-deploy-{VERSION}.tar.gz'] + [
            f'node-infra-{VERSION}-linux-{arch}.{kind}.tar.gz'
            for arch in ('amd64', 'arm64') for kind in ('docker', 'oci')]
        for number, name in enumerate(names, 1):
            data = ('payload:' + name).encode()
            self.bytes[number] = data
            self.release['assets'].append({'name': name, 'id': number, 'state': 'uploaded',
                                          'size': len(data), 'digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
                                          'browser_download_url': 'https://untrusted.invalid/ignored'})
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request.full_url)
        self.assertEqual(timeout, 60)
        self.assertEqual(request.get_header('X-github-api-version'), '2022-11-28')
        if request.full_url == ctl.GITHUB + '/releases/tags/' + VERSION:
            self.assertEqual(request.get_header('Accept'), 'application/vnd.github+json')
            return io.BytesIO(json.dumps(self.release).encode())
        self.assertEqual(request.get_header('Accept'), 'application/octet-stream')
        self.assertTrue(request.full_url.startswith(ctl.GITHUB + '/releases/assets/'))
        return io.BytesIO(self.bytes[int(request.full_url.rsplit('/', 1)[1])])

    def download(self, arch='x86_64', kind='none'):
        with patch.object(ctl, 'urlopen', side_effect=self.open):
            return ctl.download_release(VERSION, self.output, arch, kind)

    def test_default_downloads_only_deployment_package_without_root_or_runtime(self):
        with patch.object(ctl.sys, 'argv', ['node-infra-ctl', 'download', '--release', VERSION,
                                         '--output', str(self.output)]), \
             patch.object(ctl.os, 'geteuid', return_value=1000), \
             patch.object(ctl.platform, 'machine', return_value='x86_64'), \
             patch.object(ctl, 'urlopen', side_effect=self.open), \
             patch.object(ctl, 'run', side_effect=AssertionError('must not invoke a runtime')), \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(ctl.main(), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result['architecture'], 'amd64')
        self.assertEqual(len(result['assets']), 1)
        self.assertEqual(result['image_reference'], 'ghcr.io/kuasar-sandbox/node-infra:' + VERSION)
        self.assertEqual(len(self.requests), 2)
        receipt = json.loads((self.output / 'download.json').read_text())
        self.assertEqual(receipt['assets'][0]['name'], f'node-infra-deploy-{VERSION}.tar.gz')
        self.assertFalse(any(self.output.glob('*.part')))

    def test_exact_architecture_and_format_selection(self):
        for arch in ('amd64', 'x86_64', 'arm64', 'aarch64'):
            for kind in ('docker', 'oci'):
                with self.subTest(arch=arch, kind=kind):
                    self.output = Path(self.temporary.name) / (arch + '-' + kind)
                    result = self.download(arch, kind)
                    native = 'amd64' if arch in ('amd64', 'x86_64') else 'arm64'
                    self.assertEqual(result['assets'][1]['name'], f'node-infra-{VERSION}-linux-{native}.{kind}.tar.gz')
                    self.assertEqual(len(result['assets']), 2)

    def test_rejects_wrong_release_channel_and_asset_metadata_before_creating_output(self):
        original = copy.deepcopy(self.release)
        for change in ('draft', 'tag', 'prerelease', 'missing', 'duplicate', 'digest', 'size', 'asset_id'):
            with self.subTest(change=change):
                self.release = copy.deepcopy(original)
                if change == 'draft': self.release['draft'] = True
                elif change == 'tag': self.release['tag_name'] = 'release-v0.2.0'
                elif change == 'prerelease': self.release['prerelease'] = False
                elif change == 'missing': self.release['assets'].pop(0)
                elif change == 'duplicate': self.release['assets'].append(self.release['assets'][0].copy())
                elif change == 'digest': self.release['assets'][0]['digest'] = None
                elif change == 'size': self.release['assets'][0]['size'] = True
                elif change == 'asset_id': self.release['assets'][0]['id'] = '../other'
                with self.assertRaises(ValueError): self.download()
                self.assertFalse(self.output.exists())

    def test_invalid_selection_makes_no_network_request(self):
        with patch.object(ctl, 'urlopen', side_effect=AssertionError('must not call GitHub')):
            for version, arch, kind in [('latest', 'amd64', 'none'), ('release-v1.2.3/../x', 'amd64', 'none'),
                                        (VERSION, 'riscv64', 'none'), (VERSION, 'amd64', 'unknown')]:
                with self.subTest(version=version, arch=arch, kind=kind), self.assertRaises(ValueError):
                    ctl.download_release(version, self.output, arch, kind)
        self.assertFalse(self.output.exists())

    def test_existing_directory_and_link_preserve_user_files(self):
        self.output.mkdir()
        keep = self.output / 'keep'
        keep.write_text('user data')
        with self.assertRaisesRegex(ValueError, 'new output directory'): self.download()
        self.assertEqual(keep.read_text(), 'user data')
        linked = self.output.with_name('link')
        linked.symlink_to(self.output, target_is_directory=True)
        self.output = linked
        with self.assertRaisesRegex(ValueError, 'new output directory'): self.download()
        self.assertEqual(keep.read_text(), 'user data')

    def test_mismatch_never_publishes_corrupt_asset_or_success_receipt(self):
        original = self.bytes[1]
        for data in (original[:-1], original + b'extra', b'x' * len(original)):
            with self.subTest(size=len(data)):
                self.output = Path(self.temporary.name) / ('bad-' + str(len(data)))
                self.bytes[1] = data
                with self.assertRaises(ValueError): self.download()
                self.assertFalse((self.output / 'download.json').exists())
                self.assertEqual(list(self.output.iterdir()), [])

    def test_failure_after_verified_asset_keeps_it_without_success_receipt(self):
        self.bytes[2] = b'bad image'
        with self.assertRaises(ValueError): self.download(kind='docker')
        self.assertEqual((self.output / self.release['assets'][0]['name']).read_bytes(), self.bytes[1])
        self.assertFalse((self.output / 'download.json').exists())
        self.assertFalse(any(self.output.glob('*.part')))

    def test_new_options_do_not_change_host_operations(self):
        with patch.object(ctl.sys, 'argv', ['node-infra-ctl', 'status', '--name', 'node-a', '--release', VERSION]), \
             patch.object(ctl.os, 'geteuid', return_value=0), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(ctl.main(), 1)
        self.assertIn('options require download', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
