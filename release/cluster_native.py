"""Admit the fixed cluster-ctl native closure without executing product code.

The launcher, private libraries and redistribution material belong to the same
product/source selection as cluster-ctl. Hashes and modes are trusted framework
inputs, not claims made by a component archive or its build scripts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile

LOCK = json.loads((Path(__file__).resolve().parents[1] / 'ci/integration/cluster-native-runtime.json').read_text())
PREFIXES = ('lib/cluster-ctl', 'share/licenses/orchestrator/native')
LAUNCHER = 'bin/run-cluster-ctl'
PROGRAM = 'bin/cluster-ctl'
MANIFEST = PREFIXES[1] + '/runtime.json'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def attached(name):
    return name == LAUNCHER or any(name == prefix or name.startswith(prefix + '/') for prefix in PREFIXES)


def digest(stream):
    checksum = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        checksum.update(block)
    return checksum.hexdigest()


def needs_runtime(stream):
    """Read DT_NEEDED as data; legacy static products need no native payload."""
    header = stream.read(64)
    if len(header) < 64 or header[:6] != b'\x7fELF\x02\x01':
        return False  # The ordinary product architecture gate owns this error.
    offset, = struct.unpack_from('<Q', header, 32)
    size, count = struct.unpack_from('<HH', header, 54)
    if not count:
        return False
    require(size >= 56 and count < 4096, 'invalid cluster-ctl ELF program headers')
    segments = []
    dynamic = None
    for i in range(count):
        stream.seek(offset + i * size)
        row = stream.read(56)
        require(len(row) == 56, 'truncated cluster-ctl ELF program headers')
        kind, _, start, address, _, length, _, _ = struct.unpack('<IIQQQQQQ', row)
        if kind == 1:
            segments.append((address, start, length))
        elif kind == 2:
            dynamic = (start, length)
    if dynamic is None:
        return False
    start, length = dynamic
    require(length <= 1024 * 1024 and length % 16 == 0, 'invalid cluster-ctl ELF dynamic table')
    stream.seek(start)
    table = stream.read(length)
    require(len(table) == length, 'truncated cluster-ctl ELF dynamic table')
    needed, strings = [], None
    for tag, value in struct.iter_unpack('<QQ', table):
        if tag == 0:
            break
        if tag == 1:
            needed.append(value)
        elif tag == 5:
            strings = value
    if not needed:
        return False
    require(strings is not None, 'cluster-ctl ELF has no dynamic string table')
    for address, start, length in segments:
        if address <= strings < address + length:
            for entry in needed:
                require(strings + entry < address + length, 'invalid cluster-ctl ELF dependency')
                stream.seek(start + strings - address + entry)
                name = stream.read(min(256, address + length - strings - entry)).split(b'\0', 1)[0]
                if name == b'libfdb_c.so':
                    return True
            return False
    raise ValueError('cluster-ctl ELF dynamic strings are not file backed')


def inspect_elf(path, arch):
    with path.open('rb') as stream:
        header = stream.read(20)
    require(len(header) == 20 and header[:6] == b'\x7fELF\x02\x01'
            and int.from_bytes(header[18:20], 'little') == {'x86_64': 62, 'aarch64': 183}[arch],
            'wrong native ELF architecture: ' + str(path))
    def readelf(option):
        return subprocess.check_output(['readelf', option, str(path)], text=True,
                                       env={**os.environ, 'LC_ALL': 'C'})
    dynamic = readelf('-d')
    definitions, requirements, section, provider = set(), {}, '', None
    for line in readelf('-V').splitlines():
        if line.startswith('Version definition section'):
            section = 'definitions'
        elif line.startswith('Version needs section'):
            section = 'requirements'
        elif line.startswith('Version symbols section'):
            section = 'symbols'
        if section == 'definitions':
            definitions.update(re.findall(r'Name: (\S+)', line))
        elif section == 'requirements':
            match = re.search(r'File: (\S+)', line)
            if match:
                provider = match.group(1)
            match = re.search(r'Name: (\S+)', line)
            if match and provider:
                requirements.setdefault(provider, set()).add(match.group(1))
    return {'needed': re.findall(r'\(NEEDED\).*\[([^]]+)\]', dynamic),
            'sonames': re.findall(r'\(SONAME\).*\[([^]]+)\]', dynamic),
            'definitions': definitions, 'requirements': requirements}


def validate_abi(root, arch, files):
    providers, objects = {}, []
    for name in files:
        if not name.startswith('lib/'):
            continue
        obj = inspect_elf(root / name, arch)
        objects.append(obj)
        for alias in [Path(name).name, *obj['sonames']]:
            require(alias not in providers or providers[alias] is obj, 'duplicate native SONAME: ' + alias)
            providers[alias] = obj
    program = inspect_elf(root / PROGRAM, arch)
    require('libfdb_c.so' in program['needed'], 'cluster-ctl does not link the FoundationDB client')
    for obj in [*objects, program]:
        require(set(obj['needed']) <= set(providers), 'unbundled cluster-ctl native dependency')
        for name, versions in obj['requirements'].items():
            require(name in providers and versions <= providers[name]['definitions'],
                    'cluster-ctl native ABI not supplied by bundle: ' + name)


def validate(root, arch=None):
    """Return the verified attachment digest map, or None for a legacy product."""
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), 'native product root is not a regular directory')
    present = any((root / name).exists() or (root / name).is_symlink() for name in (*PREFIXES, LAUNCHER))
    if not present:
        if (root / PROGRAM).is_file():
            with (root / PROGRAM).open('rb') as stream:
                require(not needs_runtime(stream), 'FoundationDB cluster-ctl is missing its native runtime')
        return None
    for prefix in ('bin', *PREFIXES):
        path = root / prefix
        for parent in (path, *path.parents):
            if parent == root:
                break
            require(not parent.is_symlink(), 'native runtime crosses a symlink: ' + prefix)
    manifest = root / MANIFEST
    require(manifest.is_file() and not manifest.is_symlink() and manifest.stat().st_size < 65536,
            'missing or invalid cluster-ctl native manifest')
    selected_arch = json.loads(manifest.read_text()).get('architecture')
    require(selected_arch in LOCK['architectures'] and (arch is None or selected_arch == arch),
            'cluster-ctl native runtime architecture mismatch')
    arch = selected_arch
    expected = LOCK['architectures'][arch]['files']
    actual = {LAUNCHER}
    for prefix in PREFIXES:
        directory = root / prefix
        require(directory.is_dir() and not directory.is_symlink(), 'missing native directory: ' + prefix)
        for path in directory.rglob('*'):
            require(not path.is_symlink(), 'native runtime must not contain symlinks: ' + str(path))
            if not path.is_dir():
                require(path.is_file(), 'native runtime contains a non-regular file')
                actual.add(path.relative_to(root).as_posix())
    require(actual == set(expected), 'native runtime file set differs from the fixed allowlist')
    for name, record in expected.items():
        path = root / name
        require(path.is_file() and not path.is_symlink()
                and path.stat().st_mode & 0o7777 == record['mode'], 'wrong native file or mode: ' + name)
        with path.open('rb') as stream:
            require(digest(stream) == record['sha256'], 'native runtime SHA256 mismatch: ' + name)
    binary = root / PROGRAM
    require(binary.is_file() and not binary.is_symlink() and binary.stat().st_mode & 0o7777 == 0o755,
            'missing or invalid native cluster-ctl executable')
    validate_abi(root, arch, expected)
    return {name: row['sha256'] for name, row in expected.items()}


def validate_archive(source, members, unit=None):
    """Validate a preflighted tar before any of its entries reach the destination."""
    entries = {member.name.removeprefix('./').rstrip('/'): member for member in members}
    names = {name for name in entries if attached(name)}
    if not names:
        member = entries.get(PROGRAM)
        if member is not None and member.isfile():
            with source.extractfile(member) as stream:
                require(not needs_runtime(stream), 'FoundationDB cluster-ctl archive is missing its native runtime')
        return
    require(unit in (None, 'orchestrator'), 'only orchestrator can own the cluster-ctl native runtime')
    allowed = set(next(iter(LOCK['architectures'].values()))['files'])
    files = {name for name in names if entries[name].isfile()}
    require(files == allowed and PROGRAM in entries and entries[PROGRAM].isfile(),
            'incomplete or undeclared cluster-ctl native archive payload')
    with tempfile.TemporaryDirectory(prefix='cluster-native-admission-') as temporary:
        root = Path(temporary)
        for name in [PROGRAM, *sorted(files)]:
            member = entries[name]
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as data, target.open('xb') as output:
                shutil.copyfileobj(data, output)
            target.chmod(member.mode)
        validate(root)


def remove(root):
    for prefix in PREFIXES:
        path = root / prefix
        if path.exists():
            shutil.rmtree(path)
    (root / LAUNCHER).unlink(missing_ok=True)


def copy(source, destination, arch):
    files = validate(source, arch)
    require(files is not None, 'missing cluster-ctl native closure')
    for name in [PROGRAM, *files]:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    return files


def verify_delta(root, arch, metadata, sources):
    receipt = metadata.get('cluster_native')
    files = validate(root, arch)
    if files is None:
        require('cluster_native' not in metadata, 'native runtime receipt has no payload')
        return set()
    require('cluster-ctl' in metadata['products'], 'unplanned cluster-ctl native overlay')
    require(isinstance(receipt, dict) and set(receipt) == {'files', 'sources'}
            and receipt['files'] == files and receipt['sources'] == sources.get('cluster-ctl'),
            'cluster-ctl native overlay identity differs from its product')
    return set(files)


def source_bundle(source, arch):
    """Bind a bundle to the product selected by the owner's actual make target."""
    bundle = source / 'cluster-runtime'
    if not bundle.exists():
        if (source / 'cluster-ctl').is_file():
            with (source / 'cluster-ctl').open('rb') as stream:
                require(not needs_runtime(stream), 'native source product lacks cluster-runtime')
        return None
    validate(bundle, arch)
    selected = source / 'cluster-ctl'
    require(selected.is_file(), 'native source bundle has no selected cluster-ctl product')
    with selected.open('rb') as actual, (bundle / PROGRAM).open('rb') as bundled:
        require(digest(actual) == digest(bundled), 'native source bundle differs from the selected cluster-ctl product')
    return bundle


def collect(source, destination, arch):
    """Preserve the full source bundle and its reviewed launcher in a bin tree."""
    bundle = source_bundle(source, arch)
    if bundle is None:
        return
    target = destination / 'cluster-runtime'
    if target.exists():
        shutil.rmtree(target)
    copy(bundle, target, arch)
    for name in ('cluster-ctl', 'run-cluster-ctl'):
        link = destination / name
        link.unlink(missing_ok=True)
        link.symlink_to('cluster-runtime/bin/' + name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('validate', 'collect'))
    parser.add_argument('--arch', required=True, choices=tuple(LOCK['architectures']))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    if args.command == 'validate':
        validate(args.root, args.arch)
    else:
        require(args.destination is not None, 'collect requires --destination')
        collect(args.root, args.destination, args.arch)
