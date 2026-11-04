#!/usr/bin/env python3
"""Validate flat cases and prebuilt helper bytes without executing archive code."""
import hashlib
import argparse
import importlib.util
import json
import re
from pathlib import Path
import struct
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ci/integration'))
import artifacts
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tag_sources
import helper_inputs

spec = importlib.util.spec_from_file_location('demo_wheels', Path(__file__).resolve().parents[1] / 'test/e2e/lib/demo_wheels.py')
demo_wheels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo_wheels)


def validate(path, expected_contract=None, *, publication=False):
    with tarfile.open(path, 'r:gz') as archive:
        members = {}
        for entry in archive.getmembers():
            name = entry.name.removeprefix('./').rstrip('/')
            if not name or name == '.':
                artifacts.require(entry.isdir(), 'invalid archive root')
                continue
            artifacts.relative(name)
            artifacts.require(name not in members and (entry.isfile() or entry.isdir()), 'duplicate or non-regular platform entry')
            members[name] = entry
        cases = [name for name in members if name.startswith('test/e2e/cases/')]
        artifacts.require(cases, 'platform archive has no product cases')
        for name in cases:
            artifacts.case_name(name.removeprefix('test/e2e/cases/'))
            artifacts.require(members[name].isfile(), 'product case is not a file')
        artifacts.capabilities_module().packaged(
            lambda name: archive.extractfile(members[name]).read(),
            {name for name, entry in members.items() if entry.isfile()})
        artifacts.validate_e2e_layout(
            {name.removeprefix('test/e2e/'): entry.mode for name, entry in members.items()
             if name.startswith('test/e2e/') and entry.isfile()},
            (name.removeprefix('test/e2e/') for name, entry in members.items()
             if name.startswith('test/e2e/') and entry.isdir()))
        for name, entry in members.items():
            if name.startswith('test/e2e/helpers/'):
                artifacts.require(not entry.mode & 0o7022, 'unsafe helper package permissions')
                if name != 'test/e2e/helpers/contract.json':
                    artifacts.require(Path(name).parts[3] in artifacts.ARCHES, 'unknown helper architecture')
        if 'test/e2e/cases/basic.demo.sh' in cases:
            # Validate with the controller's code, never with code from the archive.
            with tempfile.TemporaryDirectory(prefix='release-demo-wheels-') as directory:
                demo = Path(directory)
                for name, entry in members.items():
                    if name in {'test/demo/requirements.txt', 'test/demo/requirements.lock'} or name.startswith('test/demo/wheels/'):
                        if entry.isfile():
                            target = demo / name.removeprefix('test/demo/')
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(archive.extractfile(entry).read())
                for arch in artifacts.ARCHES:
                    demo_wheels.validate(demo / 'wheels' / arch, demo / 'requirements.lock', demo / 'requirements.txt', arch)
        if publication:
            artifacts.require('test/e2e/helpers/contract.json' not in members, 'new package contains helper source records')
        previous = None
        with tempfile.TemporaryDirectory(prefix='release-helpers-') as directory:
            root = Path(directory)
            for name, entry in members.items():
                if name.startswith('test/e2e/helpers/') and entry.isfile():
                    path = root / name.removeprefix('test/e2e/helpers/')
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(archive.extractfile(entry).read())
                    path.chmod(entry.mode)
            if publication:
                helper_inputs.contract.packages(root, runtime_only=True)
            for arch in artifacts.ARCHES:
                contract, records = helper_inputs.contract.package(root / arch, arch)
                helper_inputs.contract.selected(contract, [name.removeprefix('test/e2e/cases/') for name in cases])
                artifacts.require(previous is None or previous == contract, 'architecture helper inventories differ')
                artifacts.require(expected_contract is None or contract == helper_inputs.contract.runtime(expected_contract),
                                  'packaged runtime helper inventory differs from selected inputs')
                previous = contract



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--publication', action='store_true')
    args = parser.parse_args()
    validate(args.archive, publication=args.publication)
