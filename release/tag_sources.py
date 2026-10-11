#!/usr/bin/env python3
"""Materialize released units by tag and derive checked source facts locally.

This is release input handling, not candidate admission. Candidate branch/PR
inputs have their own admission and run-artifact transport contract.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import selection

OWNER_UNITS = {owner: 'runtime' if owner == 'guest-runtime' else owner
               for owner in selection.TEST_OWNERS}
NETWORK_TIMEOUT = 120


def require(condition, message):
    if not condition:
        raise ValueError(message)


def repository(unit):
    require(unit in selection.UNITS, 'unknown release unit')
    return 'kuasar-sandbox/' + ('guest-runtime' if unit in ('runtime', 'vmlinux') else unit)


def validate_tag(unit, tag):
    pattern = (selection.RUNTIME_RE if unit == 'runtime' else
               selection.VMLINUX_RE if unit == 'vmlinux' else selection.COMPONENT_RE)
    require(unit in selection.UNITS and isinstance(tag, str) and pattern.fullmatch(tag),
            'released source must be selected by a unit tag')


def git(root, *args):
    # Scope transport controls to this child process; never change user Git or
    # credential configuration. Both discovery and transfer must be bounded.
    environment = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'}
    try:
        return subprocess.check_output(['git', '-C', str(root), *args], text=True,
                                       env=environment, timeout=NETWORK_TIMEOUT).strip()
    except subprocess.TimeoutExpired as error:
        raise ValueError(f'git {args[0]} timed out while checking source inputs') from error
    except subprocess.CalledProcessError as error:
        raise ValueError(f'git {args[0]} failed while checking source inputs') from error


def inspect_unit(root, unit, tag):
    validate_tag(unit, tag)
    require(not root.is_symlink() and (root / '.git').is_dir() and not (root / '.git').is_symlink(),
            f'{unit} requires its materialized tag checkout')
    require(Path(git(root, 'rev-parse', '--show-toplevel')).resolve() == root.resolve(),
            f'{unit} source is not a repository root')
    require(git(root, 'config', '--get', 'remote.origin.url').removesuffix('.git') == f'https://github.com/{repository(unit)}',
            f'{unit} source repository differs from its owner')
    ref = 'refs/tags/' + tag
    require(git(root, 'cat-file', '-t', ref) == 'commit', 'release tag must directly identify a commit')
    sha = git(root, 'rev-parse', ref)
    require(re.fullmatch(r'[0-9a-f]{40}', sha) and git(root, 'rev-parse', 'HEAD') == sha,
            f'{unit} source differs from selected tag {tag}')
    require(not git(root, 'diff', '--no-ext-diff', '--no-textconv', '--name-only', 'HEAD', '--'),
            f'{unit} source has modified tag inputs')
    # Include ignored files: they could otherwise become compiler/helper inputs.
    require(not git(root, 'ls-files', '--others'), f'{unit} source has untracked tag inputs')
    for row in git(root, 'ls-tree', '-r', 'HEAD').splitlines():
        require(row.split()[0] in ('100644', '100755'), f'{unit} source contains a link or submodule')
    return {'repository': repository(unit), 'tag': tag, 'sha': sha,
            'tree': git(root, 'rev-parse', 'HEAD^{tree}')}


def fetch_unit(root, unit, tag, expected_sha):
    """expected_sha is automatically resolved evidence, never a fetch key."""
    validate_tag(unit, tag)
    require(re.fullmatch(r'[0-9a-f]{40}', expected_sha), 'invalid resolved tag identity')
    require(not root.exists() and not root.is_symlink(), 'tag source output must be fresh')
    subprocess.run(['git', 'init', '--quiet', '--template=', str(root)], check=True)
    git(root, 'config', 'core.hooksPath', '/dev/null')
    git(root, 'remote', 'add', 'origin', f'https://github.com/{repository(unit)}.git')
    ref = 'refs/tags/' + tag
    git(root, 'fetch', '--quiet', '--no-tags', '--depth=1', 'origin', f'{ref}:{ref}')
    git(root, 'checkout', '--quiet', '--detach', ref)
    record = inspect_unit(root, unit, tag)
    require(record['sha'] == expected_sha, f'{unit} tag moved while fetching source')
    require(git(root, 'ls-remote', '--refs', 'origin', ref) == f'{expected_sha}\t{ref}',
            f'{unit} tag moved after fetching source')
    return record


def source_path(sources, unit, *, owner_layout=False):
    """The existing compiler workspace names its two guest-runtime trees separately."""
    relative = {'runtime': 'guest-runtime', 'vmlinux': 'kernel-unit/guest-runtime'}.get(unit, unit) if owner_layout else unit
    return sources / relative


def inspect_sources(sources, selected, *, owner_layout=False):
    require(set(selected) == set(selection.UNITS), 'incomplete release unit selection')
    return {unit: inspect_unit(source_path(sources, unit, owner_layout=owner_layout), unit, selected[unit])
            for unit in selection.UNITS}


def owner_revisions(records):
    """Factual helper/test provenance; there is no independent selection input."""
    validate_records(records)
    return {owner: records[unit]['sha'] for owner, unit in OWNER_UNITS.items()}


def validate_records(records):
    require(isinstance(records, dict) and set(records) == set(selection.UNITS), 'incomplete unit source facts')
    for unit, record in records.items():
        require(isinstance(record, dict) and set(record) == {'repository', 'tag', 'sha', 'tree'},
                'invalid unit source facts')
        validate_tag(unit, record['tag'])
        require(record['repository'] == repository(unit) and all(
            isinstance(record[key], str) and re.fullmatch(r'[0-9a-f]{40}', record[key]) for key in ('sha', 'tree')),
            'invalid unit source identity')


def require_owner_guides(sources, *, owner_layout=False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'test/e2e'))
    from package_inputs import guide_inputs
    for owner, unit in OWNER_UNITS.items():
        source = source_path(sources, unit, owner_layout=owner_layout)
        declaration = source / 'release/guide-inputs.txt'
        require(declaration.is_file(), f'{owner} selected tag lacks release/guide-inputs.txt; '
                'first cutover requires a normal new component tag, not newer HEAD inputs')
        guide_inputs(source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    fetch = commands.add_parser('fetch')
    fetch.add_argument('unit', choices=selection.UNITS)
    fetch.add_argument('tag')
    fetch.add_argument('expected_sha', help='automatically resolved identity, used only for comparison')
    fetch.add_argument('output', type=Path)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('platform', type=Path)
    inspect.add_argument('version')
    inspect.add_argument('sources', type=Path)
    inspect.add_argument('--evidence', type=Path, help='compare with previously recorded source facts')
    inspect.add_argument('--owner-revisions', action='store_true', help='emit derived owner SHA facts')
    args = parser.parse_args()
    if args.command == 'fetch':
        records = fetch_unit(args.output, args.unit, args.tag, args.expected_sha)
    else:
        records = inspect_sources(args.sources, selection.resolve_new_selection(args.platform, args.version))
        if args.evidence:
            require(json.loads(args.evidence.read_text()) == records, 'fetched source facts differ from selected tag inputs')
        require_owner_guides(args.sources)
        if args.owner_revisions:
            records = owner_revisions(records)
    print(json.dumps(records, sort_keys=True))


if __name__ == '__main__':
    main()
