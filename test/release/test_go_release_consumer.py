"""Stable signatures resolve public modules; Preview fixtures consume their selected SHA."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'release'))
import go_release_consumer as consumer


class ReleaseConsumer(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sources = self.root / 'sources'
        self.connector = self.sources / 'connector'
        self.connector.mkdir(parents=True)
        self.module = self.root / 'client'
        self.module.mkdir()
        self.path = 'github.com/kuasar-sandbox/connector'
        self.manifest = 'module ' + self.path + '\n\ngo 1.22\n'
        (self.connector / 'go.mod').write_text(self.manifest)
        (self.connector / 'connector.go').write_text('package connector\nconst Origin = "checkout"\n')
        def git(*args):
            return subprocess.check_output(['git', *args], cwd=self.connector, text=True).strip()
        git('init', '-q', '--template=')
        git('add', '.')
        git('-c', 'user.name=test', '-c', 'user.email=test@example.com', '-c', 'commit.gpgsign=false',
            'commit', '-qm', 'candidate')
        self.sha = git('rev-parse', 'HEAD')
        (self.module / 'main.go').write_text('package main\nimport ("fmt"; "' + self.path +
                                             '")\nfunc main(){fmt.Print(connector.Origin)}\n')
        self.proxy = self.root / 'proxy'
        self.env = dict(os.environ, GOPROXY=self.proxy.as_uri(), GOSUMDB='off',
                        GOTOOLCHAIN='local', GOFLAGS='', GOMODCACHE=str(self.root / 'mods'),
                        GOENV='off', GOWORK=str(self.root / 'ambient.work'))

    def run_client(self, release, tag):
        env = consumer.prepare(self.module, 'connector', self.sources,
                               {'connector': {'version': tag, 'sha': self.sha}}, release, self.env)
        actual = subprocess.check_output(['go', 'run', '.'], cwd=self.module, env=env, text=True)
        return env, actual

    def test_stable_uses_the_published_module_even_with_a_checkout(self):
        tag = 'v0.1.8'
        versions = self.proxy / self.path / '@v'
        versions.mkdir(parents=True)
        (versions / (tag + '.info')).write_text('{"Version":"v0.1.8","Time":"2026-01-01T00:00:00Z"}')
        (versions / (tag + '.mod')).write_text(self.manifest)
        with zipfile.ZipFile(versions / (tag + '.zip'), 'w') as archive:
            prefix = self.path + '@' + tag + '/'
            archive.writestr(prefix + 'go.mod', self.manifest)
            archive.writestr(prefix + 'connector.go', 'package connector\nconst Origin = "published"\n')
        env, actual = self.run_client('release-v0.1.14', tag)
        self.assertEqual((env['GOWORK'], actual), ('off', 'published'))
        self.assertNotIn('replace', (self.module / 'go.mod').read_text())

    def test_preview_uses_selected_unpublished_source_for_consumer(self):
        env, actual = self.run_client('release-v0.2.0-preview.20261112', 'v0.2.0-preview.20261112')
        self.assertEqual(actual, 'checkout')
        self.assertIn(self.path + ' v0.2.0 => ', Path(env['GOWORK']).read_text())
        self.assertNotIn('preview', (self.module / 'go.mod').read_text())
        self.assertNotIn('replace', (self.module / 'go.mod').read_text())

    def test_stable_rejects_preview_dependency(self):
        with self.assertRaisesRegex(ValueError, 'selects Preview'):
            self.run_client('release-v0.2.0', 'v0.2.0-preview.20261112')


if __name__ == '__main__':
    unittest.main()
