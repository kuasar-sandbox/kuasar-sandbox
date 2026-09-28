#!/usr/bin/env python3
"""Exercise cold host-tool setup and the artifact E2E case bridge."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import artifacts as ARTIFACTS
from test_fixtures import CASES, selection, make_demo_wheelhouse

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('assemble_docs', ROOT / 'test/e2e/assemble_docs.py')
ASSEMBLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ASSEMBLER)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DemoSDKFixtures(unittest.TestCase):
    def setUp(self):
        self.prepare = load_module('demo_sdk_prepare', ROOT / 'test/e2e/lib/workspace.py')
        temporary = tempfile.TemporaryDirectory(prefix='kuasar-demo-sdk-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.requirements = self.root / 'test/demo/requirements.txt'
        self.arch = self.prepare.platform.machine()
        self.wheels = self.root / 'test/demo/wheels' / self.arch
        make_demo_wheelhouse(self.requirements.parent, self.wheels.parent, (self.arch,))

    def test_wheel_tree_is_complete_without_host_packages_or_bytecode(self):
        # Run the production installer unchanged; no test-injected offline flags.
        real_run = subprocess.run
        commands = []
        def record_run(command, **kwargs):
            commands.append(command)
            return real_run(command, **kwargs)
        with patch.dict(os.environ, {'PIP_INDEX_URL': 'https://index.invalid/unreachable'}), \
             patch.object(self.prepare.subprocess, 'run', side_effect=record_run):
            record = self.prepare.prepare_demo_sdk(self.root)
        self.assertEqual(record['requirements_sha256'], ARTIFACTS.digest(self.requirements))
        self.assertEqual(record['files'], ARTIFACTS.tree_files(self.root / record['directory']))
        self.assertIn('e2b/__init__.py', record['files'])
        self.assertIn('prepared_dependency/__init__.py', record['files'])
        self.assertEqual(record['wheelhouse'], json.loads((self.wheels / 'manifest.json').read_text()))
        self.assertTrue({'--no-index', '--require-hashes', '--find-links'} <= set(commands[0]))
        self.assertEqual(commands[0][commands[0].index('--find-links') + 1], str(self.wheels))
        self.assertTrue({'--only-binary=:all:', '--no-compile', '--ignore-installed', '--isolated'} <= set(commands[0]))
        self.assertTrue({'-I', '-S', '-B'} <= set(commands[1]))
        self.assertFalse(any('__pycache__' in name for name in record['files']))
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.prepare.prepare_demo_sdk(self.root)

    def test_missing_requirements_fails_before_installation(self):
        self.requirements.unlink()
        with patch.object(self.prepare.subprocess, 'run') as run, \
             self.assertRaisesRegex(ValueError, 'requirements'):
            self.prepare.prepare_demo_sdk(self.root)
        run.assert_not_called()

    def test_missing_changed_or_unlocked_wheels_fail_before_installation(self):
        wheel = self.wheels / 'prepared_dependency-1.0-py3-none-any.whl'
        content = wheel.read_bytes()
        for fault in ('missing', 'changed', 'unlocked'):
            with self.subTest(fault=fault):
                if fault == 'missing': wheel.unlink()
                elif fault == 'changed': wheel.write_bytes(content + b'tampered')
                else:
                    wheel.write_bytes(content)
                    (self.wheels / 'unexpected.whl').write_bytes(b'unlocked')
                with patch.object(self.prepare.subprocess, 'run') as run, self.assertRaises(ValueError):
                    self.prepare.prepare_demo_sdk(self.root)
                run.assert_not_called()

    def test_manifest_cannot_hide_missing_transitive_dependency(self):
        path = self.wheels / 'manifest.json'
        manifest = json.loads(path.read_text())
        name = 'prepared_dependency-1.0-py3-none-any.whl'
        (self.wheels / name).unlink()
        del manifest['wheels'][name]
        path.write_text(json.dumps(manifest))
        with patch.object(self.prepare.subprocess, 'run') as run, \
             self.assertRaisesRegex(ValueError, 'incomplete Demo wheel closure'):
            self.prepare.prepare_demo_sdk(self.root)
        run.assert_not_called()

    def test_lock_and_package_versions_are_bound_before_installation(self):
        path = self.wheels / 'manifest.json'
        manifest = json.loads(path.read_text())
        for fault in ('version', 'lock', 'arch'):
            changed = json.loads(json.dumps(manifest))
            if fault == 'version': changed['wheels']['e2b-2.25.1-py3-none-any.whl']['version'] = '0.0.0'
            elif fault == 'lock': changed['lock_sha256'] = '0' * 64
            else: changed['arch'] = 'other'
            path.write_text(json.dumps(changed))
            with self.subTest(fault=fault), patch.object(self.prepare.subprocess, 'run') as run, self.assertRaises(ValueError):
                self.prepare.prepare_demo_sdk(self.root)
            run.assert_not_called()


    def test_install_failure_is_not_replaced_by_host_sdk(self):
        with patch.object(self.prepare.subprocess, 'run',
                          side_effect=subprocess.CalledProcessError(23, ['pip'])) as run, \
             self.assertRaises(subprocess.CalledProcessError) as failure:
            self.prepare.prepare_demo_sdk(self.root)
        self.assertEqual(failure.exception.returncode, 23)
        self.assertEqual(run.call_count, 1)

    def test_missing_installed_sdk_fails_before_import(self):
        with patch.object(self.prepare.subprocess, 'run') as run, \
             self.assertRaisesRegex(ValueError, 'missing e2b'):
            self.prepare.prepare_demo_sdk(self.root)
        self.assertEqual(run.call_count, 1)

    def test_host_pythonpath_cannot_supply_missing_sdk_dependency(self):
        host = self.root / 'host'
        host.mkdir()
        (host / 'host_only_dependency.py').write_text('value = 1\n')
        real_run = subprocess.run
        def incomplete_install(command, **kwargs):
            if 'pip' in command:
                package = self.root / 'fixtures/demo-sdk/e2b'
                package.mkdir(parents=True)
                (package / '__init__.py').write_text('import host_only_dependency\n')
                return subprocess.CompletedProcess(command, 0)
            return real_run(command, capture_output=True, **kwargs)
        with patch.dict(os.environ, {'PYTHONPATH': str(host)}), \
             patch.object(self.prepare.subprocess, 'run', side_effect=incomplete_install), \
             self.assertRaises(subprocess.CalledProcessError) as failure:
            self.prepare.prepare_demo_sdk(self.root)
        self.assertIn(b'host_only_dependency', failure.exception.stderr)

    def test_source_build_fetches_hash_locked_wheels_and_records_their_metadata(self):
        builder = load_module('demo_wheel_builder', ROOT / 'ci/integration/build_demo_wheels.py')
        destination = self.root / 'built-wheels'
        commands = []
        def prepared_download(command, **kwargs):
            commands.append(command)
            for wheel in self.wheels.glob('*.whl'):
                shutil.copyfile(wheel, destination / wheel.name)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(builder.subprocess, 'run', side_effect=prepared_download):
            builder.build(self.requirements.parent, self.arch, destination)
        self.assertTrue({'download', '--only-binary=:all:', '--require-hashes'} <= set(commands[0]))
        self.assertEqual(commands[0][commands[0].index('--requirement') + 1], str(self.requirements.with_suffix('.lock')))
        self.assertEqual(json.loads((destination / 'manifest.json').read_text()),
                         json.loads((self.wheels / 'manifest.json').read_text()))

    def test_source_build_rejects_credentials_before_download(self):
        builder = load_module('demo_wheel_credentials', ROOT / 'ci/integration/build_demo_wheels.py')
        with patch.dict(os.environ, {'GH_TOKEN': 'fixture-token'}), \
             patch.object(builder.subprocess, 'run') as run, self.assertRaisesRegex(ValueError, 'must not receive GH_TOKEN'):
            builder.build(self.requirements.parent, self.arch, self.root / 'built-wheels')
        run.assert_not_called()


class CaseBridgeContracts(unittest.TestCase):
    def test_case_filename_is_the_only_suite_contract(self):
        for name in ('basic.cli.sh', 'storage.cache-membership.sh', 'image.flatten.sh',
                     'network.tap.sh', 'sandbox.lifecycle.sh', 'snapshot.restore.sh',
                     'orchestrator.exec.sh', 'builder.copy.sh', 'telemetry.metrics.sh'):
            self.assertEqual(ARTIFACTS.case_name(name), name)
        for name in ('working-set.smoke.sh', 'density.scale.sh', 'storage.sh',
                     'storage..sh', 'network/case.sh', '../network.case.sh'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                ARTIFACTS.case_name(name)

    def test_suites_span_exact_owner_case_files_and_exclude_obs(self):
        selected = selection(['accelerator'], 'x86_64')
        self.assertEqual(selected['cases'], ['image.guest-fixture.sh', 'image.manifest.sh', 'storage.cache.sh'])
        self.assertEqual(selected['exclusions'][0]['case'], 'storage.obs.sh')
        with self.assertRaisesRegex(ValueError, 'invalid case list'):
            selection(['connector'], 'x86_64', {'connector': ['network.tap.sh', 'network.geneve-ip.sh']})
        with self.assertRaisesRegex(ValueError, 'incomplete test case source set'):
            ARTIFACTS.suite_selection(['connector'], 'x86_64', {'connector': ['network.tap.sh']})

    def test_normalization_is_flat_and_rejects_duplicate_case_ids(self):
        with tempfile.TemporaryDirectory(prefix='kuasar-cases-') as directory:
            root = Path(directory) / 'test/e2e'
            (root / 'lib').mkdir(parents=True)
            for owner, names in CASES.items():
                for name in names:
                    case = root / owner / 'cases' / name
                    case.parent.mkdir(parents=True, exist_ok=True)
                    case.write_text('exit 0\n')
                    case.chmod(0o644)
                helper = root / owner / 'lib/helper.py'
                helper.parent.mkdir()
                helper.write_text(owner)
            expected = {name: owner for owner, names in CASES.items() for name in names}
            self.assertEqual(ARTIFACTS.normalize_e2e_cases(Path(directory), CASES, from_owners=True), expected)
            self.assertEqual((root / 'cases/network.tap.sh').read_text(), 'exit 0\n')
            self.assertEqual((root / 'cases/network.tap.sh').stat().st_mode & 0o777, 0o644)
            self.assertEqual((root / 'lib/connector/helper.py').read_text(), 'connector')
            self.assertFalse(any((root / owner).exists() for owner in CASES))
            self.assertEqual(ARTIFACTS.normalize_e2e_cases(Path(directory), CASES, from_owners=False), expected)
            duplicate = CASES | {'accelerator': ['network.tap.sh']}
            with self.assertRaisesRegex(ValueError, 'duplicate E2E case ID'):
                ARTIFACTS.normalize_e2e_cases(Path(directory), duplicate, from_owners=False)
            (root / 'connector').mkdir()
            with self.assertRaisesRegex(ValueError, 'superseded owner E2E content: connector'):
                ARTIFACTS.normalize_e2e_cases(Path(directory), CASES, from_owners=False)

    def test_resolver_rejects_legacy_missing_and_malformed_source_cases(self):
        resolver = load_module('case_bridge_resolver', ROOT / 'ci/integration/resolve-artifacts.py')
        listing = [
            {'type': 'file', 'name': 'network.tap.sh'},
            {'type': 'file', 'name': 'network.geneve-ip.sh'},
        ]
        legacy = {'type': 'file', 'name': 'run_all.sh'}
        with patch.object(resolver.release, 'api_optional', return_value=legacy) as api, \
             patch.object(resolver.release, 'gh') as gh:
            with self.assertRaises(ValueError):
                resolver.candidate_case_names('kuasar-sandbox/connector', 'a' * 40, 'connector')
        self.assertEqual(api.call_count, 1)
        self.assertEqual(gh.call_count, 0)
        self.assertIn('test/e2e/run_all.sh', api.call_args.args[0])

        directory = subprocess.CompletedProcess(['gh'], 0, json.dumps(listing), '')
        with patch.object(resolver.release, 'api_optional', return_value=None), \
             patch.object(resolver.release, 'gh', return_value=directory) as gh:
            self.assertEqual(resolver.candidate_case_names('kuasar-sandbox/connector', 'a' * 40, 'connector'),
                             ['network.geneve-ip.sh', 'network.tap.sh'])
        self.assertEqual(gh.call_count, 1)
        self.assertIn('contents/test/e2e/cases?ref=' + 'a' * 40, gh.call_args.args[1])

        missing = subprocess.CompletedProcess(['gh'], 1, '', 'gh: Not Found (HTTP 404)')
        with patch.object(resolver.release, 'api_optional', return_value=None), \
             patch.object(resolver.release, 'gh', return_value=missing):
            with self.assertRaises(ValueError):
                resolver.candidate_case_names('kuasar-sandbox/connector', 'a' * 40, 'connector')

        not_directory = subprocess.CompletedProcess(['gh'], 0, json.dumps({'type': 'file'}), '')
        with patch.object(resolver.release, 'api_optional', return_value=None), \
             patch.object(resolver.release, 'gh', return_value=not_directory):
            with self.assertRaisesRegex(ValueError, 'not a directory'):
                resolver.candidate_case_names('kuasar-sandbox/connector', 'a' * 40, 'connector')

        nested = subprocess.CompletedProcess(['gh'], 0, json.dumps([{'type': 'dir', 'name': 'nested'}]), '')
        with patch.object(resolver.release, 'api_optional', return_value=None), \
             patch.object(resolver.release, 'gh', return_value=nested):
            with self.assertRaisesRegex(ValueError, 'flat files'):
                resolver.candidate_case_names('kuasar-sandbox/connector', 'a' * 40, 'connector')

        invalid = subprocess.CompletedProcess(
            ['gh'], 0, json.dumps([{'type': 'file', 'name': 'working-set.smoke.sh'}]), '')
        with patch.object(resolver.release, 'api_optional', return_value=None), \
             patch.object(resolver.release, 'gh', return_value=invalid):
            with self.assertRaisesRegex(ValueError, 'unsupported E2E suite'):
                resolver.candidate_case_names('kuasar-sandbox/connector', 'a' * 40, 'connector')

    def test_privilege_boundary_forwards_only_declared_environment_names(self):
        executor = load_module('execution_boundary', ROOT / 'ci/integration/execution.py')
        command = ['python3', '/prepared/test/e2e/e2e', 'run']
        prepared = {'PATH': '/trusted/bin:/usr/bin', 'TMPDIR': '/var/tmp/private'}
        with patch.object(executor.os, 'geteuid', return_value=1000):
            self.assertEqual(executor.privileged_command(command, prepared),
                             ['sudo', '-n', '--', 'env', 'PATH=/trusted/bin:/usr/bin', 'TMPDIR=/var/tmp/private', *command])
        with patch.object(executor.os, 'geteuid', return_value=0):
            self.assertEqual(executor.privileged_command(command, prepared), command)

    def test_clean_execution_exposes_only_prepared_inputs_and_private_state(self):
        executor = load_module('clean_execution', ROOT / 'ci/integration/execution.py')
        command = executor.clean_command('sha256:' + 'a' * 64, Path('/prepared'), Path('/var/tmp/private'),
                                         ['--arch', 'x86_64', '--suite', 'snapshot'])
        self.assertIn('--pull=never', command)
        self.assertIn('--read-only', command)
        mounts = [command[index + 1] for index, value in enumerate(command) if value == '--mount']
        self.assertEqual(mounts, ['type=bind,src=/prepared,dst=/inputs,readonly',
                                  'type=bind,src=/var/tmp/private,dst=/state',
                                  'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock'])
        for tool in ('go', 'cargo', 'rustc'):
            self.assertIn(tool, command[command.index('-c') + 1])
        with self.assertRaisesRegex(ValueError, 'immutable runtime image'):
            executor.clean_command('ubuntu:latest', Path('/prepared'), Path('/state'), [])

    def test_clean_preparation_preserves_input_boundary_and_output_ownership(self):
        from types import SimpleNamespace
        executor = load_module('clean_prepare', ROOT / 'ci/integration/execution.py')
        with patch.object(executor.os, 'getuid', return_value=1001), \
             patch.object(executor.os, 'getgid', return_value=1001), \
             patch.object(executor.Path, 'stat', return_value=SimpleNamespace(st_gid=123)):
            command = executor.clean_prepare_command('sha256:' + 'a' * 64, Path('/composed/x86_64'),
                                                      Path('/prepared/x86_64'), ['--suite', 'storage'])
        self.assertEqual(command[command.index('--user') + 1], '1001:1001')
        self.assertEqual(command[command.index('--group-add') + 1], '123')
        self.assertIn('type=bind,src=/composed/x86_64,dst=/release,readonly', command)
        self.assertIn('type=bind,src=/prepared,dst=/prepared', command)
        self.assertIn('--workdir /prepared/x86_64', command[command.index('-c') + 1])
        self.assertNotIn('--privileged', command)

    def test_runtime_preflight_rejects_available_language_compilers(self):
        execution = load_module('compiler_preflight', ROOT / 'ci/integration/execution.py')
        with tempfile.TemporaryDirectory(prefix='kuasar-compiler-preflight-') as directory:
            tools = Path(directory) / 'tools'
            tools.mkdir()
            for compiler in ('go', 'cargo', 'rustc', 'cc', 'gcc', 'g++', 'clang', 'clang++'):
                with self.subTest(compiler=compiler):
                    executable = tools / compiler
                    executable.write_text('#!/bin/sh\nexit 0\n')
                    executable.chmod(0o755)
                    result = subprocess.run(
                        ['/bin/sh', '-c', execution.runtime_preflight('/prepared')],
                        env={'PATH': str(tools)}, text=True, capture_output=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('unexpected compiler: ' + compiler, result.stderr)
                    executable.unlink()

    def test_execution_rejects_credentials_before_launching_cases(self):
        executor = load_module('execution_credentials', ROOT / 'ci/integration/execution.py')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, GH_TOKEN='fixture-token'):
            with self.assertRaisesRegex(ValueError, 'must not receive GH_TOKEN'):
                executor.environment(Path(directory))


class ToolPaths(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kuasar-tool-paths-')
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.platform = self.workspace / 'kuasar-sandbox'
        self.script = self.platform / 'ci/integration/ensure-versitygw.sh'
        self.script.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / 'ci/integration/ensure-versitygw.sh', self.script)
        self.binary = self.workspace / 'host-bin'
        self.binary.mkdir()
        for name in ('bash', 'dirname', 'mkdir', 'cp', 'chmod', 'uname', 'python3'):
            (self.binary / name).symlink_to(shutil.which(name))
        self.env = {'PATH': str(self.binary), 'HOME': str(self.workspace),
                    'TARGET_ARCH': 'x86_64', 'PROBE': str(self.workspace / 'probe.json')}
        builder = self.workspace / 'guest-runtime/native-deps/deps/build-versitygw.sh'
        builder.parent.mkdir(parents=True)
        builder.write_text('''#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
import json, os
from pathlib import Path
keys = ('TARGET_ARCH', 'GO_ARCH', 'BINDIR', 'BUILD_DIR', 'TARBALL_CACHE', 'VERSITYGW_SRC')
Path(os.environ['PROBE']).write_text(json.dumps({key: os.environ[key] for key in keys}))
source = Path(os.environ['VERSITYGW_SRC']) / 'aws'
source.mkdir(parents=True)
(source / 'README.md').write_text('[Third-party license](./LICENSE)\\n')
output = Path(os.environ['BINDIR']) / 'versitygw'
output.write_text('#!/bin/sh\\nexit 0\\n')
output.chmod(0o755)
PY
''')

    def run_helper(self, **overrides):
        return subprocess.run(['bash', str(self.script)], cwd=self.workspace,
                              env={**self.env, **overrides}, capture_output=True,
                              text=True, timeout=10)

    def probe(self):
        return json.loads(Path(self.env['PROBE']).read_text())

    def test_cold_defaults_and_document_assembly(self):
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = {'BINDIR': self.platform / 'build/e2e-tools/x86_64',
                    'BUILD_DIR': self.platform / 'build/e2e-tools-build/x86_64',
                    'TARBALL_CACHE': self.platform / 'build/tarball',
                    'VERSITYGW_SRC': self.platform / 'build/src/versitygw'}
        for name, path in expected.items():
            self.assertEqual(self.probe()[name], str(path))
        self.assertFalse((self.platform / 'ci/build').exists())
        (self.script.parent / 'README.md').write_text('# First-party CI guide\n')
        roots = {}
        for owner in ASSEMBLER.OWNERS:
            root = self.platform if owner == 'platform' else self.workspace / owner
            root.mkdir(exist_ok=True)
            (root / 'README.md').write_text('# ' + owner + '\n')
            roots[owner] = root
        output = self.workspace / 'assembled'
        ASSEMBLER.assemble(output, roots, {})
        self.assertTrue((output / 'docs/project/ci/integration/README.md').is_file())
        self.assertFalse(any('versitygw' in str(path) for path in output.rglob('*')))

    def test_explicit_paths_and_architecture_alias(self):
        overrides = {name: str(self.workspace / 'custom' / name.lower())
                     for name in ('BINDIR', 'BUILD_DIR', 'TARBALL_CACHE', 'VERSITYGW_SRC')}
        result = self.run_helper(TARGET_ARCH='arm64', **overrides)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.probe()['TARGET_ARCH'], 'aarch64')
        self.assertEqual(self.probe()['GO_ARCH'], 'arm64')
        for name, value in overrides.items():
            self.assertEqual(self.probe()[name], value)
        self.assertFalse((self.platform / 'build').exists())

    def test_preinstalled_binary_does_not_build(self):
        host = self.binary / 'versitygw'
        host.write_text('#!/bin/sh\nexit 0\n')
        host.chmod(0o755)
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.platform / 'build/e2e-tools/x86_64/versitygw'
        self.assertEqual(target.read_bytes(), host.read_bytes())
        self.assertFalse(Path(self.env['PROBE']).exists())
        host.unlink()
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(Path(self.env['PROBE']).exists())

    def test_explicit_environment_tool_precedes_cached_output(self):
        supplied = self.workspace / 'selected tool'
        supplied.write_text('#!/bin/sh\necho independently-built\n')
        supplied.chmod(0o751)
        old = self.workspace / 'previous environment tool'
        old.write_text('#!/bin/sh\necho do-not-overwrite\n')
        old.chmod(0o751)
        target = self.platform / 'build/e2e-tools/x86_64/versitygw'
        target.parent.mkdir(parents=True)
        target.symlink_to(old)
        result = self.run_helper(E2E_VGW_BIN=str(supplied))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_bytes(), supplied.read_bytes())
        self.assertIn('do-not-overwrite', old.read_text())
        self.assertEqual(supplied.stat().st_mode & 0o777, 0o751)
        self.assertFalse(Path(self.env['PROBE']).exists())

    def test_invalid_explicit_environment_tool_has_no_fallback(self):
        result = self.run_helper(VGW_BIN=str(self.workspace / 'missing'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('environment versitygw is not executable', result.stderr)
        self.assertFalse(Path(self.env['PROBE']).exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
