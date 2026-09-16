#!/usr/bin/env python3
"""Exercise cold host-tool setup without downloads or preinstalled binaries."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('assemble_docs', ROOT / 'test/e2e/assemble_docs.py')
ASSEMBLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ASSEMBLER)


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


if __name__ == '__main__':
    unittest.main(verbosity=2)
