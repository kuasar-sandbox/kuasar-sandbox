"""Admit Connector's static VPP executable without executing candidate code."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import tempfile

ARCHES = {'x86_64': (62, 'x86_64-linux-gnu'), 'aarch64': (183, 'aarch64-linux-gnu')}
BASELINES = {'x86_64': 'x86-64-v2', 'aarch64': 'armv8-a+crc'}
PROGRAM = 'connector-vpp'
MANIFEST = 'vpp-runtime.json'
LICENSES = 'share/licenses/connector/native'
LICENSE_FILES = {'CONNECTOR-LICENSE', 'VPP-LICENSE', 'VPP-EMBEDDED-NOTICES'}


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def attached(name):
    """Paths owned by VPP within a component or aggregate release root."""
    return name in ('bin/' + PROGRAM, 'bin/' + MANIFEST, LICENSES) or name.startswith(LICENSES + '/')


def regular(path, root):
    require(path.is_file() and not path.is_symlink(), 'missing or non-regular VPP file: ' + str(path))
    for parent in path.parents:
        require(not parent.is_symlink(), 'VPP path crosses a symlink: ' + str(path))
        if parent == root:
            break


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate VPP manifest key: ' + key)
        result[key] = value
    return result


def static_elf(data, machine):
    require(len(data) >= 64 and data[:6] == b'\x7fELF\x02\x01'
            and struct.unpack_from('<H', data, 18)[0] == machine, 'wrong native ELF')
    require(struct.unpack_from('<H', data, 16)[0] in (2, 3), 'VPP ELF is not executable')
    offset = struct.unpack_from('<Q', data, 32)[0]
    header_size, entry_size, count = struct.unpack_from('<HHH', data, 52)
    require(header_size == 64 and entry_size == 56 and 0 < count < 0xffff
            and offset >= 64 and offset + entry_size * count <= len(data),
            'invalid VPP ELF program headers')
    load = False
    for index in range(count):
        kind, _, start, _, _, size, _, _ = struct.unpack_from('<IIQQQQQQ', data, offset + index * entry_size)
        require(start <= len(data) and size <= len(data) - start, 'invalid VPP ELF segment range')
        require(kind != 3, 'VPP ELF requires a dynamic interpreter')
        load = load or kind == 1
        if kind == 2:
            require(size % 16 == 0, 'invalid VPP ELF dynamic entries')
            terminated = False
            for entry in range(start, start + size, 16):
                tag = struct.unpack_from('<Q', data, entry)[0]
                require(tag != 1, 'VPP ELF requires a shared library')
                if tag == 0:
                    terminated = True
                    break
            require(terminated, 'unterminated VPP ELF dynamic entries')
    require(load, 'VPP ELF has no loadable segment')


def inspect(runtime, arch=None):
    """Validate a bin directory and return its native manifest."""
    runtime = Path(runtime)
    require(runtime.is_dir() and not runtime.is_symlink(), 'invalid VPP output directory')
    manifest_path = runtime / MANIFEST
    regular(manifest_path, runtime)
    require(manifest_path.stat().st_size < 1024 * 1024, 'oversized VPP manifest')
    manifest = json.loads(manifest_path.read_text(), object_pairs_hook=unique_object)
    require(isinstance(manifest, dict), 'invalid VPP runtime manifest')
    selected = manifest.get('architecture')
    require(manifest.get('schema') == 1 and manifest.get('product') == 'connector-egress-native-runtime'
            and selected in ARCHES and (arch is None or selected == arch), 'VPP runtime identity mismatch')
    require(manifest.get('cpu_baseline') == BASELINES[selected], 'VPP CPU baseline mismatch')
    machine, _ = ARCHES[selected]
    require(manifest.get('required_system_libraries') == [], 'VPP runtime requires system libraries')
    files = manifest.get('files')
    require(isinstance(files, dict) and set(files) == {PROGRAM}, 'invalid VPP file manifest')
    expected = files[PROGRAM]
    require(isinstance(expected, str) and re.fullmatch('[0-9a-f]{64}', expected), 'invalid VPP digest')
    source = runtime / PROGRAM
    regular(source, runtime)
    require(source.stat().st_mode & 0o7777 == 0o755, 'invalid VPP executable mode')
    require(source.stat().st_size <= 256 * 1024 * 1024, 'invalid VPP runtime size')
    static_elf(source.read_bytes(), machine)
    require(digest(source) == expected, 'VPP digest mismatch')
    require(manifest_path.stat().st_mode & 0o7777 == 0o644, 'invalid VPP manifest mode')
    return manifest


def license_files(licenses):
    require(licenses.is_dir() and not licenses.is_symlink(), 'missing VPP license materials')
    require(all((licenses / name).is_file() and (licenses / name).stat().st_size > 0
                for name in LICENSE_FILES), 'incomplete VPP license materials')
    files = {}
    system_notices = False
    for path in licenses.rglob('*'):
        require(not path.is_symlink(), 'VPP license materials may not contain links')
        if path.is_dir():
            continue
        regular(path, licenses)
        require(path.stat().st_mode & 0o7777 == 0o644, 'invalid VPP license mode')
        relative = path.relative_to(licenses)
        name = path.name.lower()
        if (relative.parts[0] == 'system' and path.stat().st_size > 0
                and ('copyright' in name or 'license' in name or name.startswith('copying'))):
            system_notices = True
        files[relative.as_posix()] = digest(path)
    require(system_notices, 'missing statically linked system library license materials')
    return files


def validate(root, arch=None):
    """Return all VPP delivery digests, or None for a release without VPP."""
    root = Path(root)
    runtime = root / 'bin'
    if not any((root / name).exists() or (root / name).is_symlink()
               for name in ('bin/' + PROGRAM, 'bin/' + MANIFEST, LICENSES)):
        return None
    manifest = inspect(runtime, arch)
    files = {'bin/' + name: sha for name, sha in manifest['files'].items()}
    files['bin/' + MANIFEST] = digest(runtime / MANIFEST)
    files.update({LICENSES + '/' + name: sha for name, sha in license_files(root / LICENSES).items()})
    return files


def validate_archive(source, members, unit=None):
    entries = {member.name.removeprefix('./').rstrip('/'): member for member in members}
    names = {name for name in entries if attached(name) and not entries[name].isdir()}
    if not names:
        return
    require(unit in (None, 'connector'), 'only connector can own the VPP runtime')
    total = 0
    for name in names:
        member = entries[name]
        path = PurePosixPath(name)
        require(not path.is_absolute() and '..' not in path.parts and str(path) == name
                and '\\' not in name and member.isfile(), 'invalid VPP archive entry')
        total += member.size
        require(member.size <= 256 * 1024 * 1024 and total <= 1024 * 1024 * 1024 and len(names) < 4096,
                'invalid VPP archive size')
    with tempfile.TemporaryDirectory(prefix='vpp-admission-') as temporary:
        root = Path(temporary)
        for name in sorted(names):
            member = entries[name]
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as data, target.open('xb') as output:
                shutil.copyfileobj(data, output)
            target.chmod(member.mode)
        validate(root)


def remove(root):
    root = Path(root)
    for name in ('bin/' + PROGRAM, 'bin/' + MANIFEST):
        (root / name).unlink(missing_ok=True)
    path = root / LICENSES
    if path.exists():
        shutil.rmtree(path)


def copy(source, destination, arch):
    files = validate(source, arch)
    require(files is not None, 'missing VPP runtime')
    for name in files:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    return files


def collect(runtime, licenses, destination, arch):
    """Collect the owner's ordinary bin tree and retained license materials."""
    manifest = inspect(runtime, arch)
    license_files(licenses)
    for name in [*manifest['files'], MANIFEST]:
        target = destination / 'bin' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(runtime / name, target)
    shutil.copytree(licenses, destination / LICENSES)
    return validate(destination, arch)


def collect_bin(runtime, output, arch):
    """Retain the normal bin layout in a developer's platform collection."""
    if not (runtime / PROGRAM).exists() and not (runtime / MANIFEST).exists():
        return
    manifest = inspect(runtime, arch)
    for name in [*manifest['files'], MANIFEST]:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(runtime / name, target)


def verify_delta(root, arch, metadata, sources):
    files = validate(root, arch)
    receipt = metadata.get('egress_native')
    if files is None:
        require('egress_native' not in metadata, 'VPP receipt has no payload')
        return set()
    require(PROGRAM in metadata['products'], 'unplanned VPP native overlay')
    require(isinstance(receipt, dict) and set(receipt) == {'files', 'sources'}
            and receipt['files'] == files and receipt['sources'] == sources.get(PROGRAM),
            'VPP overlay identity differs from its product')
    return set(files)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('validate', 'collect-bin'))
    parser.add_argument('--arch', required=True, choices=ARCHES)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--licenses', type=Path, help='also validate retained native license materials')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.command == 'validate':
        inspect(args.runtime, args.arch)
        if args.licenses is not None:
            license_files(args.licenses)
    else:
        require(args.output is not None, '--output is required')
        collect_bin(args.runtime, args.output, args.arch)
