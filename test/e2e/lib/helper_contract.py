"""Data-only helper inventory shared by source, package and public consumers."""
import fnmatch
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import struct

OWNERS = ('platform', 'accelerator', 'connector', 'guest-runtime', 'sandboxer', 'orchestrator')
ARCHES = ('x86_64', 'aarch64')
DECLARATION = 'release/helper-inputs.json'
# Existing historical case interface only; never an allowed output inventory.
LEGACY_ALIASES = {
    'zot': 'ZOT_BIN', 'versitygw': 'VGW_BIN', 'custom-proxy': 'CUSTOM_PROXY_BIN',
    'telemetry-grpc-probe': 'TELEMETRY_GRPC_PROBE_BIN', 'usage-probe': 'USAGE_PROBE_BIN',
    'cgroup-fork-probe': 'CGROUP_FORK_PROBE_BIN', 'node-ctl-runner-test': 'NODE_CTL_RUNNER_TEST_BINARY',
}



def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate helper JSON key: ' + key)
        result[key] = value
    return result


def loads(text):
    return json.loads(text, object_pairs_hook=unique)


def safe_path(value):
    require(isinstance(value, str) and value and '\\' not in value and '\x00' not in value
            and all(part not in ('', '.', '..') for part in value.split('/'))
            and not value.startswith('/'), 'invalid helper input path')
    return Path(value)


def regular(root, name):
    path = safe_path(name)
    require(all(not (root / parent).is_symlink() for parent in (path, *path.parents)),
            'helper input contains a symbolic link')
    result = root / path
    require(result.is_file(), 'missing helper input: ' + name)
    return result


def declaration(text):
    value = loads(text)
    require(isinstance(value, dict) and set(value) == {'schema', 'recipe', 'dependencies', 'helpers'}
            and type(value['schema']) is int and value['schema'] == 1, 'invalid helper declaration schema')
    recipe = value['recipe']
    require(recipe is None or isinstance(recipe, str), 'invalid helper recipe')
    if recipe is not None:
        safe_path(recipe)
    deps = value['dependencies']
    require(isinstance(deps, list) and all(isinstance(x, str) and x in OWNERS for x in deps)
            and len(set(deps)) == len(deps), 'invalid helper dependency owners')
    require(isinstance(value['helpers'], list), 'invalid declared helper inventory')
    require(bool(value['helpers']) == (recipe is not None), 'helper recipe/inventory mismatch')
    names, aliases = set(), set()
    for item in value['helpers']:
        require(isinstance(item, dict) and set(item) == {'name', 'env', 'cases'}, 'invalid helper declaration entry')
        name = item['name']
        require(isinstance(name, str) and re.fullmatch(r'[a-z][a-z0-9_-]*', name)
                and name not in names, 'invalid or duplicate helper name')
        names.add(name)
        alias = item['env']
        require(alias is None or (isinstance(alias, str) and re.fullmatch(r'[A-Z][A-Z0-9_]*(?:_BIN|_BINARY)', alias)
                                 and alias not in aliases), 'invalid or duplicate helper environment binding')
        if alias is not None:
            aliases.add(alias)
        patterns = item['cases']
        require(isinstance(patterns, list) and all(isinstance(p, str) and
                re.fullmatch(r'[a-z][a-z0-9*_-]*\.[a-z0-9.*_-]+\.sh', p) for p in patterns)
                and len(set(patterns)) == len(patterns), 'invalid helper case requirements')
    return value


