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

ROOT = Path(__file__).resolve().parents[1]
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
        package.validate(archive, pins, expected_contract=self.contract if expected_contract is None else expected_contract)
        return release

    def prepare(self, release, target='prepared', case='basic.component.sh'):
        return subprocess.run([sys.executable, '-B', str(release / 'test/e2e/e2e'), 'prepare',
            '--release-dir', str(release), '--workdir', str(self.root / target), '--arch', platform.machine(),
            '--include', case, '--offline'], text=True, capture_output=True, timeout=30)

    def test_platform_recipe_uses_fixed_owner_and_ignores_host_helpers(self):
        # Isolate upstream transport only. Execute the unchanged platform entry
        # and an owner fixture with the normal versitygw build ABI.
        root = self.root / 'platform-production'; root.mkdir()
        platform_root = root / 'platform'; platform_root.mkdir()
        for name in ('release/build-platform-helpers.sh', 'ci/integration/materialize-source.sh'):
            target = platform_root / name; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
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
        env = dict(os.environ, PATH=str(commands)+':'+os.environ['PATH'], KUASAR_WORKSPACE_ROOT=str(root),
                   KUASAR_BUILD_JOBS='2', FIXTURE_TARBALL=str(tarball), FIXTURE_ZOT=str(root/'zot'),
                   FIXTURE_ZOT_SHA=c.digest((root/'zot').read_bytes()), FIXTURE_OWNER_USED=str(root/'used'),
                   FIXTURE_POISON=str(root/'poison'), E2E_ZOT_BIN=str(poison), ZOT_BIN=str(poison),
                   E2E_VGW_BIN=str(poison), VGW_BIN=str(poison))
        output = root / 'output'; output.mkdir()
        command = ['bash', str(platform_root/'release/build-platform-helpers.sh'), platform.machine(), str(output)]
        subprocess.run(command, env=env, check=True, capture_output=True)
        self.assertEqual(set(p.name for p in output.iterdir()), {'zot','versitygw'})
        for name in ('zot','versitygw'): c.check_binary(output/name, platform.machine())
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
        self.assertEqual(provenance['helper_contract'], self.contract)
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
        self.assertIn('trusted inventory', result.stderr)
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
        self.assertEqual((self.root / 'prepared/test/e2e/helpers' / platform.machine() / 'helpers.json').read_bytes(), original)
        for mode in (0o777, 0o4755, 0o600, 0o001):
            binary.chmod(mode)
            result = self.prepare(release, 'rejected')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / 'rejected').exists())

    def test_source_declaration_and_tag_are_independently_bound(self):
        root = self.roots['connector']
        for tag in ('v1.2.3', 'v1.2.4'): git(root, 'tag', tag)
        self.sources['connector']['tag'] = 'v1.2.3'
        contract = helper_inputs.discover(self.roots, self.sources)
        changed = copy.deepcopy(contract)
        changed['owners']['connector']['source']['tag'] = 'v1.2.4'
        # The same commit under another tag cannot replace the planned source
        # in an otherwise byte-identical package and producer receipt.
        self.contract = contract
        built = self.build()
        self.contract = changed
        with self.assertRaisesRegex(ValueError, 'packaged helper contract differs from source inputs'):
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





