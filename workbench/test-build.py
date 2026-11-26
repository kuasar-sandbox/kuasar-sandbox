#!/usr/bin/env python3
"""Natively build every current component in the workbench as the invoking UID."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci/integration'))
import artifacts
import source_inputs


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def source_build_program(root):
    # PR sources can require release cores whose Stable tags are not published.
    # Use the same selected-source binding as the ordinary component build lane.
    script = shlex.quote(str(root / 'platform/ci/integration/go_workspace.py'))
    source = shlex.quote(str(root))
    platform_source = shlex.quote(str(root / 'platform'))
    return f'''
GOWORK="$(python3 -B {script} --root {source} \\
  --owners accelerator connector guest-runtime sandboxer orchestrator --source-requirements)"
export GOWORK
make -C {platform_source} build
'''


def qualify_products(root, arch, owner_bin, connector_bin):
    """Verify the platform's collected products and fixed native attachment."""
    artifacts.require(root.is_dir() and not root.is_symlink(), f'missing artifact directory: {root}')
    expected = {line.split()[0].removesuffix(':')
                for line in (ROOT / 'release/inputs/bin-inputs.yaml').read_text().splitlines()
                if line.strip() and not line.startswith('#')}
    actual = {path.name for path in root.iterdir()}
    native_layout = 'cluster-runtime' in actual
    artifacts.require(actual == expected | {'lib', 'vpp-runtime.json'}
                      | ({'cluster-runtime', 'run-cluster-ctl'} if native_layout else set()),
                      'full native build product set differs from the platform manifest')
    for name in sorted(expected - ({'cluster-ctl'} if native_layout else set())):
        path = root / name
        artifacts.require(path.is_file() and not path.is_symlink(), f'non-regular platform product: {name}')
    if native_layout:
        for name in ('cluster-ctl', 'run-cluster-ctl'):
            link = root / name
            artifacts.require(link.is_symlink() and os.readlink(link) == 'cluster-runtime/bin/' + name,
                              'invalid collected native link: ' + name)
    native = artifacts.cluster_native_module()
    selected_bundle = native.source_bundle(owner_bin, arch)
    artifacts.require((selected_bundle is not None) == native_layout,
                      'collected cluster-ctl layout differs from the selected owner product')
    result = {}
    if native_layout:
        bundle = root / 'cluster-runtime'
        attachments = native.validate(bundle, arch)
        artifacts.require(attachments is not None, 'collected native product lacks its runtime attachment')
        files = artifacts.tree_files(bundle)
        artifacts.require(set(files) == {native.PROGRAM, *attachments},
                          'collected native bundle contains undeclared files')
        result['cluster_native'] = {'files': files}
    result['products'] = {name: artifacts.digest(root / name) for name in sorted(expected)}
    selected = owner_bin / 'cluster-ctl'
    artifacts.require(selected.is_file() and artifacts.digest(selected) == result['products']['cluster-ctl'],
                      'collected cluster-ctl differs from the selected owner product')
    egress = artifacts.egress_native_module()
    manifest = egress.inspect(root, arch)
    artifacts.require(manifest == egress.inspect(connector_bin, arch),
                      'collected VPP differs from the selected Connector product')
    result['egress_native'] = {'files': {name: artifacts.digest(root / name)
                                       for name in [*manifest['files'], egress.MANIFEST]}}
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
        source_inputs.copy_run_inputs(plan, plan['test_revisions'], sources)
        cases, deps = root / 'empty-cases', root / 'empty-deps'
        cases.mkdir()
        deps.mkdir()
        (deps / 'images.json').write_text('[]\n')
        image_build = module('workbench_image_build', sources / 'platform/workbench/build.py')
        receipt = image_build.build(plan['baseline']['version'], platform.machine(), cases, deps, root / 'image', 2, 6)
        result['build_image'] = receipt
        systems = module('workbench_admin_smoke', sources / 'platform/workbench/test-system.py')
        result['system_startup'] = systems.smoke(receipt['image_id'], root / 'admin-smoke')
        # This toolchain qualification image deliberately has no selected E2E
        # image inputs. Joint release qualification builds the complete image.
        subprocess.run([*launcher, 'start', '--image', receipt['image_id'], '--mode', 'build',
                        '--source', str(sources), '--cpus', '2', '--memory-gib', '6'], check=True)
        program = '''
set -euo pipefail
test "$(id -u)" != 0
test "$(awk '/^CapEff:/ {print $2}' /proc/self/status)" = 0000000000000000
test "$(awk '/^NoNewPrivs:/ {print $2}' /proc/self/status)" = 1
if sudo -n true; then
    echo 'build mode unexpectedly acquired root through sudo' >&2
    exit 1
fi
test ! -e /dev/kvm && test ! -e /dev/net/tun && test ! -S /run/docker.sock
go version
rustc --version
''' + source_build_program(Path('/src'))
        subprocess.run([*launcher, 'exec', '--', 'bash', '-ceu', program], check=True, timeout=9000)
        arch = platform.machine()
        result.update(qualify_products(sources / 'platform/bin' / arch, arch,
                                       sources / 'orchestrator/bin' / arch, sources / 'connector/bin' / arch))
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
    source_inputs.INPUT_ARCHIVE = args.plan.resolve().parent / "source-inputs.tar"
    qualify(json.loads(args.plan.read_text()), args.root.resolve())
