#!/usr/bin/env python3
"""Natively build every current component in the workbench as the invoking UID."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci/integration'))
import artifacts


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def qualify(plan, root):
    artifacts.check_plan(plan)
    artifacts.require(os.getuid() != 0, 'native build qualification must run as an ordinary UID')
    artifacts.require(subprocess.check_output(['git', '-C', ROOT, 'rev-parse', 'HEAD'], text=True).strip()
                      == plan['framework_sha'], 'native qualification framework differs from the admitted plan')
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'CALLER_TOKEN', 'KUASAR_CI_APP_PRIVATE_KEY'):
        artifacts.require(not os.environ.get(key), f'build qualification must not receive {key}')
    root.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = {'plan_id': artifacts.identity(plan), 'arch': platform.machine(), 'uid': os.getuid(),
              'framework_sha': plan['framework_sha'], 'source_revisions': plan['test_revisions'], 'conclusion': 'failure'}
    launcher = [sys.executable, '-B', str(ROOT / 'workbench/workbench'), '--root', str(root / 'instances'), '--name', 'native-build']
    try:
        # The full source build qualifies toolchain completeness against the
        # exact current test revisions. Its binaries never enter release assets.
        owner_build = module('workbench_owner_build', ROOT / 'ci/integration/build-artifacts.py')
        sources = root / 'sources'
        sources.mkdir()
        for owner, record in plan['test_revisions'].items():
            owner_build.checkout(record['repository'], record['sha'], sources / owner)
        cases, deps = root / 'empty-cases', root / 'empty-deps'
        cases.mkdir()
        deps.mkdir()
        (deps / 'images.json').write_text('[]\n')
        image_build = module('workbench_image_build', sources / 'platform/workbench/build.py')
        receipt = image_build.build(plan['baseline']['version'], platform.machine(), cases, deps, root / 'image', 2, 6)
        result['build_image'] = receipt
        if platform.machine() == 'x86_64':
            # Ubuntu hosted KVM availability is independent of this required
            # enforcing-AppArmor check. Full KVM/E2E acceptance remains separate.
            subprocess.run([sys.executable, '-B', sources / 'platform/workbench/test-apparmor.py',
                            '--image', receipt['image_id'], '--root', str(root / 'apparmor')], check=True, timeout=600)
            result['apparmor'] = json.loads((root / 'apparmor/result.json').read_text())
        # This toolchain qualification image deliberately has no selected E2E
        # image inputs. Joint release qualification builds the complete image.
        subprocess.run([*launcher, 'start', '--image', receipt['image_id'], '--mode', 'build',
                        '--source', str(sources), '--cpus', '2', '--memory-gib', '6'], check=True)
        program = '''
set -euo pipefail
test "$(id -u)" != 0
test "$(awk '/^CapEff:/ {print $2}' /proc/self/status)" = 0000000000000000
test ! -e /dev/kvm && test ! -e /dev/net/tun && test ! -S /run/docker.sock
go version
rustc --version
go work init ./accelerator ./connector ./guest-runtime ./sandboxer ./orchestrator
make -C /src/platform build
'''
        subprocess.run([*launcher, 'exec', '--', 'bash', '-ceu', program], check=True, timeout=9000)
        result['products'] = artifacts.tree_files(sources / 'platform/bin' / platform.machine())
        expected = [line.split()[1] for line in (ROOT / 'release/bin-inputs.manifest').read_text().splitlines()
                    if line.strip() and not line.startswith('#')]
        artifacts.require(set(result['products']) == set(expected), 'full native build did not produce every platform binary')
        result['conclusion'] = 'success'
    finally:
        result['wall_seconds'] = time.monotonic() - started
        if (root / 'instances/native-build/instance.json').exists():
            try:
                subprocess.run([*launcher, 'cleanup'], check=True, timeout=420)
            except Exception as error:
                result.update(conclusion='failure', cleanup_error=str(error))
        (root / 'result.json').write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    artifacts.require(result['conclusion'] == 'success', 'native build qualification failed; inspect result.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--root', required=True, type=Path, help='new task directory outside the framework checkout')
    args = parser.parse_args()
    qualify(json.loads(args.plan.read_text()), args.root.resolve())
