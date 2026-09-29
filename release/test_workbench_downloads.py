"""Execute the public quickstart's asset selection with release metadata fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TAG = 'release-v1.2.3'


class DownloadTests(unittest.TestCase):
    def select(self, *, contract='workbench-v1', omit=(), extra=(), download=False, missing_binding=False):
        names = ['SHA256SUMS', f'platform-{TAG}.tar.gz']
        for arch in ('x86_64', 'aarch64'):
            names += [f'{unit}-v1.2.3-linux-{arch}.tar.gz' for unit in ('accelerator', 'connector', 'orchestrator', 'sandboxer')]
            names += [f'sandbox-runtime-{arch}-v1.2.3.tar.gz', f'vmlinux-{arch}-v1.2.3.tar.gz']
            if contract == 'workbench-v1':
                names += [f'workbench-{arch}-v1.2.3.tar.gz']
        names = [name for name in names if name not in omit] + list(extra)
        metadata = {'tag_name': TAG, 'draft': False, 'prerelease': False,
                    'body': '<!-- kuasar-integration-validation ' + json.dumps({'delivery': contract}) + ' -->' if contract != 'historical' else '',
                    'assets': [{'name': name, 'size': 100, 'digest': 'sha256:' + 'a'*64,
                        'browser_download_url': f'https://github.com/kuasar-sandbox/kuasar-sandbox/releases/download/{TAG}/{name}'} for name in names]}
        if missing_binding:
            metadata['body'] = ''
        sources = [(ROOT / 'docs' / name).read_text().split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
                   for name in ('quickstart.md', 'quickstart_zh.md')]
        self.assertEqual(*sources)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'release.json').write_text(json.dumps(metadata))
            (root / 'selection.yaml').write_text('version: ' + TAG + '\n' +
                ('delivery: ' + contract + '\n' if contract != 'historical' else ''))
            result = subprocess.run([sys.executable, '-c', sources[0], str(root / 'release.json'), TAG, str(root / 'assets.tsv'), str(root / 'selection.yaml')],
                                    env=os.environ | {'DOWNLOAD_WORKBENCH': str(int(download))}, capture_output=True, text=True)
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

    def test_historical_contract_stays_readable(self):
        code, rows = self.select(contract='historical')
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 8)


if __name__ == '__main__':
    unittest.main()
