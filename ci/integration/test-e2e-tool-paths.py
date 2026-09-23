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

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('assemble_docs', ROOT / 'test/e2e/assemble_docs.py')
ASSEMBLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ASSEMBLER)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    def test_candidate_profile_uses_exact_cases_without_run_all(self):
        cases = {'accelerator': ['image.manifest.sh', 'storage.cache.sh', 'storage.obs.sh']}
        profile = ARTIFACTS.profiles(['accelerator'], 'x86_64', cases)
        self.assertEqual(profile['cases'], [
            'test/e2e/accelerator/cases/image.manifest.sh',
            'test/e2e/accelerator/cases/storage.cache.sh',
        ])
        self.assertNotIn('test/e2e/accelerator/run_all.sh', profile['cases'])
        self.assertEqual(profile['exclusions'][0]['case'], 'storage.obs.sh')
        with self.assertRaisesRegex(ValueError, 'candidate case list'):
            ARTIFACTS.profiles(['connector'], 'x86_64',
                               {'connector': ['network.tap.sh', 'network.geneve-ip.sh']})

    def test_normalization_is_flat_and_rejects_duplicate_case_ids(self):
        with tempfile.TemporaryDirectory(prefix='kuasar-case-bridge-') as directory:
            root = Path(directory) / 'test/e2e'
            (root / 'lib').mkdir(parents=True)
            for owner, name in (('connector', 'network.tap.sh'), ('guest-runtime', 'image.flatten.sh')):
                case = root / owner / 'cases' / name
                case.parent.mkdir(parents=True)
                case.write_text('#!/bin/sh\nexit 0\n')
                case.chmod(0o755)
                helper = root / owner / 'lib' / 'helper.py'
                helper.parent.mkdir(parents=True)
                helper.write_text(owner)
            seen = ARTIFACTS.normalize_e2e_cases(Path(directory))
            self.assertEqual(seen, {'image.flatten.sh': 'guest-runtime', 'network.tap.sh': 'connector'})
            self.assertEqual((root / 'cases/network.tap.sh').read_text(), '#!/bin/sh\nexit 0\n')
            self.assertEqual((root / 'lib/connector/helper.py').read_text(), 'connector')
            duplicate = root / 'accelerator/cases/network.tap.sh'
            duplicate.parent.mkdir(parents=True)
            duplicate.write_text('#!/bin/sh\nexit 0\n')
            duplicate.chmod(0o755)
            with self.assertRaisesRegex(ValueError, 'duplicate E2E case ID'):
                ARTIFACTS.normalize_e2e_cases(Path(directory))

    def test_resolver_reads_flat_cases_from_exact_candidate(self):
        resolver = load_module('case_bridge_resolver', ROOT / 'ci/integration/resolve-artifacts.py')
        listing = [
            {'type': 'file', 'name': 'network.tap.sh'},
            {'type': 'file', 'name': 'network.geneve-ip.sh'},
        ]
        with patch.object(resolver.release, 'api_optional', return_value=listing) as api:
            self.assertEqual(resolver.candidate_case_names('kuasar-sandbox/connector', 'a' * 40, 'connector'),
                             ['network.geneve-ip.sh', 'network.tap.sh'])
        self.assertIn('ref=' + 'a' * 40, api.call_args.args[0])
        with patch.object(resolver.release, 'api_optional', return_value=[{'type': 'dir', 'name': 'nested'}]):
            with self.assertRaisesRegex(ValueError, 'flat files'):
                resolver.candidate_case_names('kuasar-sandbox/connector', 'a' * 40, 'connector')
        with patch.object(resolver.release, 'api_optional',
                          return_value=[{'type': 'file', 'name': 'working-set.smoke.sh'}]):
            with self.assertRaisesRegex(ValueError, 'unsupported E2E suite'):
                resolver.candidate_case_names('kuasar-sandbox/connector', 'a' * 40, 'connector')

    def test_executor_sets_prepared_contract_for_rewritten_case(self):
        executor = load_module('case_bridge_executor', ROOT / 'ci/integration/run-artifact-tests.py')
        with tempfile.TemporaryDirectory(prefix='kuasar-case-exec-') as directory:
            workspace = Path(directory) / 'prepared'
            case_name = 'test/e2e/connector/cases/network.tap.sh'
            case = workspace / case_name
            case.parent.mkdir(parents=True)
            (workspace / 'bin').mkdir()
            (workspace / 'test/e2e/lib').mkdir(parents=True)
            case.write_text(
                '#!/bin/sh\nset -eu\n'
                f'[ "$E2E_WORKSPACE" = "{workspace}" ]\n'
                f'[ "$E2E_LIB" = "{workspace}/test/e2e/lib" ]\n'
                '[ "$E2E_ARCH" = x86_64 ]\n'
                'case "$WORK" in /var/tmp/ki-*/cases/network.tap.sh) ;; *) exit 41 ;; esac\n'
                'case "$OUT" in /var/tmp/ki-*/out/network.tap.sh) ;; *) exit 42 ;; esac\n')
            case.chmod(0o755)
            (workspace / 'provenance.json').write_text('{}')
            revisions = ARTIFACTS.release_test_revisions(
                {owner: 'd' * 40 for owner in ARTIFACTS.OWNERS if owner != 'platform'}, 'd' * 40)
            profile = {'cases': [case_name], 'required_products': [], 'exclusions': [], 'name': 'x86-owner-kvm'}
            provenance = {'profile': profile, 'helpers': {}, 'embedded': {'init': 'a' * 64},
                          'test_revisions': revisions}
            plan = {'schema': 1, 'framework_sha': 'a' * 40, 'test_revisions': revisions,
                    'lanes': {'x86_64': {'extra_checks': {}}, 'aarch64': {}}, 'owners': ['connector']}
            result = Path(directory) / 'result.json'
            credentials = {key: '' for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'CALLER_TOKEN', 'KUASAR_CI_APP_PRIVATE_KEY')}
            with patch.object(executor.ARTIFACTS if hasattr(executor, 'ARTIFACTS') else executor.artifacts,
                              'verify_workspace', return_value=provenance), \
                 patch.object(executor.platform, 'machine', return_value='x86_64'), \
                 patch.dict(os.environ, credentials):
                record = executor.execute(plan, 'x86_64', 'core', workspace, result)
            self.assertEqual(record['conclusion'], 'success')
            self.assertEqual(record['timings'][0]['exit_code'], 0)


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
