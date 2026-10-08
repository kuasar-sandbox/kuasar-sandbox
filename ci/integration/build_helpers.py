"""Source-build test helpers before packaging; never called by prepare or run."""
import os
from pathlib import Path
import subprocess

import artifacts

ROOT = Path(__file__).resolve().parents[2]


def build(sources, arch, output, helpers, environment):
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'CALLER_TOKEN', 'KUASAR_CI_APP_PRIVATE_KEY'):
        artifacts.require(not environment.get(key), f'helper build must not receive {key}')
    output.mkdir(parents=True, exist_ok=False)
    def run(command, **extra):
        subprocess.run([str(arg) for arg in command], env={**environment, **extra}, check=True)
    selected = {**environment, 'TARGET_ARCH': arch, 'KUASAR_WORKSPACE_ROOT': str(sources)}
    environment = selected
    if 'versitygw' in helpers:
        run(['bash', ROOT / 'ci/hosted/exact-assets-tools.sh'], KUASAR_E2E_TOOL_OUTPUT=str(output))
    elif 'zot' in helpers:
        run(['bash', ROOT / 'ci/integration/ensure-zot.sh'], BINDIR=str(output))
    if 'custom-proxy' in helpers or 'telemetry-grpc-probe' in helpers:
        run(['bash', sources / 'orchestrator/scripts/ci-e2e-build.sh', 'fixtures', arch, output])
    if 'node-ctl-runner-test' in helpers:
        # Compile, never execute, the test-only runner at the exact owner pin.
        # Retain the selected Go workspace: SDK dependencies belong to this
        # helper source set, not the module versions of an arbitrary checkout.
        run(['go', '-C', sources / 'orchestrator', 'test', '-c', '-trimpath',
             '-o', output / 'node-ctl-runner-test', './cmd/node-ctl'],
            GOOS='linux', GOARCH={'x86_64': 'amd64', 'aarch64': 'arm64'}[arch],
            CGO_ENABLED='0')
    for name, target in (('usage-probe', 'e2e-usage-probe'), ('cgroup-fork-probe', 'e2e-cgroup-fork-probe')):
        if name in helpers:
            run(['make', '-C', sources / 'sandboxer', f'TARGET_ARCH={arch}', f'E2E_FIXTURE_DIR={output}', target])
    artifacts.require(set(artifacts.tree_files(output)) == set(helpers), 'unexpected or missing built helper')
    for name in helpers:
        artifacts.check_architecture(output / name, arch)
        (output / name).chmod(0o755)
