#!/usr/bin/env python3
"""Admit producer branches and dependency tags once; transport fixed inputs."""
import argparse
import json
from pathlib import Path
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ci/integration'))
import artifacts
import source_inputs
import transport


def freeze(unit, source_ref, source_sha, dependencies, output):
    tag_sources = source_inputs.tag_sources
    owner = 'guest-runtime' if unit in ('runtime', 'vmlinux') else unit
    artifacts.require(unit in artifacts.UNITS, 'invalid producer unit')
    with tempfile.TemporaryDirectory(prefix='producer-inputs-') as directory:
        root = Path(directory)
        source = root / 'src' / owner
        row = source_inputs.fetch_named(source, tag_sources.repository(unit), 'refs/heads/' + source_ref, source_sha)
        records = {'src/' + owner: row}
        seen = {owner}
        for dependency, tag, sha in dependencies:
            dependency_owner = 'guest-runtime' if dependency in ('runtime', 'vmlinux') else dependency
            artifacts.require(dependency_owner not in seen, 'duplicate producer source owner')
            seen.add(dependency_owner)
            records['src/' + dependency_owner] = tag_sources.fetch_unit(root / 'src' / dependency_owner, dependency, tag, sha)
        (root / 'producer-records.json').write_bytes(artifacts.canonical({'unit': unit, 'sources': records}) + b'\n')
        with tarfile.open(output, 'x') as archive:
            archive.add(root / 'src', arcname='src')
            archive.add(root / 'producer-records.json', arcname='producer-records.json')


def restore(archive, unit, sha, output, dependencies=()):
    with tempfile.TemporaryDirectory(prefix='producer-receipt-') as directory:
        root = Path(directory) / 'inputs'
        transport.extract(archive, root)
        receipt = json.loads((root / 'producer-records.json').read_text())
        owner = 'guest-runtime' if unit in ('runtime', 'vmlinux') else unit
        artifacts.require(receipt['unit'] == unit and receipt['sources']['src/' + owner]['sha'] == sha,
                          'producer artifact differs from admitted request')
        expected = {'src/' + owner: {'repository': source_inputs.tag_sources.repository(unit), 'sha': sha}}
        for dependency, tag, dependency_sha in dependencies:
            source_inputs.tag_sources.validate_tag(dependency, tag)
            dependency_owner = 'guest-runtime' if dependency in ('runtime', 'vmlinux') else dependency
            key = 'src/' + dependency_owner
            artifacts.require(key not in expected, 'duplicate expected producer owner')
            expected[key] = {'repository': source_inputs.tag_sources.repository(dependency),
                             'tag': tag, 'sha': dependency_sha}
        artifacts.require(set(receipt['sources']) == set(expected), 'producer dependency set differs from preflight')
        for key, facts in expected.items():
            artifacts.require(all(receipt['sources'][key].get(field) == value for field, value in facts.items()),
                              'producer dependency identity differs from preflight: ' + key)
        source_inputs.INPUT_ARCHIVE = archive
        output.mkdir(parents=True, exist_ok=False)
        source_inputs.copy_run_inputs({'run_inputs': receipt['sources']},
                                     {name.removeprefix('src/'): row for name, row in receipt['sources'].items()}, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    make = commands.add_parser('freeze')
    make.add_argument('unit', choices=artifacts.UNITS)
    make.add_argument('source_ref')
    make.add_argument('source_sha')
    make.add_argument('output', type=Path)
    make.add_argument('--dependency', nargs=3, action='append', default=[], metavar=('UNIT', 'TAG', 'SHA'))
    read = commands.add_parser('restore')
    read.add_argument('archive', type=Path)
    read.add_argument('unit', choices=artifacts.UNITS)
    read.add_argument('source_sha')
    read.add_argument('output', type=Path)
    read.add_argument('--dependency', nargs=3, action='append', default=[], metavar=('UNIT', 'TAG', 'SHA'))
    args = parser.parse_args()
    if args.command == 'freeze': freeze(args.unit, args.source_ref, args.source_sha, args.dependency, args.output)
    else: restore(args.archive, args.unit, args.source_sha, args.output, args.dependency)


if __name__ == '__main__': main()
