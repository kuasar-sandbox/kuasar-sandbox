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
            subprocess.run(['bash', str(roots[owner] / declared['recipe']), arch, str(built)],
                           cwd=roots[owner], env=selected, check=True)
            # Full declaration, not producer metadata, defines all admissible outputs.
            names = {item['name'] for item in declared['helpers']}
            helper_inputs.contract.receipt(built, arch, contract, names)
            for name in names & set(helpers):
                shutil.copy2(built / name, output / name)
    helper_inputs.contract.receipt(output, arch, contract, helpers)
