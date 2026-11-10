#!/usr/bin/env python3
"""Build helper packages from the same selected unit tags as the products."""
import argparse
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'release'))
import selection
import tag_sources
import helper_inputs
sys.path.insert(0, str(ROOT / 'ci/integration'))
import artifacts
import build_helpers
import build_demo_wheels
import node_infra_assets


def build(unit_sources, selected, output, wheels_output, platform_source, arch=None, node_infra_output=None, node_infra=False):
    artifacts.require(arch is None or platform.machine() == arch, 'helper package requires the selected native architecture')
    artifacts.require(not output.exists(), 'helper packages need a fresh output')
    source_records = tag_sources.inspect_sources(unit_sources, selected, owner_layout=True)
    component_selection = {unit: {'version': selected[unit], 'repository': record['repository'], 'sha': record['sha']}
                           for unit, record in source_records.items()}
    tag_sources.require_owner_guides(unit_sources, owner_layout=True)
    for owner, unit in tag_sources.OWNER_UNITS.items():
        artifacts.require((tag_sources.source_path(unit_sources, unit, owner_layout=True) / 'go.mod').is_file(),
                          f'missing complete tag helper source: {owner}')
    roots = {owner: tag_sources.source_path(unit_sources, unit, owner_layout=True)
             for owner, unit in tag_sources.OWNER_UNITS.items()}
    roots['platform'] = platform_source
    import source_inputs
    identities = {owner: source_records[unit] for owner, unit in tag_sources.OWNER_UNITS.items()}
    identities['platform'] = source_inputs.inspect_platform(platform_source,
        tag_sources.git(platform_source, 'rev-parse', 'HEAD'))
    contract = helper_inputs.discover(roots, identities)
    helpers = {name: row['owner'] for name, row in helper_inputs.contract.validate(contract).items()}
    with tempfile.TemporaryDirectory(prefix='release-helper-sources-') as directory:
        sources = Path(directory) / 'sources'
        # Work in a private copy; source assembly still sees its exact inputs.
        for owner, unit in tag_sources.OWNER_UNITS.items():
            shutil.copytree(tag_sources.source_path(unit_sources, unit, owner_layout=True), sources / owner)
        shutil.copytree(platform_source, sources / 'platform')
        # Keep private source copy paths out of binaries and cache identities.
        environment = {**os.environ, 'GOWORK': 'off',
                       'GOFLAGS': (os.environ.get('GOFLAGS', '') + ' -trimpath').strip()}
        for arch in ((arch,) if arch else artifacts.ARCHES):
            build_demo_wheels.build(platform_source / 'test/demo', arch, wheels_output / arch)
            selected = dict(helpers)
            destination = output / arch
            build_helpers.build(sources, arch, destination, selected, environment, contract)
            records = helper_inputs.contract.receipt(destination, arch, contract)
            (destination / 'helpers.json').write_bytes(artifacts.canonical(
                {'schema': 2, 'arch': arch, 'helpers': records}) + b'\n')
            if node_infra_output is not None:
                node_infra_output.mkdir(parents=True, exist_ok=True)
                if node_infra:
                    node_infra_assets.require_config_sources(sources)
                    native = node_infra_output / arch
                    native.mkdir()
                    selected_file = native / 'selection.json'
                    selected_file.write_bytes(artifacts.canonical(component_selection) + b'\n')
                    runtime = native / 'node-infra-runtime'
                    subprocess.run(['python3', '-B', str(platform_source / 'node-infra/runtime/build.py'),
                                    '--sources', str(sources), '--selection', str(selected_file), '--arch', arch,
                                    '--output', str(runtime)] + (['--test'] if arch == platform.machine() else []),
                                   env=environment, check=True)
                    artifacts.check_architecture(runtime, arch)
                    (native / 'runtime.json').write_bytes(artifacts.canonical({
                        'arch': arch, 'sha256': artifacts.digest(runtime), 'components': component_selection,
                        'source_revision': identities['platform']['sha']}) + b'\n')



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--wheels-output', type=Path, required=True)
    parser.add_argument('--platform-source', type=Path, required=True)
    parser.add_argument('--arch', choices=artifacts.ARCHES, help='build one native architecture')
    parser.add_argument('--node-infra-output', type=Path, help='native runtime helper output for the aggregate system image')
    args = parser.parse_args()
    build(args.sources.resolve(), selection.resolve_new_selection(args.platform_source, args.version), args.output.resolve(),
          args.wheels_output.resolve(), args.platform_source.resolve(), args.arch, args.node_infra_output,
          selection.delivery(selection.resolve_config(args.platform_source, args.version), str(args.platform_source))
          in selection.NODE_INFRA_DELIVERIES)
