#!/usr/bin/env python3
"""Execute plan/workspace validation inside the runtime YAML's actual sparse trees."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
MODULES = ('release/tag_sources.py', 'release/selection.py', 'test/e2e/package_inputs.py')
SMOKE = r'''
import copy, json
from pathlib import Path
import artifacts
from test_fixtures import CASES, selection

records = {unit: dict(repository='kuasar-sandbox/' + ('guest-runtime' if unit in ('runtime', 'vmlinux') else unit),
    tag=(unit + '-' if unit in ('runtime', 'vmlinux') else '') + 'v1.2.3', sha='c' * 40, tree='d' * 40)
    for unit in artifacts.UNITS}
for mode in ('source', 'exact-assets'):
    plan = dict(schema=2, mode=mode, framework_sha='a' * 40, owners=['accelerator'], case_files=CASES,
        baseline=dict(version='release-v1.2.3', sha='b' * 40, units={unit: dict(repository=r['repository'],
            version=r['tag'], sha=r['sha']) for unit, r in records.items()}, assets=[]),
        lanes={arch: dict(products=[], performance=[], selection=selection(['accelerator'], arch))
               for arch in artifacts.ARCHES}, test_overlays=list(artifacts.OWNERS) if mode == 'source' else [],
        test_revisions=artifacts.release_test_revisions({o: 'c' * 40 for o in artifacts.OWNERS if o != 'platform'}, 'b' * 40),
        source_records=records, platform_source=dict(repository='kuasar-sandbox/kuasar-sandbox', sha='b'*40, tree='e'*40))
    identity = artifacts.check_plan(plan)
    workspace = Path('prepared-' + mode); workspace.mkdir()
    payload = workspace / 'input'; payload.write_text('prepared bytes\n')
    provenance = dict(plan_id=identity, arch='x86_64', files=artifacts.tree_files(workspace),
        modes=artifacts.tree_modes(workspace), selection=plan['lanes']['x86_64']['selection'],
        test_revisions=plan['test_revisions'])
    (workspace / 'provenance.json').write_text(json.dumps(provenance))
    artifacts.verify_workspace(workspace, plan, 'x86_64')
    def rejected(call, message):
        try: call()
        except ValueError as error:
            assert message in str(error), str(error)
        else: raise AssertionError('invalid input accepted: ' + message)
    invalid = copy.deepcopy(plan); invalid['source_records']['runtime']['tree'] = 'invalid'
    rejected(lambda: artifacts.check_plan(invalid), 'invalid unit source identity')
    invalid = copy.deepcopy(plan); invalid['source_records']['runtime']['repository'] = 'kuasar-sandbox/accelerator'
    rejected(lambda: artifacts.check_plan(invalid), 'invalid unit source identity')
    if mode == 'exact-assets':
        invalid = copy.deepcopy(plan); invalid['source_records']['runtime']['tag'] = 'runtime-v1.2.4'
        rejected(lambda: artifacts.check_plan(invalid), 'source records differ')
        invalid = copy.deepcopy(plan); invalid['test_revisions']['guest-runtime']['sha'] = 'f'*40
        rejected(lambda: artifacts.check_plan(invalid), 'test source facts differ')
    payload.write_text('tampered\n')
    rejected(lambda: artifacts.verify_workspace(workspace, plan, 'x86_64'), 'changed after composition')
    payload.write_text('prepared bytes\n'); payload.chmod(0o700)
    rejected(lambda: artifacts.verify_workspace(workspace, plan, 'x86_64'), 'permissions changed')
print('source and exact-assets plan/workspace validation PASS')
'''


class SparseRuntime(unittest.TestCase):
    def test_actual_runtime_checkout_patterns(self):
        jobs = yaml.safe_load((ROOT / '.github/workflows/integration-architecture.yml').read_text())['jobs']
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
        environment.pop('PYTHONPATH', None)
        for job in ('e2e', 'performance'):
            with self.subTest(job=job), tempfile.TemporaryDirectory(prefix='sparse-runtime-') as directory:
                checkout = next(s['with'] for s in jobs[job]['steps'] if 'actions/checkout@' in s.get('uses', ''))
                self.assertIs(checkout['sparse-checkout-cone-mode'], False)
                root = Path(directory) / 'framework'
                subprocess.run(['git', 'clone', '--quiet', '--no-checkout', '--depth=1', ROOT.as_uri(), str(root)],
                               env=environment, check=True, timeout=120)
                subprocess.run(['git', '-C', str(root), 'sparse-checkout', 'set', '--no-cone', '--stdin'],
                               input=checkout['sparse-checkout'], text=True, env=environment, check=True, timeout=30)
                subprocess.run(['git', '-C', str(root), 'checkout', '--quiet'], env=environment, check=True, timeout=30)
                for module in MODULES:
                    self.assertTrue((root / module).is_file(), module)
                for absent in ('components', 'test/e2e/cases', 'test/e2e/platform', 'go.mod', 'release/producer-inputs.py'):
                    self.assertFalse((root / absent).exists(), absent)
                script = root / 'ci/integration/sparse-smoke.py'; script.write_text(SMOKE)
                def run():
                    return subprocess.run([sys.executable, '-B', str(script)], cwd=directory,
                                          env=environment, text=True, capture_output=True, timeout=30)
                result = run(); self.assertEqual(result.returncode, 0, result.stderr)
                for module in MODULES:
                    target = root / module; saved = target.read_bytes(); target.unlink()
                    try:
                        result = run()
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn('ModuleNotFoundError', result.stderr)
                    finally:
                        target.write_bytes(saved)


if __name__ == '__main__':
    unittest.main()
