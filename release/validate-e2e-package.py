#!/usr/bin/env python3
"""Validate flat cases and prebuilt helper bytes without executing archive code."""
import hashlib
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

spec = importlib.util.spec_from_file_location('demo_wheels', Path(__file__).resolve().parents[1] / 'test/e2e/lib/demo_wheels.py')
demo_wheels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo_wheels)


def validate(path, expected_pins=None):
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
        allowed = {'cases', 'lib', 'helpers', 'e2e'}
        for name in members:
            if name.startswith('test/e2e/'):
                artifacts.require(Path(name).parts[2] in allowed, 'superseded owner E2E content in platform archive')
        for name in members:
            if name.startswith('test/e2e/helpers/'):
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
        previous = None
        for arch in artifacts.ARCHES:
            root = f'test/e2e/helpers/{arch}/'
            metadata_path = root + 'helpers.json'
            artifacts.require(metadata_path in members, f'missing prebuilt helper package: {arch}')
            metadata = json.load(archive.extractfile(members[metadata_path]))
            artifacts.require(metadata['arch'] == arch, 'helper package architecture mismatch')
            pins = metadata['test_revisions']
            framework = metadata['framework_sha']
            artifacts.require(set(pins) == set(artifacts.OWNERS) - {'platform'} and
                              all(re.fullmatch(r'[0-9a-f]{40}', pin) for pin in [framework, *pins.values()]),
                              'invalid helper source pins')
            artifacts.require(expected_pins is None or pins == expected_pins, 'helper pins differ from selected test revisions')
            artifacts.require(previous is None or previous == (framework, pins), 'architecture helper source sets differ')
            previous = (framework, pins)
            helpers = metadata['helpers']
            required = {'zot', 'versitygw', 'custom-proxy', 'telemetry-grpc-probe', 'usage-probe'}
            if arch == 'x86_64': required.add('cgroup-fork-probe')
            artifacts.require(set(helpers) == required, 'missing or unexpected prebuilt E2E helper')
            actual = {name.removeprefix(root) for name, entry in members.items() if name.startswith(root) and entry.isfile()}
            artifacts.require(actual == {'helpers.json', *helpers}, 'undeclared helper package files')
            for name, record in helpers.items():
                owner = 'orchestrator' if name in {'custom-proxy', 'telemetry-grpc-probe'} else 'sandboxer'
                revision = framework if name in {'zot', 'versitygw'} else pins[owner]
                artifacts.require(record['source_sha'] == revision, 'helper source identity mismatch')
                member = members[root + name]
                artifacts.require(member.mode & 0o111 and not member.mode & 0o7022, 'invalid helper permissions')
                with archive.extractfile(member) as data:
                    header = data.read(64)
                    artifacts.require(len(header) == 64 and header[:7] == b'\x7fELF\x02\x01\x01' and
                                      struct.unpack_from('<H', header, 18)[0] == {'x86_64': 62, 'aarch64': 183}[arch],
                                      'helper binary architecture mismatch')
                    digest = hashlib.sha256(header)
                    while chunk := data.read(1024 * 1024):
                        digest.update(chunk)
                artifacts.require(digest.hexdigest() == record['sha256'], f'helper checksum mismatch: {name}')


if __name__ == '__main__':
    validate(Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text()) if len(sys.argv) > 2 else None)
