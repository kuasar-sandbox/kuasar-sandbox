"""Validate and extract product archives without links, unsafe modes or collisions."""
import argparse
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile


def relative_name(member):
    raw = member.name
    # tarfile normalizes directory '/' (and '////') to an empty member name.
    if not raw or raw.startswith('/'):
        raise ValueError(f'unsafe archive path: {raw!r}')
    name = raw.removeprefix('./').rstrip('/')
    if name in ('', '.') and member.isdir():
        return '.'
    if (not name or '\\' in name or any(ord(char) < 32 for char in name)
            or any(part in ('', '.', '..') for part in name.split('/'))):
        raise ValueError(f'unsafe archive path: {raw!r}')
    return name


def inspect(archive, seen, *, on_member=None):
    local = set()
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            name = relative_name(member)
            if (not (member.isfile() or member.isdir()) or member.mode & 0o7022
                    or member.uid != 0 or member.gid != 0):
                raise ValueError(f'unsafe archive type, ownership or permissions: {name}')
            if name == '.' and member.isdir():
                continue
            parts = PurePosixPath(name).parts
            if name in local:
                raise ValueError(f'duplicate archive member: {name}')
            local.add(name)
            kind = 'directory' if member.isdir() else 'file'
            if name in seen and (kind != 'directory' or seen[name] != kind):
                raise ValueError(f'release archives contain the same file path: {name}')
            for i in range(1, len(parts)):
                parent = '/'.join(parts[:i])
                if seen.get(parent) == 'file':
                    raise ValueError(f'archive path crosses a file: {name}')
                seen[parent] = 'directory'
            seen[name] = kind
            if on_member is not None:
                on_member(name, member)
    return seen


def validate_aggregate(selected):
    """Validate one architecture, including reused historical component archives."""
    seen = {}
    init_path = 'bin/sandbox-init'
    for unit, archive in selected:
        def check_init(name, member):
            if name == init_path and (unit not in ('runtime', 'sandboxer')
                    or not member.isfile() or not member.mode & 0o111):
                raise ValueError(f'{unit}: expected executable {init_path} from sandboxer or runtime')
        inspect(archive, seen, on_member=check_init)
    # inspect already rejects duplicates across archives and path collisions.
    if seen.get(init_path) != 'file':
        raise ValueError(f'aggregate lacks required executable {init_path}')


def extract(archives, destination):
    seen = {}
    for archive in archives:
        inspect(archive, seen)
    # Never traverse pre-existing links or replace an installed file.
    destination = destination.absolute()
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        raise ValueError('extraction destination or ancestor is a symlink')
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError('extraction requires an empty, non-symlink directory')
    directories = {}
    for archive in archives:
        with tarfile.open(archive, 'r:gz') as source:
            for member in source:
                if member.isdir():
                    target = destination / relative_name(member)
                    if target in directories and directories[target] != member.mode:
                        raise ValueError(f'conflicting archive directory permissions: {member.name}')
                    directories[target] = member.mode
    destination.mkdir(parents=True, exist_ok=True)
    for target in directories:
        target.mkdir(parents=True, exist_ok=True)
    for archive in archives:
        with tarfile.open(archive, 'r:gz') as source:
            for member in source:
                if not member.isfile():
                    continue
                target = destination / relative_name(member)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as data, target.open('xb') as output:
                    shutil.copyfileobj(data, output)
                target.chmod(member.mode & 0o777)
    # Delay restrictive directory permissions until every file has been written.
    for target in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        target.chmod(directories[target] & 0o777)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='mode', required=True)
    aggregate = commands.add_parser('validate-aggregate')
    aggregate.add_argument('--unit', nargs=2, metavar=('NAME', 'ARCHIVE'), required=True,
                           action='append')
    for mode in ('validate', 'extract'):
        command = commands.add_parser(mode)
        command.add_argument('output', type=Path)
        command.add_argument('archives', nargs='+', type=Path)
    args = parser.parse_args()
    if args.mode == 'validate-aggregate':
        validate_aggregate(args.unit)
    elif args.mode == 'extract':
        extract(args.archives, args.output)
    else:
        seen = json.loads(args.output.read_text()) if args.output.exists() and args.output.stat().st_size else {}
        for archive in args.archives:
            inspect(archive, seen)
        args.output.write_text(json.dumps(seen))