class HistoricalEvidence(unittest.TestCase):
    def test_authentic_original_producer_scope_and_failure_boundaries(self):
        import helper_history
        import zipfile
        from unittest import mock
        fixtures = ROOT / 'ci/integration/fixtures'
        original = json.loads((fixtures / 'helper-original-plan.json').read_text())
        for arch in c.ARCHES:
            with self.subTest(arch=arch), zipfile.ZipFile(fixtures / f'helper-original-producer-{arch}.zip') as archive:
                files = {name: archive.read(name).decode() for name in archive.namelist()
                         if name.endswith('/receipt.json') or name.startswith('workbench-cache-scope-')}
                scope = json.loads(next(value for name, value in files.items() if name.startswith('workbench-cache-scope-')))
                roots = {row['path']: (Path('/unused') / row['path'], {'repository': row['repository'], 'sha': row['sha']})
                         for row in scope['sources']}
                content = {str(roots[row['path']][0]): row['content_sha256'] for row in scope['sources']}
                # No historical sources are fetched by SHA. The retained receipt
                # is original evidence; source-byte checking is exercised below.
                real_exec = helper_history.importlib.util.module_from_spec
                def cache_module(spec):
                    module = real_exec(spec)
                    return module
                with mock.patch.object(helper_history.importlib.util, 'module_from_spec', side_effect=cache_module):
                    with mock.patch.object(helper_history.importlib.util, 'spec_from_file_location') as load:
                        fake = mock.Mock()
                        fake.loader.exec_module.side_effect = lambda module: setattr(module, 'snapshot',
                            lambda root, git: (content[str(root)], {}))
                        load.return_value = fake
                        # Use an actual module to preserve production validation.
                        with mock.patch.object(helper_history.importlib.util, 'module_from_spec', return_value=mock.Mock()):
                            observed = helper_history.producer(files, arch, int(scope['run_id']), original, roots)
                            self.assertEqual(observed['receipt'], next(v for n, v in files.items() if n.endswith('/receipt.json')))
                            first = next(iter(content)); old = content[first]; content[first] = '0' * 64
                            with self.assertRaisesRegex(ValueError, 'dependency content changed'):
                                helper_history.producer(files, arch, int(scope['run_id']), original, roots)
                            content[first] = old
                            for field, value in [('framework_sha', '0' * 40), ('conclusion', 'failure')]:
                                bad = dict(files); name = next(n for n in bad if n.endswith('/receipt.json'))
                                row = json.loads(bad[name]); row[field] = value; bad[name] = json.dumps(row)
                                with self.assertRaises(ValueError): helper_history.producer(bad, arch, int(scope['run_id']), original, roots)
                            with self.assertRaisesRegex(ValueError, 'source scope'):
                                helper_history.producer(files, arch, int(scope['run_id']) + 1, original, roots)


