#!/usr/bin/env python3
"""Exercise aggregate Go declarations and publication channels without a network."""
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import go_module_policy as policy
import go_workspace as workspace


class PolicyTests(unittest.TestCase):
    def test_host_closure_follows_only_the_selected_source_declaration(self):
        owner = self.root / 'sandboxer'
        owner.mkdir()
        module = owner / 'go.mod'
        module.write_text('module '+policy.PREFIX+'sandboxer\ngo 1.22\n')
        self.assertNotIn('guest-runtime', workspace.closure(['sandboxer'], root=self.root))
        with module.open('a') as output:
            output.write('require '+policy.PREFIX+'guest-runtime v0.2.0\n')
        self.assertIn('guest-runtime', workspace.closure(['sandboxer'], root=self.root))

    def test_owner_selection_distinguishes_runtime_from_kernel(self):
        runtime = {'version': 'runtime-v0.2.0', 'sha': 'a' * 40}
        kernel = {'version': 'vmlinux-v0.1.6', 'sha': 'b' * 40}
        self.assertEqual(policy.owner_selection({'runtime': runtime, 'vmlinux': kernel}),
                         {'guest-runtime': runtime})
        self.assertEqual(policy.OWNER_UNITS['guest-runtime'], 'runtime')

    def test_bound_runtime_dependency_validates_its_unit_tag(self):
        owner = self.root / 'sandboxer'
        owner.mkdir()
        (owner / 'go.mod').write_text('module ' + policy.PREFIX + 'sandboxer\n')
        bindings = {'guest-runtime': {'tag': 'runtime-v0.2.0', 'sha': self.sha}}
        def document(go, args, env):
            return {'Module': {'Path': policy.PREFIX + Path(args[1]).name}}
        with (patch.object(workspace, 'closure', return_value=['sandboxer', 'guest-runtime']),
              patch.object(workspace, 'git_output', return_value=self.sha),
              patch.object(workspace, 'require_clean_dependency'),
              patch.object(workspace, 'go_json', side_effect=document),
              patch.object(policy, 'declaration_changes', return_value={}),
              patch.object(workspace, 'validate_build_list') as validate):
            env = workspace.configure_bound_release(self.root, 'sandboxer', 'v0.2.0', bindings, self.env)
        self.assertEqual(env['GOWORK'], 'off')
        self.assertEqual(validate.call_args.args[1:3], ({policy.PREFIX + 'guest-runtime': 'v0.2.0'}, {}))

    def test_declarations_apply_only_to_selected_go_modules(self):
        tags = {'accelerator': 'v0.1.12', 'connector': 'v0.2.0-preview.20261112.2',
                'runtime': 'runtime-v0.2.1-preview.20261112', 'vmlinux': 'vmlinux-v0.1.6'}
        expected = policy.expected_versions(tags, {'accelerator': 'v0.2.0'})
        self.assertEqual(expected[policy.PREFIX + 'accelerator'], 'v0.1.12')
        self.assertEqual(expected[policy.PREFIX + 'connector'], 'v0.2.0')
        self.assertEqual(expected[policy.PREFIX + 'guest-runtime'], 'v0.2.1')
        original = ('module example.test/client\n\ngo 1.22\n\nrequire (\n'
                    '\tgithub.com/kuasar-sandbox/accelerator v0.1.10\n'
                    '\tgithub.com/kuasar-sandbox/connector v0.2.0-preview.20261110 // indirect\n'
                    '\tgithub.com/kuasar-sandbox/go-erofs v0.0.0-20261014160540-827da6f90114\n)\n')
        self.assertEqual(set(policy.declaration_changes(original, expected)),
                         {policy.PREFIX + 'accelerator', policy.PREFIX + 'connector'})
        fixed = policy.align_declarations(original, expected)
        self.assertIn('/connector v0.2.0 // indirect', fixed)
        self.assertIn('/go-erofs v0.0.0-20261014160540-827da6f90114', fixed)
        self.assertNotIn('guest-runtime', fixed)
        self.assertEqual(policy.align_declarations(fixed, expected), fixed)
        for directive in ('replace github.com/kuasar-sandbox/connector => ../connector',
                          'exclude github.com/kuasar-sandbox/connector v0.2.0'):
            with self.assertRaises(ValueError):
                policy.declaration_changes(fixed + directive + '\n', expected)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.env = dict(os.environ, GOPROXY='off', GOSUMDB='off', GOWORK='off',
                        GOMODCACHE=str(self.root / 'modcache'))
        self.dep = self.root / 'accelerator'
        self.app = self.root / 'guest-runtime'
        self.dep.mkdir(); self.app.mkdir()
        (self.dep / 'go.mod').write_text('module ' + policy.PREFIX + 'accelerator\ngo 1.22\n')
        (self.dep / 'dep.go').write_text('package accelerator\nconst V = 1\n')
        (self.app / 'go.mod').write_text('module ' + policy.PREFIX + 'guest-runtime\ngo 1.22\n'
                                         'require ' + policy.PREFIX + 'accelerator v0.2.0\n')
        (self.app / 'app.go').write_text('package app\nimport "' + policy.PREFIX + 'accelerator"\nconst V = accelerator.V\n')
        for args in (['init','-q'], ['add','.'], ['-c','user.name=CI','-c','user.email=ci@example.org','commit','-qm','source']):
            subprocess.run(['git', *args], cwd=self.dep, check=True)
        self.sha = subprocess.check_output(['git','rev-parse','HEAD'], cwd=self.dep, text=True).strip()

    def bound(self, tag, version, *, sha=None, env=None):
        return workspace.configure_bound_release(self.root, 'guest-runtime', version,
                        {'accelerator': {'tag': tag, 'sha': sha or self.sha}}, env or self.env)

    def test_unpublished_version_is_bound_only_in_preview(self):
        tag = 'v0.2.0-preview.20261112'
        version = 'runtime-' + tag
        env = self.bound(tag, version)
        doc = json.loads(subprocess.check_output(['go','work','edit','-json',env['GOWORK']], env=env))
        self.assertEqual([r['DiskPath'] for r in doc['Use']], [str(self.app)])
        self.assertEqual(len(doc['Replace']), 1)
        row = doc['Replace'][0]
        self.assertEqual(row['Old'], {'Path': policy.PREFIX + 'accelerator', 'Version': 'v0.2.0'})
        self.assertFalse(Path(row['New']['Path']).is_absolute())
        self.assertEqual((Path(env['GOWORK']).parent / row['New']['Path']).resolve(), self.dep)
        for command in (['go','build','.'], ['go','list','-m','all']):
            subprocess.run(command, cwd=self.app, env=env, stdout=subprocess.DEVNULL, check=True)
        with self.assertRaisesRegex(ValueError, 'Stable release selected Preview'):
            self.bound(tag, 'runtime-v0.2.0')
        with self.assertRaisesRegex(ValueError, 'selected dependency SHA'):
            self.bound(tag, version, sha='f' * 40)
        with self.assertRaisesRegex(ValueError, 'go.mod differs'):
            self.bound('v0.2.7-preview.20261112', version)
        with self.assertRaises(subprocess.CalledProcessError):
            self.bound('v0.2.0', 'runtime-v0.2.0')
        with self.assertRaisesRegex(ValueError, 'uncommitted'):
            (self.dep / 'dep.go').write_text('package accelerator\n')
            self.bound(tag, version)

    def test_preview_creates_workspace_with_owners_selected_toolchain(self):
        # A bootstrap Go can select the required toolchain from a module while
        # the same binary in an empty directory would look it up on the proxy.
        shim = self.root / 'launcher'
        shim.mkdir()
        launcher = shim / 'go'
        (shim / 'go.real').symlink_to(shutil.which('go'))
        launcher.write_text('#!/bin/sh\n'
                            'if [ "$1 $2" = "work init" ]; then exit 88; fi\n'
                            'if [ "$1 $2" = "env GOROOT" ]; then pwd -P > "$0.owner"; fi\n'
                            'exec "$0.real" "$@"\n')
        launcher.chmod(0o755)
        tag = 'v0.2.0-preview.20261112'
        env = dict(self.env, PATH=str(shim) + os.pathsep + self.env['PATH'])
        bound = self.bound(tag, 'runtime-' + tag, env=env)
        self.assertEqual((shim / 'go.owner').read_text().strip(), str(self.app))
        subprocess.run(['go', 'build', '.'], cwd=self.app, env=bound, check=True)

    def test_preview_binary_is_identical_across_private_source_roots(self):
        tag = 'v0.2.0-preview.20261112'
        (self.app/'app.go').write_text('package main\nimport ("fmt"; "' + policy.PREFIX + 'accelerator")\n'
                                       'func main(){fmt.Println(accelerator.V)}\n')
        binaries = []
        for phase in ('cold', 'warm'):
            root = self.root / phase
            shutil.copytree(self.app, root / self.app.name)
            shutil.copytree(self.dep, root / self.dep.name)
            env = workspace.configure_bound_release(root, self.app.name, 'runtime-' + tag,
                        {'accelerator': {'tag':tag, 'sha':self.sha}}, self.env)
            binary = root/'probe'
            subprocess.run(['go','build','-trimpath','-o',str(binary),'.'], cwd=root/self.app.name, env=env, check=True)
            info = subprocess.check_output(['go', 'version', '-m', binary], text=True)
            self.assertNotIn(str(root), '\n'.join(info.splitlines()[1:]))
            binaries.append(binary.read_bytes())
        self.assertEqual(binaries[0], binaries[1])

    def test_stable_and_mixed_preview_resolve_formal_dependency_from_proxy(self):
        proxy = self.root / 'proxy'
        module = policy.PREFIX + 'accelerator'
        tag = 'v0.2.0'
        def publish(path, version, files):
            directory = proxy / path / '@v'
            directory.mkdir(parents=True, exist_ok=True)
            (directory / (version + '.info')).write_text(json.dumps({'Version': version, 'Time': '2026-11-01T00:00:00Z'}))
            (directory / (version + '.mod')).write_text(files['go.mod'])
            with zipfile.ZipFile(directory / (version + '.zip'), 'w') as archive:
                for name, content in files.items():
                    archive.writestr(f'{path}@{version}/{name}', content)
        source = {name: (self.dep / name).read_text() for name in ('go.mod', 'dep.go')}
        source['go.mod'] = source['go.mod'].replace('go 1.22', 'go 1.16')
        publish(module, tag, source)
        env = {**self.env, 'GOPROXY':proxy.as_uri()}
        subprocess.run(['go','mod','download',module], cwd=self.app, env=env, check=True)
        for version in ['runtime-v0.2.0', 'runtime-v0.2.0-preview.20261112']:
            bound = self.bound(tag, version, env=env)
            if '-preview.' in version:
                doc = json.loads(subprocess.check_output(['go','work','edit','-json',bound['GOWORK']], env=bound))
                self.assertEqual([r['DiskPath'] for r in doc['Use']], [str(self.app)])
                self.assertFalse(doc['Replace'])
            else:
                self.assertEqual(bound['GOWORK'], 'off')
            result = subprocess.check_output(['go','list','-m','-json',module], cwd=self.app, env=bound)
            self.assertEqual(json.loads(result)['Version'], tag)
            self.assertNotIn('Replace', json.loads(result))
            subprocess.run(['go','build','.'], cwd=self.app, env=bound, check=True)

        # The declaration still pins the selected version, but an unrelated
        # transitive requirement upgrades it under minimal version selection.
        external = 'example.test/other'
        publish(module, 'v0.2.1', source)
        publish(external, 'v1.0.0', {'go.mod': f'module {external}\ngo 1.16\nrequire {module} v0.2.1\n'})
        file = self.app / 'go.mod'
        file.write_text(file.read_text().replace('go 1.22', 'go 1.16') + f'require {external} v1.0.0\n')
        original = file.read_text()
        subprocess.run(['go', 'mod', 'download', 'all'], cwd=self.app, env=env, check=True)
        file.write_text(original)
        self.assertFalse(policy.declaration_changes(file.read_text(), {module: tag}))
        # Go rejects the stale managed version before printing the graph when
        # -mod=readonly would require an edit. Neither metadata nor tags move.
        with self.assertRaises(subprocess.CalledProcessError):
            self.bound(tag, 'runtime-v0.2.0', env=env)
        self.assertEqual(file.read_text(), original)


if __name__ == '__main__':
    unittest.main()