def validate(contract):
    if isinstance(contract, dict) and contract.get('schema') == 3:
        require(set(contract) == {'schema', 'helpers'} and isinstance(contract['helpers'], dict),
                'invalid runtime helper inventory')
        inventory, aliases = {}, set()
        for name, row in contract['helpers'].items():
            require(isinstance(name, str) and re.fullmatch(r'[a-z][a-z0-9_-]*', name), 'invalid helper name')
            require(isinstance(row, dict) and set(row) == {'owner', 'env', 'cases'}
                    and row['owner'] in OWNERS, 'invalid runtime helper entry')
            alias = row['env']
            require(alias is None or isinstance(alias, str) and
                    re.fullmatch(r'[A-Z][A-Z0-9_]*(?:_BIN|_BINARY)', alias) and alias not in aliases,
                    'invalid or duplicate helper environment binding')
            if alias is not None:
                aliases.add(alias)
            patterns = row['cases']
            require(isinstance(patterns, list) and len(patterns) == len(set(patterns)) and
                    all(isinstance(p, str) and (p == '*' or re.fullmatch(
                        r'[a-z][a-z0-9*_-]*\.[a-z0-9.*_-]+\.sh', p)) for p in patterns),
                    'invalid helper case requirements')
            inventory[name] = dict(row, name=name)
        return inventory
    require(isinstance(contract, dict) and set(contract) == {'schema', 'owners'}
            and type(contract['schema']) is int and contract['schema'] == 1
            and isinstance(contract['owners'], dict) and set(contract['owners']) == set(OWNERS),
            'missing or invalid trusted helper contract')
    inventory, aliases = {}, set()
    for owner, row in contract['owners'].items():
        require(isinstance(row, dict) and set(row) == {'source', 'declaration', 'declaration_sha256', 'declaration_blob', 'recipe_sha256', 'recipe_blob'},
                'invalid helper owner evidence')
        source = row['source']
        require(isinstance(source, dict) and set(source) in ({'repository', 'sha', 'tree', 'tag'},
                {'repository', 'sha', 'tree', 'ref'}, {'repository', 'sha', 'tree'}), 'invalid helper source facts')
        repository = 'kuasar-sandbox/' + ('kuasar-sandbox' if owner == 'platform' else owner)
        require(source['repository'] == repository and all(isinstance(source[k], str) and
                re.fullmatch('[0-9a-f]{40}', source[k]) for k in ('sha', 'tree')), 'helper source owner/identity mismatch')
        if 'tag' in source:
            prefix = 'runtime-' if owner == 'guest-runtime' else ''
            require(isinstance(source['tag'], str) and re.fullmatch(re.escape(prefix) +
                    r'v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-preview\.[0-9]{8}(?:\.[1-9][0-9]*)?)?', source['tag']),
                    'invalid helper owner tag')
        if 'ref' in source:
            require(isinstance(source['ref'], str) and source['ref'].startswith(('refs/heads/', 'refs/pull/'))
                    and '..' not in source['ref'], 'invalid admitted helper source ref')
        require(isinstance(row['declaration'], str), 'missing original helper declaration bytes')
        data = row['declaration'].encode()
        require(row['declaration_sha256'] == digest(data) and row['declaration_blob'] == blob(data),
                'helper declaration identity mismatch')
        declared = declaration(row['declaration'])
        for key, size in (('recipe_sha256', 64), ('recipe_blob', 40)):
            require((row[key] is None if declared['recipe'] is None else
                     isinstance(row[key], str) and re.fullmatch('[0-9a-f]{' + str(size) + '}', row[key])),
                    'invalid helper recipe identity')
        for item in declared['helpers']:
            require(item['name'] not in inventory, 'duplicate helper across owners')
            if item['env'] is not None:
                require(item['env'] not in aliases, 'duplicate helper environment across owners')
                aliases.add(item['env'])
            inventory[item['name']] = dict(item, owner=owner)
    return inventory


def runtime(contract):
    """Project declarations onto runtime fields without reading source evidence."""
    if isinstance(contract, dict) and contract.get('schema') == 3:
        validate(contract)
        return contract
    require(isinstance(contract, dict) and isinstance(contract.get('owners'), dict),
            'invalid legacy helper inventory')
    helpers = {}
    for owner, row in contract['owners'].items():
        require(owner in OWNERS, 'unknown helper owner')
        require(isinstance(row, dict) and isinstance(row.get('declaration'), str),
                'missing legacy helper runtime declaration')
        declared = loads(row['declaration'])
        require(isinstance(declared, dict) and isinstance(declared.get('helpers'), list),
                'invalid legacy helper runtime declarations')
        for item in declared['helpers']:
            require(isinstance(item, dict) and {'name', 'env', 'cases'} <= set(item),
                    'incomplete legacy helper runtime entry')
            name = item['name']
            require(isinstance(name, str) and name not in helpers, 'invalid or duplicate helper name')
            helpers[name] = {'owner': owner, 'env': item['env'], 'cases': item['cases']}
    result = {'schema': 3, 'helpers': helpers}
    validate(result)
    return result


