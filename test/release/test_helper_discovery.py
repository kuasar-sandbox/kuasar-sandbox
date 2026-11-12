"""Exercise owner-only helper additions through build, archive and public prepare."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'release'), str(ROOT / 'ci/integration')]
import helper_inputs
import build_helpers

spec = importlib.util.spec_from_file_location('helper_package', ROOT / 'release/validate-e2e-package.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)
c = helper_inputs.contract


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.PIPE, timeout=30).strip()


class OwnerDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='helpers-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roots, self.sources = {}, {}
        for owner in c.OWNERS:
            root = self.root / 'sources' / owner
            (root / 'release').mkdir(parents=True)
            (root / c.DECLARATION).write_text(json.dumps({'schema': 1, 'recipe': None, 'dependencies': [], 'helpers': []}))
            git(root, 'init', '-q')
            git(root, 'config', 'user.name', 'Fixture')
            git(root, 'config', 'user.email', 'fixture@example.invalid')
            repository = 'kuasar-sandbox/' + ('kuasar-sandbox' if owner == 'platform' else owner)
            git(root, 'remote', 'add', 'origin', 'https://github.com/' + repository)
            git(root, 'add', '.')
            git(root, 'commit', '-qm', 'Initial source')
            self.roots[owner] = root
            self.sources[owner] = dict(repository=repository, sha=git(root, 'rev-parse', 'HEAD'), tree=git(root, 'rev-parse', 'HEAD^{tree}'))
        self.add_helper()

    def add_helper(self, recipe_extra=''):
        # This addition changes only the component source. No platform names,
        # package allowlist or environment aliases are added for the new binary.
        root = self.roots['connector']
        (root / 'release/helper.c').write_text('int main(void) { return 0; }\n')
        (root / 'release/build-helpers.sh').write_text('set -euo pipefail\ncc -o "$2/component-probe" release/helper.c\n' + recipe_extra)
        (root / c.DECLARATION).write_text(json.dumps({'schema': 1, 'recipe': 'release/build-helpers.sh', 'dependencies': [],
            'helpers': [{'name': 'component-probe', 'env': None, 'cases': ['basic.component.sh']}]}))
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'Declare component helper')
        self.sources['connector'].update(sha=git(root, 'rev-parse', 'HEAD'), tree=git(root, 'rev-parse', 'HEAD^{tree}'))
        self.contract = helper_inputs.discover(self.roots, self.sources)

    def build(self):
        output = self.root / 'built'
        env = {k: v for k, v in os.environ.items() if k not in ('GH_TOKEN', 'GITHUB_TOKEN', 'CALLER_TOKEN', 'KUASAR_CI_APP_PRIVATE_KEY')}
        build_helpers.build(self.root / 'sources', platform.machine(), output, {'component-probe': 'connector'}, env, self.contract)
        return output

    def packaged(self, built, expected_contract=None):
        release = self.root / 'release'
        (release / 'bin').mkdir(parents=True)
        shutil.copytree(ROOT / 'test/e2e/lib', release / 'test/e2e/lib', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(ROOT / 'test/e2e/e2e', release / 'test/e2e/e2e')
        cases = release / 'test/e2e/cases'; cases.mkdir()
        (cases / 'basic.component.sh').write_text('set -euo pipefail\n"$E2E_HELPERS_DIR/component-probe"\n')
        helpers = release / 'test/e2e/helpers'; helpers.mkdir()
        (helpers / 'contract.json').write_bytes(c.canonical(self.contract))
        pins = {owner: row['sha'] for owner, row in self.sources.items() if owner != 'platform'}
        for arch, machine in [('x86_64', 62), ('aarch64', 183)]:
            directory = helpers / arch; directory.mkdir()
            binary = bytearray((built / 'component-probe').read_bytes())
            # The other architecture is a non-executed archive validation fixture.
            # Native execution below always uses the actually compiled binary.
            struct.pack_into('<H', binary, 18, machine)
            (directory / 'component-probe').write_bytes(binary)
            (directory / 'component-probe').chmod(0o755)
            receipt = {'schema': 1, 'arch': arch, 'framework_sha': self.sources['platform']['sha'],
                       'test_revisions': pins, 'contract_sha256': c.digest(c.canonical(self.contract)),
                       'helpers': c.receipt(directory, arch, self.contract)}
            (directory / 'helpers.json').write_bytes(c.canonical(receipt))
        archive = self.root / 'platform.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for path in sorted(release.rglob('*')):
                if path.is_file(): tar.add(path, arcname=str(path.relative_to(release)), recursive=False)
        package.validate(archive, expected_contract=self.contract if expected_contract is None else expected_contract)
        return release

    def prepare(self, release, target='prepared', case='basic.component.sh'):
        return subprocess.run([sys.executable, '-B', str(release / 'test/e2e/e2e'), 'prepare',
            '--release-dir', str(release), '--workdir', str(self.root / target), '--arch', platform.machine(),
            '--include', case, '--offline'], text=True, capture_output=True, timeout=30)

    def test_platform_recipe_uses_fixed_owner_and_ignores_host_helpers(self):
        # Isolate upstream transport and the Go compiler boundary. Execute the
        # platform entry with its normal owner/workspace ABI; actual SDK
        # compilation belongs to the platform helper build, not this offline test.
        root = self.root / 'platform-production'; root.mkdir()
        platform_root = root / 'platform'; platform_root.mkdir()
        for name in ('release/build-platform-helpers.sh', 'release/inputs/helper-tools.yaml', 'ci/integration/materialize-source.sh'):
            target = platform_root / name; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        shutil.copytree(ROOT / 'test/e2e/platform/helpers', platform_root / 'test/e2e/platform/helpers')
        orchestrator = root / 'orchestrator'; orchestrator.mkdir()
        (orchestrator / 'go.mod').write_text('module github.com/kuasar-sandbox/orchestrator\n')
        workspace = platform_root / 'ci/integration/go_workspace.py'
        workspace.write_text('import os,sys\n'
            'assert sys.argv[1:] == ["--root", os.environ["KUASAR_WORKSPACE_ROOT"], "--owners", "orchestrator", "--source-requirements"]\n'
            'print(os.environ["FIXTURE_GO_WORKSPACE"])\n')
        upstream = root / 'upstream'; upstream.mkdir()
        (upstream / 'helper.c').write_text('int main(void) { return 0; }\n')
        subprocess.run(['cc', '-o', str(root / 'zot'), str(upstream / 'helper.c')], check=True)
        tarball = root / 'upstream.tar.gz'
        with tarfile.open(tarball, 'w:gz') as tar: tar.add(upstream, arcname='versitygw')
        bootstrap = platform_root / 'ci/hosted/bootstrap.sh'; bootstrap.parent.mkdir()
        bootstrap.write_text('download() {\n'
            'case "$1" in\n'
            'https://codeload.github.com/versity/versitygw/tar.gz/refs/tags/v1.5.0)\n'
            ' [ "$3" = 6a667e38d78a08effdf0b7c3c3e4b9929233b333c8a53c16b62eded7d43eeedc ]; cp "$FIXTURE_TARBALL" "$2" ;;\n'
            'https://github.com/project-zot/zot/releases/download/v2.1.17/zot-linux-*-minimal)\n'
            ' cp "$FIXTURE_ZOT" "$2"; echo "$3  $2" | sha256sum -c - ;;\n'
            '*) echo "unexpected upstream URL" >&2; exit 1 ;; esac\n}\n')
        guest = root / 'guest-runtime/native-deps/deps'; guest.mkdir(parents=True)
        (guest / 'build-versitygw.sh').write_text('set -euo pipefail\n'
            '[ "$VERSITYGW_GOFLAGS" = "-mod=mod -p=2" ]\n'
            '[ "$GO_ARCH" = amd64 ] || [ "$GO_ARCH" = arm64 ]\n'
            '[ -f "$VERSITYGW_SRC/.extracted" ]\n'
            '[ -f "$VERSITYGW_TARBALL" ]\n'
            'cc -o "$BINDIR/versitygw" "$VERSITYGW_SRC/helper.c"\n'
            'echo fixed-owner > "$FIXTURE_OWNER_USED"\n')
        commands = root / 'commands'; commands.mkdir()
        poison = commands / 'zot'; poison.write_text('#!/bin/sh\necho bypass > "$FIXTURE_POISON"\nexit 91\n'); poison.chmod(0o755)
        shutil.copy2(poison, commands / 'versitygw')
        curl = commands / 'curl'; curl.write_text('#!/bin/sh\nset -eu\n'
            'case "$*" in *https://github.com/project-zot/zot/releases/download/v2.1.17/checksums.sha256.txt*) ;; *) exit 92 ;; esac\n'
            'while [ "$1" != -o ]; do shift; done\n'
            'printf "%s  zot-linux-amd64-minimal\\n%s  zot-linux-arm64-minimal\\n" "$FIXTURE_ZOT_SHA" "$FIXTURE_ZOT_SHA" > "$2"\n'); curl.chmod(0o755)
        compiler = commands / 'go'
        compiler.write_text('#!/usr/bin/env python3\nimport os,pathlib,shutil,sys\n'
            'args=sys.argv[1:]\n'
            'if args[:1] == ["-C"]:\n'
            ' assert args[1] == os.environ["KUASAR_WORKSPACE_ROOT"] + "/orchestrator"\n'
            ' assert os.environ["GOWORK"] == os.environ["FIXTURE_GO_WORKSPACE"]\n'
            ' args=args[2:]\n'
            'else: assert os.environ["GOWORK"] == "off"\n'
            'assert args[:4] == ["build", "-p", "2", "-trimpath"]\n'
            'assert pathlib.Path(args[-1]).is_file()\n'
            'assert "/test/e2e/platform/helpers/" in args[-1]\n'
            'shutil.copy2(os.environ["FIXTURE_ZOT"],args[args.index("-o")+1])\n')
        compiler.chmod(0o755)
        env = dict(os.environ, PATH=str(commands)+':'+os.environ['PATH'], KUASAR_WORKSPACE_ROOT=str(root),
                   KUASAR_BUILD_JOBS='2', FIXTURE_TARBALL=str(tarball), FIXTURE_ZOT=str(root/'zot'),
                   FIXTURE_ZOT_SHA=c.digest((root/'zot').read_bytes()), FIXTURE_OWNER_USED=str(root/'used'),
                   FIXTURE_GO_WORKSPACE=str(root/'selected-go.work'),
                   FIXTURE_POISON=str(root/'poison'), E2E_ZOT_BIN=str(poison), ZOT_BIN=str(poison),
                   E2E_VGW_BIN=str(poison), VGW_BIN=str(poison))
        output = root / 'output'; output.mkdir()
        command = ['bash', str(platform_root/'release/build-platform-helpers.sh'), platform.machine(), str(output)]
        subprocess.run(command, env=env, check=True, capture_output=True)
        names = {'zot', 'versitygw', 'node-infra-guest', 'node-infra-xproxy', 'node-infra-xconductor'}
        self.assertEqual(set(p.name for p in output.iterdir()), names)
        for name in names: c.check_binary(output/name, platform.machine())
        self.assertEqual((root/'used').read_text(), 'fixed-owner\n')
        self.assertFalse((root/'poison').exists())
        (guest/'build-versitygw.sh').unlink()
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing admitted guest-runtime', result.stderr)

    def test_component_only_addition_builds_packages_prepares_and_runs(self):
        release = self.packaged(self.build())
        result = self.prepare(release)
        self.assertEqual(result.returncode, 0, result.stderr)
        prepared = self.root / 'prepared'
        provenance = json.loads((prepared / 'provenance.json').read_text())
        self.assertEqual(provenance['helper_contract'], c.runtime(self.contract))
        self.assertEqual(set(provenance['helpers']), {'component-probe'})
        result = subprocess.run([sys.executable, '-B', str(prepared / 'test/e2e/e2e'), 'run', '--workdir', str(prepared),
                                 '--include', 'basic.component.sh'], text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PASS basic.component.sh', result.stdout)

    def test_repeated_subset_prepare_preserves_exact_declared_inventory(self):
        release = self.packaged(self.build())
        (release / 'test/e2e/cases/basic.other.sh').write_text('exit 0\n')
        self.assertEqual(self.prepare(release, case='basic.*').returncode, 0)
        result = self.prepare(self.root / 'prepared', 'prepared-again', case='basic.other.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        original = json.loads((self.root / 'prepared/provenance.json').read_text())
        repeated = json.loads((self.root / 'prepared-again/provenance.json').read_text())
        self.assertEqual(original['helper_contract'], repeated['helper_contract'])
        self.assertEqual(set(original['helpers']), {'component-probe'})
        self.assertEqual(repeated['helpers'], {})
        self.assertFalse((self.root / 'prepared-again/fixtures/bin/component-probe').exists())
        result = self.prepare(self.root / 'prepared-again', 'prepared-third', case='basic.other.sh')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_extra_output_is_rejected_before_packaging(self):
        self.add_helper('cp "$2/component-probe" "$2/undeclared"\n')
        with self.assertRaisesRegex(ValueError, 'unexpected or missing built helper'):
            self.build()

    def test_receipt_cannot_authorize_extra_package_helper(self):
        release = self.packaged(self.build())
        directory = release / 'test/e2e/helpers' / platform.machine()
        shutil.copy2(directory / 'component-probe', directory / 'undeclared')
        path = directory / 'helpers.json'; data = json.loads(path.read_text())
        data['helpers']['undeclared'] = data['helpers']['component-probe']
        path.write_text(json.dumps(data))
        result = self.prepare(release)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('package declarations', result.stderr)
        self.assertFalse((self.root / 'prepared').exists())

    def test_missing_tampered_and_wrong_architecture_helpers_fail(self):
        release = self.packaged(self.build())
        file = release / 'test/e2e/helpers' / platform.machine() / 'component-probe'
        original = file.read_bytes()
        for data in (b'bad', original + b'tamper'):
            file.write_bytes(data)
            result = self.prepare(release)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / 'prepared').exists())
        file.unlink()
        self.assertNotEqual(self.prepare(release).returncode, 0)

    def test_private_extraction_modes_are_bound_without_rewriting_archive_receipt(self):
        release = self.packaged(self.build())
        directory = release / 'test/e2e/helpers' / platform.machine()
        original = (directory / 'helpers.json').read_bytes()
        binary = directory / 'component-probe'
        binary.chmod(0o700)
        result = self.prepare(release)
        self.assertEqual(result.returncode, 0, result.stderr)
        provenance = json.loads((self.root / 'prepared/provenance.json').read_text())
        self.assertEqual(provenance['helpers']['component-probe']['mode'], 0o700)
        self.assertFalse((self.root / 'prepared/test/e2e/helpers').exists())
        self.assertEqual((directory / 'helpers.json').read_bytes(), original)
        for mode in (0o777, 0o4755, 0o600, 0o001):
            binary.chmod(mode)
            result = self.prepare(release, 'rejected')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / 'rejected').exists())

    def test_yaml_helper_binds_original_blob_with_legacy_component_tags(self):
        root = self.roots['platform']
        old = c.DECLARATION
        for path in ('releases/helper-inputs.yaml', 'release/inputs/helper-inputs.yaml'):
            with self.subTest(path=path):
                (root / path).parent.mkdir()
                git(root, 'mv', old, path)
                git(root, 'commit', '-qm', 'Move platform declaration')
                self.sources['platform'].update(sha=git(root, 'rev-parse', 'HEAD'), tree=git(root, 'rev-parse', 'HEAD^{tree}'))
                contract = helper_inputs.discover(self.roots, self.sources)
                row = contract['owners']['platform']
                self.assertEqual(row['declaration_blob'], git(root, 'rev-parse', 'HEAD:' + path))
                self.assertEqual(row['declaration_sha256'], self.contract['owners']['platform']['declaration_sha256'])
                self.assertEqual(contract['owners']['connector'], self.contract['owners']['connector'])
                old = path

    def test_source_declaration_and_tag_are_independently_bound(self):
        root = self.roots['connector']
        for tag in ('v1.2.3', 'v1.2.4'): git(root, 'tag', tag)
        self.sources['connector']['tag'] = 'v1.2.3'
        contract = helper_inputs.discover(self.roots, self.sources)
        changed = copy.deepcopy(contract)
        changed['owners']['connector']['source']['tag'] = 'v1.2.4'
        # Package consumption compares runtime fields; source build inputs
        # remain verified separately within their own build.
        self.contract = contract
        built = self.build()
        self.contract = changed
        self.packaged(built, expected_contract=contract)
        self.contract = contract
        (root / c.DECLARATION).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'modified tag inputs'):
            helper_inputs.verify_sources(contract, self.roots)
        (root / c.DECLARATION).write_text(contract['owners']['connector']['declaration'])
        changed = copy.deepcopy(contract); changed['owners']['connector']['declaration_sha256'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'declaration identity'):
            helper_inputs.verify_sources(changed, self.roots)
        changed = copy.deepcopy(contract); changed['owners']['connector']['source']['tree'] = 'f' * 40
        with self.assertRaisesRegex(ValueError, 'identity'):
            helper_inputs.verify_sources(changed, self.roots)

    def test_missing_declaration_cannot_downgrade_and_recipe_tamper_fails(self):
        root = self.roots['connector']
        declaration = root / c.DECLARATION
        declaration.unlink()
        git(root, 'add', '-u'); git(root, 'commit', '-qm', 'remove declaration')
        self.sources['connector'].update(sha=git(root, 'rev-parse', 'HEAD'), tree=git(root, 'rev-parse', 'HEAD^{tree}'))
        with self.assertRaisesRegex(ValueError, 'missing helper input'):
            helper_inputs.discover(self.roots, self.sources)
        declaration.write_text(self.contract['owners']['connector']['declaration'])
        git(root, 'add', '.'); git(root, 'commit', '-qm', 'restore declaration')
        self.sources['connector'].update(sha=git(root, 'rev-parse', 'HEAD'), tree=git(root, 'rev-parse', 'HEAD^{tree}'))
        contract = helper_inputs.discover(self.roots, self.sources)
        contract['owners']['connector']['recipe_sha256'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'source/declaration differs'):
            helper_inputs.verify_sources(contract, self.roots)
        git(root, 'remote', 'set-url', 'origin', 'https://github.com/kuasar-sandbox/connector-extra')
        with self.assertRaisesRegex(ValueError, 'repository changed'):
            helper_inputs.discover(self.roots, self.sources)

    def test_helper_declaration_change_does_not_select_product_rebuild(self):
        import artifacts
        spec = importlib.util.spec_from_file_location('helper_preview_selection', ROOT / 'release/preview-selection.py')
        selection = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = selection; spec.loader.exec_module(selection)
        root = self.roots['connector']
        changed = git(root, 'diff', '--name-only', 'HEAD^', 'HEAD').splitlines()
        self.assertIn(c.DECLARATION, changed)
        self.assertEqual(artifacts.changed_products({'connector': changed}), [])
        self.assertFalse(selection.unit_inputs_changed(root, 'HEAD^', 'HEAD', 'connector'))

    def test_required_capabilities_cannot_be_removed_from_declarations(self):
        with self.assertRaisesRegex(ValueError, 'required helper capability'):
            c.selected(self.contract, ['sandbox.cgroup.sh'])

    def test_declaration_rejects_duplicate_paths_and_wrong_owner(self):
        value = json.loads(self.contract['owners']['connector']['declaration'])
        for mutate in (lambda x: x['helpers'].append(x['helpers'][0]),
                       lambda x: x.update(recipe='../escape'),
                       lambda x: x['helpers'][0].update(name='dir/file')):
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(ValueError): c.declaration(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            c.declaration('{"schema":1,"schema":1}')
        changed = copy.deepcopy(self.contract)
        changed['owners']['connector']['source']['repository'] = 'kuasar-sandbox/connector-extra'
        with self.assertRaisesRegex(ValueError, 'owner/identity'): c.validate(changed)





class RuntimePackages(unittest.TestCase):
    setUp = OwnerDiscovery.setUp
    add_helper = OwnerDiscovery.add_helper
    build = OwnerDiscovery.build
    packaged = OwnerDiscovery.packaged
    prepare = OwnerDiscovery.prepare
    # Only the added tests need fixtures; do not rerun the source build suite.
    def test_new_format_prepares_and_runs_without_source_records(self):
        release = self.packaged(self.build())
        helpers = release / 'test/e2e/helpers'
        (helpers / 'contract.json').unlink()
        for arch in c.ARCHES:
            directory = helpers / arch
            (directory / 'helpers.json').unlink()
            (directory / 'helpers.json').write_text(json.dumps(dict(schema=2, arch=arch,
                helpers=c.receipt(directory, arch, self.contract))))
        prepared = self.prepare(release)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        target = self.root / 'prepared'
        row = json.loads((target / 'provenance.json').read_text())
        self.assertEqual(row['helper_contract'], c.runtime(self.contract))
        self.assertEqual(set(row['helpers']['component-probe']), {'sha256','size','mode','owner','env','cases'})
        self.assertFalse((target / 'test/e2e/helpers').exists())
        result = subprocess.run([sys.executable, '-B', str(target / 'test/e2e/e2e'), 'run', '--workdir', str(target),
            '--include', 'basic.component.sh'], text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_old_package_runner_uses_matching_reader_after_new_preparation(self):
        release = self.packaged(self.build())
        # Freeze real old public runtime code, not a fixture with new libraries.
        for fixture, name in (('legacy-e2e.py', 'test/e2e/e2e'),
                              ('legacy-helper-reader.py', 'test/e2e/lib/helper_contract.py')):
            shutil.copyfile(ROOT / 'test/release/fixtures' / fixture, release / name)
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'test/e2e/e2e'), 'prepare',
            '--release-dir', str(release), '--workdir', str(self.root / 'prepared'), '--arch', platform.machine(),
            '--include', 'basic.component.sh', '--offline'], text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        prepared = self.root / 'prepared'
        self.assertEqual((prepared / 'test/e2e/e2e').read_bytes(), (release / 'test/e2e/e2e').read_bytes())
        result = subprocess.run([sys.executable, '-B', str(prepared / 'test/e2e/e2e'), 'run',
            '--workdir', str(prepared), '--include', 'basic.component.sh'], text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_runtime_ignores_build_declarations(self):
        changed = copy.deepcopy(self.contract)
        for row in changed['owners'].values():
            declared = json.loads(row['declaration'])
            declared['recipe'] = None
            declared['dependencies'] = ['obsolete-owner']
            row['declaration'] = json.dumps(declared)
            row['source'] = None
            row['declaration_sha256'] = 'obsolete'
        self.assertEqual(c.runtime(changed), c.runtime(self.contract))
        declared = json.loads(changed['owners']['connector']['declaration'])
        declared['helpers'][0]['env'] = 'unsafe-env'
        changed['owners']['connector']['declaration'] = json.dumps(declared)
        with self.assertRaisesRegex(ValueError, 'environment'):
            c.runtime(changed)

    def test_legacy_inventory_keeps_all_seventeen_and_ignores_proofs(self):
        built = self.build()
        root = self.root / 'legacy'; root.mkdir()
        names = sorted(set(c.LEGACY_ALIASES) | {'helper-' + str(i) for i in range(10)})
        records = {}
        for name in names:
            shutil.copy2(built / 'component-probe', root / name)
            records[name] = {'sha256': c.digest((root / name).read_bytes()), 'source_sha': 'obsolete'}
        metadata = dict(arch=platform.machine(), helpers=records, framework_sha='obsolete',
            test_revisions='unavailable', source_records='unavailable')
        (root / 'helpers.json').write_text(json.dumps(metadata))
        runtime, actual = c.package(root, platform.machine())
        self.assertEqual(len(actual), 17)
        self.assertEqual(set(runtime['helpers']), set(names))
        self.assertTrue(all(set(row) == {'owner','env','cases','size','sha256','mode'} for row in actual.values()))
        metadata['helpers']['zot']['sha256'] = '0' * 64
        (root / 'helpers.json').write_text(json.dumps(metadata))
        with self.assertRaises(ValueError): c.package(root, platform.machine())

    def test_unknown_package_schema_cannot_hide_duplicate_runtime_environment(self):
        built = self.build()
        root = self.root / 'runtime'; root.mkdir()
        for name in ('one', 'two'): shutil.copy2(built / 'component-probe', root / name)
        rows = {name: dict(c.check_binary(root / name, platform.machine()), owner='platform',
                          env='DUP_BIN', cases=['*']) for name in ('one','two')}
        for schema in (2, 999, True, None):
            (root / 'helpers.json').write_text(json.dumps(dict(schema=schema, arch=platform.machine(), helpers=rows)))
            with self.assertRaises(ValueError): c.package(root, platform.machine())

    def test_runtime_duplicate_environment_and_unsafe_names_fail(self):
        contract = c.runtime(self.contract)
        contract['helpers']['component-probe']['env'] = 'PROBE_BIN'
        contract['helpers']['other'] = dict(contract['helpers']['component-probe'])
        with self.assertRaisesRegex(ValueError, 'duplicate'): c.validate(contract)
        contract['helpers'].pop('other')
        contract['helpers']['../escape'] = contract['helpers'].pop('component-probe')
        with self.assertRaises(ValueError): c.validate(contract)


if __name__ == '__main__': unittest.main()
