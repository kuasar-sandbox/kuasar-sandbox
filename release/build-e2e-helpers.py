#!/usr/bin/env python3
"""Build both helper packages from the independently pinned test sources."""
import argparse
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci/integration'))
import artifacts
import build_helpers
import build_demo_wheels


def build(test_sources, pins, output, wheels_output, platform_source, arch=None):
    artifacts.require(arch is None or platform.machine() == arch, 'helper package requires the selected native architecture')
    artifacts.require(not output.exists(), 'helper packages need a fresh output')
    artifacts.require(set(pins) == set(artifacts.OWNERS) - {'platform'}, 'incomplete helper test revisions')
    framework_sha = subprocess.check_output(['git', '-C', ROOT, 'rev-parse', 'HEAD'], text=True).strip()
    for owner in pins:
        artifacts.require((test_sources / owner / 'go.mod').is_file(), f'missing complete pinned helper source: {owner}')
    helpers = {'zot': 'framework', 'versitygw': 'framework', 'custom-proxy': 'orchestrator',
               'telemetry-grpc-probe': 'orchestrator', 'node-ctl-runner-test': 'orchestrator',
               'usage-probe': 'sandboxer',
               'cgroup-fork-probe': 'sandboxer'}
    with tempfile.TemporaryDirectory(prefix='release-helper-sources-') as directory:
        sources = Path(directory) / 'sources'
        # Work in a private copy; source assembly still sees its exact inputs.
        shutil.copytree(test_sources, sources)
        # Pinned owner recipes may omit -trimpath. Private copy names must not
        # enter their binaries or defeat reuse of the same compiler inputs.
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
                {'arch': arch, 'framework_sha': framework_sha, 'test_revisions': pins, 'helpers': records}) + b'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--test-revisions', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--wheels-output', type=Path, required=True)
    parser.add_argument('--platform-source', type=Path, required=True)
    parser.add_argument('--arch', choices=artifacts.ARCHES, help='build one native architecture')
    args = parser.parse_args()
    build(args.sources.resolve(), json.loads(args.test_revisions.read_text()), args.output.resolve(),
          args.wheels_output.resolve(), args.platform_source.resolve(), args.arch)
