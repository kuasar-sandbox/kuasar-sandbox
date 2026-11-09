#!/usr/bin/env python3
"""Package a candidate node-infra image after native compilation, before prepare.

This is part of the existing build stage. It uses the same frozen sources and
baseline-plus-delta products; E2E preparation and cases never build an image.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import artifacts


def package(plan, arch, assets, sources, delta):
    artifacts.check_plan(plan)
    if 'node_infra' not in plan:
        return
    for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'CALLER_TOKEN', 'KUASAR_CI_APP_PRIVATE_KEY'):
        artifacts.require(not os.environ.get(name), 'candidate image packaging must not receive ' + name)
    artifacts.require(plan['mode'] == 'source', 'candidate image packaging is only a source build step')
    metadata = json.loads((delta / 'outputs.json').read_text())
    artifacts.require(metadata['plan_id'] == artifacts.identity(plan) and metadata['arch'] == arch,
                      'candidate delta differs from image build plan')
    node = plan['node_infra']
    platform = sources / 'platform'
    actual = subprocess.check_output(['git', '-C', platform, 'rev-parse', 'HEAD'], text=True).strip()
    artifacts.require(actual == node['source_revision'], 'candidate node-infra recipe has another source')
    subprocess.run(['git', '-C', platform, 'diff', '--exit-code', 'HEAD', '--', 'node-infra',
                    'docs/node-infra.md', 'docs/node-infra_zh.md'], check=True)
    helper = sources / '.ci/node-infra-build/node-infra-runtime'
    artifacts.check_architecture(helper, arch)
    artifacts.require(metadata.get('node_infra_helper') == {'sha256': artifacts.digest(helper)},
                      'native node-infra runtime helper differs from compilation output')
    subject = artifacts.node_infra_module()
    with tempfile.TemporaryDirectory(prefix='node-infra-products-') as temporary:
        products = Path(temporary) / 'products'
        products.mkdir()
        seen = {}
        records = {row['name']: row for row in plan['baseline']['assets']}
        for unit, record in plan['baseline']['units'].items():
            name = artifacts.archive_name(unit, record['version'], arch)
            path = assets / name
            artifacts.require(path.stat().st_size == records[name]['size']
                              and 'sha256:' + artifacts.digest(path) == records[name]['digest'],
                              'node-infra baseline product bytes differ: ' + name)
            artifacts.unpack(path, products, unit, seen)
        for name in plan['lanes'][arch]['products']:
            path = delta / 'bin' / name
            artifacts.require(metadata['products'][name]['sha256'] == artifacts.digest(path),
                              'node-infra candidate product bytes differ: ' + name)
            shutil.copy2(path, products / 'bin' / name)
        output = delta / 'node-infra'
        selection = Path(temporary) / 'selection.json'
        selection.write_bytes(artifacts.canonical(node['components']) + b'\n')
        subprocess.run(['python3', '-B', str(platform / 'node-infra/image/build.py'),
                        '--version', node['version'], '--arch', arch, '--products', str(products),
                        '--selection', str(selection), '--output', str(output),
                        '--source-revision', node['source_revision'],
                        '--runtime-helper', str(helper), '--engine', 'docker'], check=True)
        subject.package(platform, output, output, node['version'], node['source_revision'], arches=(arch,))
        subject.validate(output, node['version'], arch, node['source_revision'])
    metadata.pop('node_infra_helper')
    metadata['node_infra'] = {'files': artifacts.tree_files(output)}
    (delta / 'outputs.json').write_bytes(artifacts.canonical(metadata) + b'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--arch', choices=artifacts.ARCHES, required=True)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--delta', type=Path, required=True)
    args = parser.parse_args()
    package(json.loads(args.plan.read_text()), args.arch, args.assets.resolve(), args.sources.resolve(), args.delta.resolve())