class HistoricalTransport(unittest.TestCase):
    setUp = OwnerDiscovery.setUp
    add_helper = OwnerDiscovery.add_helper
    build = OwnerDiscovery.build
    prepare = OwnerDiscovery.prepare
    # Reuse the real ELF compiler and public runner fixture with a distinct
    # original pre-declaration source/archive.
    def test_original_binary_transport_keeps_true_origin(self):
        import helper_history
        built = self.build()
        for owner, root in self.roots.items():
            if owner == 'platform': continue
            (root / c.DECLARATION).unlink()
            (root / 'owner.txt').write_text(owner)
            git(root, 'add', '-A'); git(root, 'commit', '-qm', 'Original pre-declaration fixture')
            tag = ('runtime-' if owner == 'guest-runtime' else '') + 'v1.2.3'
            git(root, 'tag', tag)
            self.sources[owner].update(sha=git(root, 'rev-parse', 'HEAD'), tree=git(root, 'rev-parse', 'HEAD^{tree}'), tag=tag)
        records = {('runtime' if o == 'guest-runtime' else o): s for o, s in self.sources.items() if o != 'platform'}
        original = {'schema': 2, 'mode': 'exact-assets', 'framework_sha': self.sources['platform']['sha'],
                    'source_records': records, 'test_revisions': {o: dict(repository=s['repository'], sha=s['sha'], role='release') for o, s in self.sources.items()},
                    'baseline': {'repository': 'kuasar-sandbox/kuasar-sandbox', 'version': 'release-v1.2.3', 'assets': []}}
        old = self.root / 'old'; old.mkdir()
        for arch, machine in [('x86_64', 62), ('aarch64', 183)]:
            path = old / 'test/e2e/helpers' / arch; path.mkdir(parents=True)
            data = bytearray((built / 'component-probe').read_bytes()); struct.pack_into('<H', data, 18, machine)
            (path / 'custom-proxy').write_bytes(data); (path / 'custom-proxy').chmod(0o755)
            (path / 'helpers.json').write_text(json.dumps({'arch': arch, 'framework_sha': original['framework_sha'],
                'test_revisions': {o: s['sha'] for o, s in self.sources.items() if o != 'platform'}, 'source_records': records,
                'helpers': {'custom-proxy': {'source_sha': self.sources['connector']['sha'], 'sha256': c.digest(data)}}}))
        archive = self.root / 'platform-release-v1.2.3.tar.gz'
        def owned(member):
            member.uid = member.gid = 0; member.uname = member.gname = ''; return member
        with tarfile.open(archive, 'w:gz') as tar: tar.add(old, arcname='.', filter=owned)
        original['baseline']['assets'] = [{'name': archive.name, 'size': archive.stat().st_size, 'digest': 'sha256:' + c.digest(archive.read_bytes())}]
        available, metadata = helper_history.inventory(archive, original, {o:s for o,s in self.sources.items() if o != 'platform'})
        # Exercise the actual admission boundary with a fixed API/ZIP transport.
        # The source scope hashes are computed from these real clean repositories.
        import zipfile
        from unittest import mock
        old_platform = self.root / 'original-platform'
        shutil.copytree(self.roots['platform'], old_platform)
        (self.roots['platform'] / 'candidate.txt').write_text('consumer candidate\n')
        git(self.roots['platform'], 'add', '.')
        git(self.roots['platform'], 'commit', '-qm', 'Candidate consumer')
        self.sources['platform'].update(sha=git(self.roots['platform'], 'rev-parse', 'HEAD'),
                                        tree=git(self.roots['platform'], 'rev-parse', 'HEAD^{tree}'))
        cache_spec = importlib.util.spec_from_file_location('original_scope_fixture', ROOT / 'ci/hosted/cache-scope.py')
        cache = importlib.util.module_from_spec(cache_spec); cache_spec.loader.exec_module(cache)
        scope_sources = []
        producer_roots = {owner: (root, self.sources[owner]) for owner, root in self.roots.items() if owner != 'platform'}
        producer_roots['platform'] = (old_platform, dict(repository='kuasar-sandbox/kuasar-sandbox', sha=original['framework_sha']))
        git(self.roots['guest-runtime'], 'tag', 'vmlinux-v1.2.3')
        original['source_records']['vmlinux'] = dict(self.sources['guest-runtime'], tag='vmlinux-v1.2.3')
        producer_roots['kernel-unit/guest-runtime'] = (self.roots['guest-runtime'], original['source_records']['vmlinux'])
        # The fixture has no separate kernel contents; its independent tag is
        # not consumed as a helper source and remains explicit in original facts.
        for name, (root, facts) in producer_roots.items():
            scope_sources.append(dict(path=name, repository=facts['repository'], sha=facts['sha'],
                clean=True, on_main=True, kind='git', content_sha256=cache.snapshot(root, git=True)[0]))
        # Retain source_records in the original metadata after adding the
        # independent kernel fixture, then bind the actual complete archive.
        for arch in c.ARCHES:
            path = old / 'test/e2e/helpers' / arch / 'helpers.json'
            row = json.loads(path.read_text()); row['source_records'] = original['source_records']; path.write_text(json.dumps(row))
        with tarfile.open(archive, 'w:gz') as tar: tar.add(old, arcname='.', filter=owned)
        original['baseline']['assets'][0].update(size=archive.stat().st_size, digest='sha256:' + c.digest(archive.read_bytes()))
        producer_archives, api_artifacts = {}, []
        for index, arch in enumerate(c.ARCHES, 1):
            with zipfile.ZipFile(ROOT / 'ci/integration/fixtures' / f'helper-original-producer-{arch}.zip') as saved:
                name = next(n for n in saved.namelist() if n.endswith('/receipt.json'))
                receipt = json.loads(saved.read(name))
            old_version = receipt['commands'][-1]['argv'][-1].split("--version '")[1].split("'",1)[0]
            receipt['framework_sha'] = original['framework_sha']
            receipt['selection']['framework_sha'] = original['framework_sha']
            receipt['commands'][-1]['argv'][-1] = receipt['commands'][-1]['argv'][-1].replace(old_version, original['baseline']['version'])
            scope = dict(repository='kuasar-sandbox/kuasar-sandbox', run_id='1', event='workflow_dispatch',
                         namespace='trusted', scope='trusted', sources=scope_sources)
            zip_path = self.root / (arch + '.zip')
            with zipfile.ZipFile(zip_path, 'w') as output:
                output.writestr(name, json.dumps(receipt))
                output.writestr('workbench-cache-scope-1-fixture.json', json.dumps(scope))
            producer_archives[str(index)] = zip_path
            api_artifacts.append(dict(id=index, name='workbench-evidence-helper-build-' + arch + '-' + 'a' * 32,
                expired=False, size_in_bytes=zip_path.stat().st_size, digest='sha256:' + c.digest(zip_path.read_bytes()),
                workflow_run=dict(id=1, head_sha=original['framework_sha'])))
        def api(path):
            if path.endswith('/artifacts?per_page=100'): return dict(total_count=2, artifacts=api_artifacts)
            self.assertTrue(path.endswith('/actions/runs/1'))
            return dict(status='completed', conclusion='success', head_sha=original['framework_sha'],
                path='.github/workflows/aggregate-release.yml', event='workflow_dispatch',
                head_repository=dict(full_name='kuasar-sandbox/kuasar-sandbox'))
        def download(*args):
            self.assertEqual(args[:3], ('release', 'download', 'release-v1.2.3'))
            shutil.copy2(archive, Path(args[-1]) / archive.name)
        def transport(command, **kwargs):
            self.assertEqual(command[:2], ['gh', 'api'])
            artifact_id = command[2].split('/artifacts/')[1].split('/')[0]
            kwargs['stdout'].write(producer_archives[artifact_id].read_bytes())
            return subprocess.CompletedProcess(command, 0)
        admission = self.root / 'admitted'; admission.mkdir()
        real_run = subprocess.run
        def dispatched(command, **kwargs):
            return transport(command, **kwargs) if command[:2] == ['gh', 'api'] else real_run(command, **kwargs)
        with mock.patch.object(helper_history.subprocess, 'run', side_effect=dispatched):
            contract = helper_history.admit(dict(original['baseline'], validation_plan=original),
                dict(self.roots, vmlinux=self.roots['guest-runtime']), self.sources, old_platform, 1, api, download, admission)
        # Even an unchanged helper owner cannot reuse an old linked binary when
        # another component dependency changes. Reject before any download.
        for field, value in [('sha', '0' * 40), ('tag', 'v9.9.9')]:
            sources = json.loads(json.dumps(self.sources))
            sources['accelerator'][field] = value
            with mock.patch.object(helper_history, 'gh') as no_download:
                with self.assertRaisesRegex(ValueError, 'changed candidate dependency'):
                    helper_history.admit(dict(original['baseline'], validation_plan=original),
                        dict(self.roots, vmlinux=self.roots['guest-runtime']), sources, old_platform,
                        1, api, no_download, admission)
                no_download.assert_not_called()
        available, metadata = helper_history.inventory(archive, original, {o:s for o,s in self.sources.items() if o != 'platform'})
        c.validate(contract)
        # Historical interface aliases participate in the same uniqueness check
        # as new declarations, without changing any original helper metadata.
        mixed = copy.deepcopy(contract)
        row = mixed['owners']['platform']
        declaration = {'schema': 1, 'recipe': 'release/platform-probe.sh', 'dependencies': [],
                       'helpers': [{'name': 'platform-probe', 'env': 'CUSTOM_PROXY_BIN', 'cases': ['basic.component.sh']}]}
        def update_alias(alias):
            declaration['helpers'][0]['env'] = alias
            data = json.dumps(declaration).encode()
            row.update(declaration=data.decode(), declaration_sha256=c.digest(data), declaration_blob=c.blob(data),
                       recipe_sha256=c.digest(b'fixture'), recipe_blob=c.blob(b'fixture'))
        update_alias('CUSTOM_PROXY_BIN')
        with self.assertRaisesRegex(ValueError, 'duplicate helper environment'):
            c.validate(mixed)
        update_alias('PLATFORM_PROBE_BIN')
        self.assertEqual(c.validate(mixed)['platform-probe']['env'], 'PLATFORM_PROBE_BIN')
        self.assertEqual(c.validate(mixed)['custom-proxy']['env'], None)
        output = self.root / 'restored'
        environment = {k:v for k,v in os.environ.items() if k not in ('GH_TOKEN','GITHUB_TOKEN','CALLER_TOKEN','KUASAR_CI_APP_PRIVATE_KEY')}
        build_helpers.build(self.root / 'sources', platform.machine(), output, {'custom-proxy':'connector'}, environment, contract, archive)
        self.assertEqual((output / 'custom-proxy').read_bytes(), (built / 'component-probe').read_bytes())
        # A composed workspace carries the trusted plan contract and exact
        # historical receipt. Public preparation consumes it without rebuilding.
        release = self.root / 'historical-prepared'; (release / 'bin').mkdir(parents=True)
        shutil.copytree(ROOT / 'test/e2e/lib', release / 'test/e2e/lib', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(ROOT / 'test/e2e/e2e', release / 'test/e2e/e2e')
        (release / 'test/e2e/cases').mkdir()
        (release / 'test/e2e/cases/basic.component.sh').write_text('set -euo pipefail\n"$E2E_HELPERS_DIR/custom-proxy"\n')
        shutil.copytree(output, release / 'fixtures/bin')
        workspace_spec = importlib.util.spec_from_file_location('historical_workspace', ROOT / 'test/e2e/lib/workspace.py')
        workspace = importlib.util.module_from_spec(workspace_spec); workspace_spec.loader.exec_module(workspace)
        aliases = workspace.helper_variables({'helper_contract': mixed})
        self.assertEqual(aliases['custom-proxy'], 'CUSTOM_PROXY_BIN')
        self.assertEqual(aliases['platform-probe'], 'PLATFORM_PROBE_BIN')
        self.assertEqual(mixed['history'], contract['history'])
        workspace.seal(release, {'arch': platform.machine(), 'prepared_cases':['basic.component.sh'], 'helper_contract':contract,
                                'helpers': c.receipt(output, platform.machine(), contract)})
        result = self.prepare(release); self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads((self.root / 'prepared/provenance.json').read_text())
        self.assertEqual(actual['helper_contract']['history']['metadata'], metadata)
        ran = subprocess.run([sys.executable, '-B', str(self.root / 'prepared/test/e2e/e2e'), 'run',
            '--workdir', str(self.root / 'prepared'), '--include', 'basic.component.sh'],
            text=True, capture_output=True, timeout=30)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(actual['helpers']['custom-proxy']['source_sha'], self.sources['connector']['sha'])
        for owner in ('accelerator','connector','guest-runtime','sandboxer','orchestrator'):
            bad = json.loads(json.dumps(contract)); bad['owners'][owner]['source']['tag'] += '-preview.20260101'
            with self.assertRaisesRegex(ValueError, 'dependency tag/source'): c.validate(bad)
        with archive.open('ab') as stream: stream.write(b'tamper')
        with self.assertRaisesRegex(ValueError, 'archive changed'):
            helper_history.inventory(archive, original, {o:s for o,s in self.sources.items() if o != 'platform'})


if __name__ == '__main__': unittest.main()
