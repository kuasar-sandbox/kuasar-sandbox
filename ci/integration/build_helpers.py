"""Build the admitted owner inventories; never called by prepare or run."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import artifacts

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'release'))
import helper_inputs


def build_owner(root, arch, output, environment, row, run=None):
    """Execute one verified declaration using the same ABI in builds and source gates."""
    artifacts.require(helper_inputs.owner(root, row['source']) == row,
                      'helper owner changed before recipe')
    declared = helper_inputs.contract.declaration(row['declaration'])
    artifacts.require(output.is_dir() and not output.is_symlink() and not list(output.iterdir()),
                      'helper recipe requires a fresh output directory')
    if declared['recipe'] is not None:
        command = ['bash', str(root / declared['recipe']), arch, str(output)]
        if run is None:
            subprocess.run(command, cwd=root, env=environment, check=True)
        else:
            run(command, root, environment)
    artifacts.require({p.name for p in output.iterdir()} == {r['name'] for r in declared['helpers']},
                      'unexpected or missing built helper')
    return {r['name']: helper_inputs.contract.check_binary(output / r['name'], arch)
            for r in declared['helpers']}


def build(sources, arch, output, helpers, environment, contract, historical_archive=None):
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'CALLER_TOKEN', 'KUASAR_CI_APP_PRIVATE_KEY'):
        artifacts.require(not environment.get(key), f'helper build must not receive {key}')
    inventory = helper_inputs.contract.validate(contract)
    artifacts.require(arch in artifacts.ARCHES and set(helpers) <= set(inventory) and
                      helpers == {name: inventory[name]['owner'] for name in helpers}, 'unplanned helper build request')
    roots = {owner: sources / owner for owner in contract['owners']}
    helper_inputs.verify_sources(contract, roots)
    output.mkdir(parents=True, exist_ok=False)
    selected = dict(environment, TARGET_ARCH=arch, KUASAR_WORKSPACE_ROOT=str(sources))
    historical_names = {name for name in helpers if inventory[name].get('historical')}
    if historical_names:
        import helper_history
        import tarfile
        artifacts.require(historical_archive is not None, 'missing authenticated historical helper archive')
        observed, metadata = helper_history.inventory(historical_archive, contract['history']['plan'],
            {owner: row['source'] for owner, row in contract['owners'].items() if owner != 'platform'})
        artifacts.require(observed == contract['history']['helpers'] and metadata == contract['history']['metadata'],
                          'historical helper inventory changed after admission')
        with tarfile.open(historical_archive, 'r:gz') as archive:
            entries = {member.name.removeprefix('./'): member for member in archive if member.isfile()}
            for name in historical_names:
                member = entries['test/e2e/helpers/' + arch + '/' + name]
                with archive.extractfile(member) as stream, (output / name).open('xb') as target:
                    shutil.copyfileobj(stream, target)
                (output / name).chmod(member.mode)
    for owner in sorted({inventory[name]['owner'] for name in set(helpers) - historical_names}):
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
