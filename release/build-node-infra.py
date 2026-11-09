#!/usr/bin/env python3
"""Build one native node-infra OCI asset from the selected aggregate products."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import archive_paths
import node_infra_assets as node
import selection
import tag_sources

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ci/integration'))
import artifacts


def build(platform, components, sources, helper, version, arch, output):
    for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'CALLER_TOKEN', 'KUASAR_CI_APP_PRIVATE_KEY'):
        node.require(not os.environ.get(name), 'node-infra image build must not receive ' + name)
    selected = selection.resolve_new_selection(platform, version)
    records = tag_sources.inspect_sources(sources, selected)
    node.require(selection.delivery(selection.resolve_config(platform, version), str(platform))
                 == selection.NODE_INFRA_DELIVERY, 'aggregate does not declare node-infra delivery')
    node.require_config_sources(sources)
    revision = tag_sources.git(platform, 'rev-parse', 'HEAD')
    units = {unit: {'version': selected[unit], 'repository': row['repository'], 'sha': row['sha']}
             for unit, row in records.items()}
    helper_record = json.loads((helper.parent / 'runtime.json').read_text())
    node.require(helper_record == {'arch': arch, 'sha256': artifacts.digest(helper), 'components': units,
                                   'source_revision': revision}, 'node-infra runtime helper differs from selected sources')
    artifacts.check_architecture(helper, arch)
    archives = []
    for unit, tag in selected.items():
        name = artifacts.archive_name(unit, tag, arch)
        path = components / unit / name
        assets = json.loads((components / unit / 'assets.json').read_text())
        expected = [row for row in assets if row['name'] == name]
        node.require(len(expected) == 1 and path.is_file() and not path.is_symlink()
                     and path.stat().st_size == expected[0]['size']
                     and 'sha256:' + node.digest(path) == expected[0]['digest'], 'node-infra product asset differs: ' + name)
        archives.append(path)
    with tempfile.TemporaryDirectory(prefix='node-infra-release-') as temporary:
        root = Path(temporary)
        archive_paths.extract(archives, root / 'products')
        selected_file = root / 'selection.json'
        selected_file.write_bytes(node.canonical(units) + b'\n')
        subprocess.run(['python3', '-B', str(platform / 'node-infra/image/build.py'),
                        '--version', version, '--arch', arch, '--products', str(root / 'products'),
                        '--selection', str(selected_file), '--output', str(output),
                        '--source-revision', revision, '--runtime-helper', str(helper), '--engine', 'docker'], check=True)
    node.validate(output, version, arch, revision)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', type=Path, required=True)
    parser.add_argument('--components', type=Path, required=True)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--runtime-helper', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--arch', choices=node.ARCHES, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.platform.resolve(), args.components.resolve(), args.sources.resolve(), args.runtime_helper.resolve(),
          args.version, args.arch, args.output.resolve())