def package(root, arch, *, private_extraction=False):
    """Read new and old package runtime fields. No historical jobs or receipts."""
    metadata = loads(regular(root, 'helpers.json').read_text())
    require(metadata.get('arch') == arch and isinstance(metadata.get('helpers'), dict),
            'helper package architecture or inventory mismatch')
    schema = metadata.get('schema')
    require(schema is None or type(schema) is int and schema in (1, 2), 'unknown helper package schema')
    records = metadata['helpers']
    require(all(isinstance(row, dict) for row in records.values()), 'invalid helper file record')
    if metadata.get('schema') == 2:
        require(set(metadata) == {'schema', 'arch', 'helpers'}, 'unexpected runtime helper metadata')
        require(all(set(row) == {'owner', 'env', 'cases', 'sha256', 'size', 'mode'} for row in records.values()),
                'unexpected runtime helper fields')
        helpers = {name: {key: row[key] for key in ('owner', 'env', 'cases')} for name, row in records.items()}
        contract = {'schema': 3, 'helpers': helpers}
    elif (root.parent / 'contract.json').is_file():
        require(not (root.parent / 'contract.json').is_symlink(), 'linked helper declaration')
        contract = runtime(loads((root.parent / 'contract.json').read_text()))
    elif any('env' in row or 'cases' in row for row in records.values()):
        require(all({'owner', 'env', 'cases'} <= set(row) for row in records.values()),
                'incomplete legacy runtime helper fields')
        contract = {'schema': 3, 'helpers': {name: {key: row[key] for key in ('owner', 'env', 'cases')}
                                          for name, row in records.items()}}
    else:
        # Old packages did not declare applicability. Preserve every delivered
        # helper; the alias map only supplies the old environment interface.
        owners = required_capabilities(['orchestrator.exec.sh', 'sandbox.cgroup.sh'])
        contract = {'schema': 3, 'helpers': {name: {'owner': owners.get(name, 'platform'),
                    'env': LEGACY_ALIASES.get(name), 'cases': ['*']} for name in records}}
    inventory = validate(contract)
    require(set(records) == set(inventory), 'helper inventory differs from package declarations')
    normalized = {}
    for name, record in records.items():
        actual = check_binary(regular(root, name), arch)
        require(record.get('sha256') == actual['sha256'], 'helper checksum mismatch: ' + name)
        for key in ('size', 'mode'):
            if key in record:
                if key == 'mode' and private_extraction:
                    mode = record[key]
                    require(type(mode) is int and 0 <= mode <= 0o7777 and mode & 0o500 == 0o500
                            and not mode & 0o7022 and actual[key] & ~mode == 0, 'unsafe helper mode')
                else:
                    require(record[key] == actual[key], 'helper ' + key + ' mismatch: ' + name)
        normalized[name] = dict(actual, **contract['helpers'][name])
    require({p.name for p in root.iterdir()} == set(records) | {'helpers.json'},
            'undeclared or missing helper package output')
    return contract, normalized


def required_capabilities(cases):
    """Required existing case capabilities, never an allowed delivery inventory."""
    cases = set(cases)
    orchestrator = any(name.startswith(('orchestrator.', 'builder.')) for name in cases)
    orchestrator |= bool(cases & {'telemetry.backends.sh', 'telemetry.guest.sh', 'telemetry.proxy.sh'})
    sandboxer = any(name.startswith(('sandbox.', 'snapshot.')) for name in cases)
    sandboxer |= bool(cases & {'network.tapfd.sh', 'image.manifest-boot.sh', 'image.sandbox-assembly.sh',
                               'telemetry.usage.sh', 'telemetry.usage-faults.sh', 'telemetry.source-faults.sh'})
    helpers = {}
    if orchestrator or sandboxer or cases & {'basic.demo.sh', 'image.flatten.sh', 'image.registry.sh'}:
        helpers['zot'] = 'platform'
    if orchestrator or 'basic.demo.sh' in cases:
        helpers['versitygw'] = 'platform'
    if orchestrator:
        helpers.update({'custom-proxy': 'orchestrator', 'telemetry-grpc-probe': 'orchestrator',
                        'node-ctl-runner-test': 'orchestrator'})
    if sandboxer:
        helpers['usage-probe'] = 'sandboxer'
    if 'sandbox.cgroup.sh' in cases:
        helpers['cgroup-fork-probe'] = 'sandboxer'
    if any(name.startswith('basic.node-infra-') for name in cases):
        helpers['node-infra-guest'] = 'platform'
    if 'basic.node-infra-config.sh' in cases:
        helpers.update({'node-infra-xproxy': 'platform', 'node-infra-xconductor': 'platform'})
    return helpers



