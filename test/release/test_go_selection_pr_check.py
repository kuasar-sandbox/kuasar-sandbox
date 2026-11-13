"""Compile the selected PR merge against real, immutable Git refs without network."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'release'))
spec = importlib.util.spec_from_file_location('go_selection_pr_check', ROOT / 'release/check-go-selection-pr.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def sign(inputs):
    data = [inputs['consumer'], int(inputs['pr_number']), inputs['head_sha'], inputs['base_sha'],
            inputs['version'], json.loads(inputs['dependencies'])]
    raw = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
    return {**inputs, 'binding': hashlib.sha256(raw).hexdigest()}


class GoSelectionPR(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        remotes = self.root / 'remotes'
        remotes.mkdir()
        self.env = patch.dict(os.environ, {
            'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'url.' + remotes.as_uri() + '/.insteadOf',
            'GIT_CONFIG_VALUE_0': 'https://github.com/kuasar-sandbox/',
            'GIT_AUTHOR_NAME': 'CI', 'GIT_AUTHOR_EMAIL': 'ci@example.org',
            'GIT_COMMITTER_NAME': 'CI', 'GIT_COMMITTER_EMAIL': 'ci@example.org',
            'GOPROXY': 'off', 'GOSUMDB': 'off', 'GOWORK': 'off', 'GOFLAGS': '', 'GOTOOLCHAIN': 'local'})
        self.env.start()
        self.addCleanup(self.env.stop)
        def repo(name):
            path = remotes / (name + '.git')
            path.mkdir()
            git(path, 'init', '-q', '-b', 'main')
            git(path, 'config', 'commit.gpgsign', 'false')
            (path / 'go.mod').write_text(f'module github.com/kuasar-sandbox/{name}\ngo 1.22\n')
            return path
        accel = self.accel = repo('accelerator')
        (accel / 'go.go').write_text('package accelerator\nconst Word = "fixed"\n')
        git(accel, 'add', '.'); git(accel, 'commit', '-qm', 'dependency')
        self.dep = git(accel, 'rev-parse', 'HEAD')
        (accel / 'go.go').write_text('package accelerator\nconst Word = "moving-head"\n')
        git(accel, 'commit', '-qam', 'later')
        runtime = self.runtime = repo('guest-runtime')
        git(runtime, 'add', '.'); git(runtime, 'commit', '-qm', 'base')
        self.base = git(runtime, 'rev-parse', 'HEAD')
        git(runtime, 'switch', '-qc', 'pr')
        (runtime / 'go.mod').write_text((runtime / 'go.mod').read_text() + 'require github.com/kuasar-sandbox/accelerator v0.2.0\n')
        cmd = runtime / 'cmd/flatten-ctl'
        cmd.mkdir(parents=True)
        (cmd / 'main.go').write_text('package main\nimport ("fmt"; "github.com/kuasar-sandbox/accelerator")\n'
                                    'func main(){fmt.Print(accelerator.Word)}\n')
        init = runtime / 'cmd/sandbox-init'
        init.mkdir(parents=True)
        (init / 'main.go').write_text('package main\nimport "fmt"\nfunc main(){fmt.Print("selected-init")}\n')
        git(runtime, 'add', '.'); git(runtime, 'commit', '-qm', 'selected PR')
        self.head = git(runtime, 'rev-parse', 'HEAD')
        git(runtime, 'switch', '-q', 'main'); git(runtime, 'merge', '--no-ff', '-qm', 'candidate', 'pr')
        self.merge = git(runtime, 'rev-parse', 'HEAD')
        git(runtime, 'update-ref', 'refs/pull/77/merge', self.merge)
        self.deps = {'accelerator': {'tag': 'v0.2.0-preview.20261112', 'sha': self.dep}}
        self.inputs = {'consumer':'runtime', 'head_sha':self.head, 'base_sha':self.base, 'pr_number':'77',
                       'version':'runtime-v0.2.0-preview.20261112', 'dependencies':json.dumps(self.deps)}
        self.sources = self.root / 'sources'
        self.sources.mkdir()

    def test_actual_merge_uses_frozen_upstream_without_creating_a_tag(self):
        check.check(sign(self.inputs), self.sources)
        self.assertEqual(git(self.sources/'guest-runtime', 'rev-parse', 'HEAD'), self.merge)
        self.assertEqual(git(self.sources/'accelerator', 'rev-parse', 'HEAD'), self.dep)
        self.assertNotEqual(git(self.accel, 'rev-parse', 'HEAD'), self.dep)
        self.assertEqual(git(self.accel, 'tag'), '')
        self.assertEqual(subprocess.check_output([self.sources/'compiled'], text=True), 'fixed')
        self.assertEqual(subprocess.check_output([self.sources/'compiled-init'], text=True), 'selected-init')

    def test_runtime_dependency_uses_unit_identity_and_owner_directory(self):
        for tag in ('runtime-v0.2.0', 'runtime-v0.2.0-preview.20261112'):
            with self.subTest(tag=tag):
                deps = {'runtime': {'tag': tag, 'sha': self.dep}}
                inputs = sign({**self.inputs, 'consumer': 'sandboxer',
                               'version': 'v0.2.0-preview.20261112', 'dependencies': json.dumps(deps)})
                parents = f'parent {self.base}\nparent {self.head}\n\nselected merge'
                with (patch.object(check.go_workspace, 'closure', return_value=['sandboxer', 'guest-runtime']),
                      patch.object(check, 'checkout') as checkout,
                      patch.object(check.tag_sources, 'git', return_value=parents),
                      patch.object(check.tag_sources, 'fetch_unit') as fetch,
                      patch.object(check.go_workspace, 'configure_bound_release', return_value={}) as bound,
                      patch.object(check.subprocess, 'run')):
                    check.check(inputs, self.sources)
                bound.assert_called_once_with(self.sources, 'sandboxer', inputs['version'],
                                              {'guest-runtime': deps['runtime']})
                if '-preview.' in tag:
                    checkout.assert_any_call(self.sources / 'guest-runtime', 'runtime', self.dep)
                    fetch.assert_not_called()
                else:
                    fetch.assert_called_once_with(self.sources / 'guest-runtime', 'runtime', tag, self.dep)

    def test_dependency_owner_key_cannot_replace_release_unit_key(self):
        deps = {'guest-runtime': {'tag': 'runtime-v0.2.0', 'sha': self.dep}}
        inputs = sign({**self.inputs, 'consumer': 'sandboxer', 'dependencies': json.dumps(deps)})
        with (patch.object(check.go_workspace, 'closure', return_value=['sandboxer', 'guest-runtime']),
              patch.object(check, 'checkout') as checkout,
              self.assertRaisesRegex(ValueError, 'closure')):
            check.check(inputs, self.sources)
        checkout.assert_not_called()

    def test_invalid_input_or_binding_stops_before_checkout(self):
        inputs = sign(self.inputs)
        for changed, message in [({'head_sha': 'deadbeef'}, 'identity'),
                                 ({'head_sha': self.dep}, 'digest'), ({'consumer': 'vmlinux'}, 'consumer'),
                                 ({'dependencies': '{}'}, 'closure'), ({'version': 'runtime-v0.2.0'}, 'Preview')]:
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, message):
                check.check({**inputs, **changed}, self.sources)
            self.assertFalse(list(self.sources.iterdir()))

    def test_swapped_merge_parents_and_moved_release_tags_fail(self):
        with self.assertRaisesRegex(ValueError, 'moved'):
            check.check(sign({**self.inputs, 'head_sha':self.base, 'base_sha':self.head}), self.sources)
        self.assertFalse((self.sources/'accelerator').exists())
        next_sources = self.root / 'wrong-tag'
        next_sources.mkdir()
        git(self.accel, 'tag', 'v0.2.0')  # This tag points to moving-head, not the frozen SHA.
        version = json.dumps({'accelerator': {'tag':'v0.2.0', 'sha':self.dep}})
        with self.assertRaisesRegex(ValueError, 'moved|differs'):
            check.check(sign({**self.inputs, 'dependencies':version}), next_sources)
        self.assertFalse((next_sources/'compiled').exists())


if __name__ == '__main__':
    unittest.main()
