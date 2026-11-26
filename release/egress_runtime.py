"""Admit Connector's VPP files directly beside its CLI without executing them."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import tempfile

PLUGINS = {'nat', 'af_packet', 'ping', 'egress_policy', 'egress_geneve', 'egress_packet'}
ARCHES = {'x86_64': (62, 'x86_64-linux-gnu'), 'aarch64': (183, 'aarch64-linux-gnu')}
BASELINES = {'x86_64': 'x86-64-v2', 'aarch64': 'armv8-a+crc'}
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
    return (name in ('bin/vpp', 'bin/' + MANIFEST, 'bin/lib', LICENSES)
            or name.startswith(('bin/lib/', LICENSES + '/')))


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
    machine, triplet = ARCHES[selected]
    files = manifest.get('files')
    require(isinstance(files, dict) and len(files) < 4096, 'invalid VPP file manifest')
    required = {'vpp', *('lib/' + triplet + '/vpp_plugins/' + name + '_plugin.so' for name in PLUGINS)}
    require(required <= set(files), 'incomplete VPP runtime')
    actual = {'vpp'} if (runtime / 'vpp').exists() else set()
    lib = runtime / 'lib'
    require(lib.is_dir() and not lib.is_symlink(), 'missing VPP libraries')
    for path in lib.rglob('*'):
        require(not path.is_symlink(), 'VPP runtime may not contain links')
        if not path.is_dir():
            require(path.is_file(), 'VPP runtime contains a special file')
            actual.add(path.relative_to(runtime).as_posix())
    require(actual == set(files), 'VPP manifest file set mismatch')
    total = 0
    for name, expected in files.items():
        path = PurePosixPath(name)
        require(not path.is_absolute() and '..' not in path.parts and str(path) == name and '\\' not in name,
                'unsafe VPP file path')
        allowed = (name == 'vpp'
                   or re.fullmatch(r'lib/' + triplet + r'/lib[A-Za-z0-9_.+-]+\.so(?:\.[0-9.]+)?', name)
                   or name in required)
        require(allowed, 'undeclared VPP file: ' + name)
        require(isinstance(expected, str) and re.fullmatch('[0-9a-f]{64}', expected), 'invalid VPP digest')
        source = runtime / name
        regular(source, runtime)
        require(source.stat().st_mode & 0o7777 == (0o755 if name == 'vpp' else 0o644),
                'invalid VPP file mode: ' + name)
        total += source.stat().st_size
        require(source.stat().st_size <= 256 * 1024 * 1024 and total <= 1024 * 1024 * 1024,
                'invalid VPP runtime size')
        with source.open('rb') as stream:
            header = stream.read(20)
        require(len(header) == 20 and header[:6] == b'\x7fELF\x02\x01'
                and struct.unpack_from('<H', header, 18)[0] == machine, 'wrong native ELF: ' + name)
        require(digest(source) == expected, 'VPP digest mismatch: ' + name)
    require(manifest_path.stat().st_mode & 0o7777 == 0o644, 'invalid VPP manifest mode')
    return manifest


def license_files(licenses):
    require(licenses.is_dir() and not licenses.is_symlink(), 'missing VPP license materials')
    require(all((licenses / name).is_file() for name in LICENSE_FILES), 'incomplete VPP license materials')
    files = {}
    for path in licenses.rglob('*'):
        require(not path.is_symlink(), 'VPP license materials may not contain links')
        if path.is_dir():
            continue
        regular(path, licenses)
        require(path.stat().st_mode & 0o7777 == 0o644, 'invalid VPP license mode')
        files[path.relative_to(licenses).as_posix()] = digest(path)
    return files


def validate(root, arch=None):
    """Return all VPP delivery digests, or None for a release without VPP."""
    root = Path(root)
    runtime = root / 'bin'
    if not any((root / name).exists() or (root / name).is_symlink()
               for name in ('bin/vpp', 'bin/' + MANIFEST, 'bin/lib', LICENSES)):
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
    for name in ('bin/vpp', 'bin/' + MANIFEST):
        (root / name).unlink(missing_ok=True)
    for name in ('bin/lib', LICENSES):
        path = root / name
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
    if not (runtime / 'vpp').exists() and not (runtime / MANIFEST).exists():
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
    require('vpp' in metadata['products'], 'unplanned VPP native overlay')
    require(isinstance(receipt, dict) and set(receipt) == {'files', 'sources'}
            and receipt['files'] == files and receipt['sources'] == sources.get('vpp'),
            'VPP overlay identity differs from its product')
    return set(files)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('validate', 'collect-bin'))
    parser.add_argument('--arch', required=True, choices=ARCHES)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.command == 'validate':
        inspect(args.runtime, args.arch)
    else:
        require(args.output is not None, '--output is required')
        collect_bin(args.runtime, args.output, args.arch)
