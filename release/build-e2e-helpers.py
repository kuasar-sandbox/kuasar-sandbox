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
sys.path.insert(0, str(ROOT / 'ci/integration'))
import artifacts
import build_helpers
import build_demo_wheels


def build(unit_sources, selected, output, wheels_output, platform_source, arch=None):
    artifacts.require(arch is None or platform.machine() == arch, 'helper package requires the selected native architecture')
    artifacts.require(not output.exists(), 'helper packages need a fresh output')
    source_records = tag_sources.inspect_sources(unit_sources, selected, owner_layout=True)
    tag_sources.require_owner_guides(unit_sources, owner_layout=True)
    pins = tag_sources.owner_revisions(source_records)
    framework_sha = subprocess.check_output(['git', '-C', ROOT, 'rev-parse', 'HEAD'], text=True).strip()
    for owner, unit in tag_sources.OWNER_UNITS.items():
        artifacts.require((tag_sources.source_path(unit_sources, unit, owner_layout=True) / 'go.mod').is_file(),
                          f'missing complete tag helper source: {owner}')
    helpers = {'zot': 'framework', 'versitygw': 'framework', 'custom-proxy': 'orchestrator',
               'telemetry-grpc-probe': 'orchestrator', 'node-ctl-runner-test': 'orchestrator',
               'usage-probe': 'sandboxer',
               'cgroup-fork-probe': 'sandboxer'}
    with tempfile.TemporaryDirectory(prefix='release-helper-sources-') as directory:
        sources = Path(directory) / 'sources'
        # Work in a private copy; source assembly still sees its exact inputs.
        for owner, unit in tag_sources.OWNER_UNITS.items():
            shutil.copytree(tag_sources.source_path(unit_sources, unit, owner_layout=True), sources / owner)
        # Keep private source copy paths out of binaries and cache identities.
        environment = {**os.environ, 'GOWORK': str(sources / 'go.work'),
                       'GOFLAGS': (os.environ.get('GOFLAGS', '') + ' -trimpath').strip()}
        subprocess.run(['go', 'work', 'init', *('./' + owner for owner in sorted(pins))], cwd=sources,
                       env={**environment, 'GOWORK': 'off'}, check=True)
        for arch in ((arch,) if arch else artifacts.ARCHES):
            build_demo_wheels.build(platform_source / 'test/demo', arch, wheels_output / arch)
            selected = dict(helpers)
            destination = output / arch
            build_helpers.build(sources, arch, destination, selected, environment)
            records = {name: {'sha256': artifacts.digest(destination / name),
                             'source_sha': framework_sha if owner == 'framework' else pins[owner]}
                       for name, owner in selected.items()}
            (destination / 'helpers.json').write_bytes(artifacts.canonical(
                {'arch': arch, 'framework_sha': framework_sha, 'test_revisions': pins,
                 'source_records': source_records, 'helpers': records}) + b'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--wheels-output', type=Path, required=True)
    parser.add_argument('--platform-source', type=Path, required=True)
    parser.add_argument('--arch', choices=artifacts.ARCHES, help='build one native architecture')
    args = parser.parse_args()
    build(args.sources.resolve(), selection.resolve_new_selection(args.platform_source, args.version), args.output.resolve(),
          args.wheels_output.resolve(), args.platform_source.resolve(), args.arch)
