"""Build the admitted owner inventories; never called by prepare or run."""
from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import artifacts
import go_workspace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'release'))
import helper_inputs
import go_release_consumer


def build_owner(root, arch, output, environment, row, run=None):
    """Execute one verified declaration using the same ABI in builds and source gates."""
    artifacts.require(helper_inputs.owner(root, row['source']) == row,
                      'helper owner changed before recipe')
    declared = helper_inputs.contract.declaration(row['declaration'])
    artifacts.require(output.is_dir() and not output.is_symlink() and not list(output.iterdir()),
                      'helper recipe requires a fresh output directory')
    if declared['recipe'] is not None:
        release_version = environment.get('KUASAR_GO_RELEASE_VERSION')
        if release_version and root.name in go_workspace.DEPENDENCIES:
            selected = json.loads(Path(environment['KUASAR_GO_RELEASE_SELECTION']).read_text())
            dependencies = {name: {'tag': selected[name]['version'], 'sha': selected[name]['sha']}
                            for name in go_workspace.closure([root.name]) if name != root.name}
            environment = go_workspace.bound_helper_environment(
                root.parent, root.name, release_version, dependencies, environment)
        elif root.name in ('sandboxer', 'orchestrator'):
            environment = go_workspace.helper_environment(root.parent, root.name, environment)
        elif (root / 'go.mod').is_file() and root.name != 'platform':
            environment = go_workspace.configure(root.parent, [root.name], environment, adapt_requirements=True)
        command = ['bash', str(root / declared['recipe']), arch, str(output)]
        with ExitStack() as resources:
            # v0.1.13, v0.1.14 and the first v0.2 Preview all selected this
            # exact recipe. It sets GOWORK itself before two Go SDK builds.
            if release_version and root.name == 'platform' and row['recipe_blob'] == '96c0397cf8e2920ceffda4135222433f2b29fd80':
                work = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix='platform-go-recipe-')))
                selected = json.loads(Path(environment['KUASAR_GO_RELEASE_SELECTION']).read_text())
                environment = go_release_consumer.legacy_platform_environment(work, root, output, selected, environment)
            if run is None:
                subprocess.run(command, cwd=root, env=environment, check=True)
            else:
                run(command, root, environment)
    artifacts.require({p.name for p in output.iterdir()} == {r['name'] for r in declared['helpers']},
                      'unexpected or missing built helper')
    return {r['name']: helper_inputs.contract.check_binary(output / r['name'], arch)
            for r in declared['helpers']}


def build(sources, arch, output, helpers, environment, contract):
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'CALLER_TOKEN', 'KUASAR_CI_APP_PRIVATE_KEY'):
        artifacts.require(not environment.get(key), f'helper build must not receive {key}')
    inventory = helper_inputs.contract.validate(contract)
    artifacts.require(arch in artifacts.ARCHES and set(helpers) <= set(inventory) and
                      helpers == {name: inventory[name]['owner'] for name in helpers}, 'unplanned helper build request')
    roots = {owner: sources / owner for owner in contract['owners']}
    helper_inputs.verify_sources(contract, roots)
    output.mkdir(parents=True, exist_ok=False)
    selected = dict(environment, TARGET_ARCH=arch, KUASAR_WORKSPACE_ROOT=str(sources))
    for owner in sorted({inventory[name]['owner'] for name in helpers}):
        declared = helper_inputs.contract.declaration(contract['owners'][owner]['declaration'])
        with tempfile.TemporaryDirectory(prefix='owner-helpers-') as directory:
            built = Path(directory) / 'bin'; built.mkdir()
            build_owner(roots[owner], arch, built, selected, contract['owners'][owner])
            # Full declaration, not producer metadata, defines all admissible outputs.
            names = {item['name'] for item in declared['helpers']}
            helper_inputs.contract.receipt(built, arch, contract, names)
            for name in names & set(helpers):
                shutil.copy2(built / name, output / name)
    helper_inputs.contract.receipt(output, arch, contract, helpers)