def selected(contract, cases):
    selection = {name: item for name, item in validate(contract).items()
            if any(fnmatch.fnmatchcase(case, pattern) for case in cases for pattern in item['cases'])}
    require(all(name in selection and selection[name]['owner'] == owner
                for name, owner in required_capabilities(cases).items()),
            'declaration omits required helper capability')
    return selection


def check_binary(path, arch):
    require(arch in ARCHES, 'unknown helper architecture')
    require(path.is_file() and not path.is_symlink(), 'missing regular helper output')
    mode = stat.S_IMODE(path.stat().st_mode)
    require(mode & 0o500 == 0o500 and not mode & 0o7022, 'unsafe helper mode')
    data = path.read_bytes()
    require(len(data) >= 64 and data[:7] == b'\x7fELF\x02\x01\x01' and
            struct.unpack_from('<H', data, 18)[0] == {'x86_64': 62, 'aarch64': 183}[arch], 'helper ELF architecture mismatch')
    return {'sha256': digest(data), 'size': len(data), 'mode': mode}


def receipt(root, arch, contract, names=None):
    contract = runtime(contract)
    inventory = validate(contract)
    names = set(inventory) if names is None else set(names)
    require(names <= set(inventory), 'undeclared helper request')
    files = {p.name for p in root.iterdir()}
    require(files == names, 'unexpected or missing built helper')
    return {name: dict(check_binary(root / name, arch), **contract['helpers'][name]) for name in sorted(names)}


def verify(root, arch, contract, records, names=None, *, metadata=False, private_extraction=False):
    legacy = contract.get('schema') != 3
    contract = runtime(contract)
    inventory = validate(contract)
    names = set(inventory) if names is None else set(names)
    require(names <= set(inventory) and isinstance(records, dict) and set(records) == names,
            'helper receipt differs from trusted inventory')
    require(not root.is_symlink() and (root.is_dir() or (not names and not metadata and not root.exists())),
            'invalid helper output directory')
    files = {p.name for p in root.iterdir()} if root.is_dir() else set()
    require(files == names | ({'helpers.json'} if metadata else set()),
            'undeclared or missing helper package output')
    normalized = {}
    for name, record in records.items():
        expected = dict(check_binary(root / name, arch), **contract['helpers'][name])
        if private_extraction:
            original_mode = record.get('mode')
            require(type(original_mode) is int and 0 <= original_mode <= 0o7777 and
                    original_mode & 0o500 == 0o500 and not original_mode & 0o7022 and
                    expected['mode'] & ~original_mode == 0, 'unsafe private helper extraction mode')
            expected['mode'] = original_mode
        observed = {key: record.get(key, expected[key] if key in ('env', 'cases') else None)
                    for key in expected} if legacy else record
        require(observed == expected, 'helper runtime fields or bytes differ: ' + name)
        normalized[name] = expected
    return normalized


def install_reader(root):
    # The library writing runtime metadata also supplies its reader. Retain
    # released component cases, binaries and the rest of the public runner.
    target = root / 'test/e2e/lib/helper_contract.py'
    require(target.parent.is_dir() and not target.is_symlink(), 'missing or linked helper reader')
    shutil.copyfile(Path(__file__), target)
    target.chmod(0o644)


def packages(root, *, runtime_only=False):
    require(root.is_dir() and not root.is_symlink(), 'missing helper package directory')
    require({path.name for path in root.iterdir()} <= set(ARCHES) | (set() if runtime_only else {'contract.json'}),
            'unexpected helper package root entry')
    result, previous = {}, None
    for arch in ARCHES:
        if runtime_only:
            metadata = loads(regular(root / arch, 'helpers.json').read_text())
            require(metadata.get('schema') == 2, 'new helpers require runtime-only metadata')
        contract, records = package(root / arch, arch)
        require(previous is None or contract == previous, 'architecture helper inventories differ')
        previous = contract
        result[arch] = records
    return result


def copy_packages(source, target):
    """Package runtime fields only, also when the input came from an old producer."""
    records = packages(source)
    require(not target.exists() and not target.is_symlink(), 'helper package output already exists')
    for arch in ARCHES:
        destination = target / arch
        destination.mkdir(parents=True)
        for name in records[arch]:
            shutil.copy2(regular(source / arch, name), destination / name)
        (destination / 'helpers.json').write_bytes(canonical(
            {'schema': 2, 'arch': arch, 'helpers': records[arch]}) + b'\n')
        (destination / 'helpers.json').chmod(0o644)
    packages(target, runtime_only=True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Check both native runtime helper inventories.')
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    packages(args.root, runtime_only=True)
