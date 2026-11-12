"""Stable signatures resolve public modules; Preview fixtures consume their selected SHA."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
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

    def test_preview_fixture_can_directly_import_an_sdk_transitive_dependency(self):
        external = 'example.test/fixture-target'
        (self.connector / 'go.mod').write_text(self.manifest + f'require {external} v1.0.0\n')
        subprocess.run(['git','-c','user.name=test','-c','user.email=test@example.com','commit','-qam','dependency'],
                       cwd=self.connector, check=True)
        self.sha = subprocess.check_output(['git','rev-parse','HEAD'], cwd=self.connector, text=True).strip()
        versions = self.proxy / external / '@v'
        versions.mkdir(parents=True)
        external_mod = 'module ' + external + '\ngo 1.22\n'
        (versions / 'v1.0.0.info').write_text('{"Version":"v1.0.0","Time":"2026-01-01T00:00:00Z"}')
        (versions / 'v1.0.0.mod').write_text(external_mod)
        with zipfile.ZipFile(versions / 'v1.0.0.zip', 'w') as archive:
            archive.writestr(external + '@v1.0.0/go.mod', external_mod)
            archive.writestr(external + '@v1.0.0/external.go', 'package ext\nconst Word = " direct"\n')
        test = self.module / 'main.go'
        test.write_text('package main\nimport ("fmt"; "' + self.path + '"; "' + external +
                        '")\nfunc main(){fmt.Print(connector.Origin + ext.Word)}\n')
        env, result = self.run_client('release-v0.2.0-preview.20261112', 'v0.2.0-preview.20261112')
        self.assertEqual(result, 'checkout direct')
        self.assertIn(external + ' v1.0.0', (self.module / 'go.mod').read_text())
        self.assertNotIn('replace', (self.module / 'go.mod').read_text())
        self.assertIn('/connector v0.2.0 => ', Path(env['GOWORK']).read_text())

    def test_historical_platform_recipe_redirects_only_the_two_selected_go_builds(self):
        platform = self.sources / 'platform'
        output = self.root / 'output'
        output.mkdir()
        work = self.root / 'driver'
        work.mkdir()
        for fixture in ('xproxy', 'xconductor'):
            source = platform / 'test/e2e/platform/helpers' / fixture
            source.mkdir(parents=True)
            (source / 'main.go').write_text('package main\nimport "fmt"\nfunc main(){fmt.Print("' + fixture + '")}\n')
        def prepare(module, owner, sources, selected, version, env):
            self.assertEqual((owner, sources, version), ('orchestrator', self.sources, 'release-v0.1.14'))
            (module / 'go.mod').write_text('module example.test/sdk-fixtures\ngo 1.22\n')
            return dict(env, GOWORK='off')
        env = dict(self.env, KUASAR_GO_RELEASE_VERSION='release-v0.1.14', KUASAR_BUILD_JOBS='2')
        with patch.object(consumer, 'prepare', side_effect=prepare):
            env = consumer.legacy_platform_environment(work, platform, output, {}, env)
        # Simulate the independent workspace the old recipe explicitly exports.
        env['GOWORK'] = str(self.root / 'inapplicable.work')
        for fixture in ('xproxy', 'xconductor'):
            binary = output / ('node-infra-' + fixture)
            args = ['go', '-C', str(self.sources / 'orchestrator'), 'build', '-p', '2', '-trimpath',
                    '-o', str(binary), str(platform / 'test/e2e/platform/helpers' / fixture / 'main.go')]
            subprocess.run(args, cwd=platform, env=env, check=True)
            self.assertEqual(subprocess.check_output([binary], text=True), fixture)
            # An unrecognized build must still use Go's own cwd and workspace.
            result = subprocess.run(args[:-2] + [str(output/'unrelated'), args[-1]], cwd=platform, env=env,
                                    capture_output=True)
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(subprocess.check_output(['go', 'env', 'GOWORK'], cwd=platform, env=env,
                                                text=True).strip(), env['GOWORK'])

    def test_stable_rejects_preview_dependency(self):
        with self.assertRaisesRegex(ValueError, 'selects Preview'):
            self.run_client('release-v0.2.0', 'v0.2.0-preview.20261112')


if __name__ == '__main__':
    unittest.main()
