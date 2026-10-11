"""Data-only helper inventory shared by source, package and public consumers."""
import fnmatch
import hashlib
import json
from pathlib import Path
import re
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


def historical_inventory(contract):
    history = contract['history']
    require(isinstance(history, dict) and set(history) == {'plan', 'plan_id', 'metadata', 'helpers', 'producers', 'artifacts'},
            'invalid historical helper evidence')
    plan = history['plan']
    require(plan.get('schema') == 2 and plan.get('mode') == 'exact-assets' and
            'helper_contract' not in plan and digest(canonical(plan)) == history['plan_id'],
            'historical helper plan identity mismatch')
    require(set(history['helpers']) == set(history['metadata']) == set(history['producers']) == set(history['artifacts']) == set(ARCHES),
            'incomplete historical helper architecture evidence')
    sources = {}
    for owner in OWNERS:
        if owner == 'platform': continue
        row = contract['owners'][owner]
        unit = 'runtime' if owner == 'guest-runtime' else owner
        require(row.get('historical') is True and row['source'] == plan.get('source_records', {}).get(unit) and
                row['source']['sha'] == plan['test_revisions'][owner]['sha'] and
                row['source']['repository'] == plan['test_revisions'][owner]['repository'],
                'historical helper dependency tag/source changed')
        sources[owner] = row['source']
    shas = [row['sha'] for row in sources.values()]
    require(len(set(shas)) == len(shas) and plan['framework_sha'] not in shas,
            'ambiguous historical helper source identity')
    inventory = None
    for arch in ARCHES:
        metadata = loads(history['metadata'][arch])
        require(metadata['arch'] == arch and metadata['framework_sha'] == plan['framework_sha'] and
                metadata['source_records'] == plan['source_records'] and
                metadata['test_revisions'] == {owner: row['sha'] for owner, row in sources.items()},
                'historical helper metadata identity mismatch')
        expected = {}
        for name, original in metadata['helpers'].items():
            require(re.fullmatch('[a-z][a-z0-9_-]*', name) and set(original) == {'sha256', 'source_sha'},
                    'invalid historical helper inventory')
            if original['source_sha'] == plan['framework_sha']: continue
            owners = [owner for owner, row in sources.items() if row['sha'] == original['source_sha']]
            require(len(owners) == 1, 'ambiguous historical helper owner')
            expected[name] = owners[0]
            actual = history['helpers'][arch].get(name)
            require(isinstance(actual, dict) and set(actual) == {'owner', 'source_sha', 'sha256', 'size', 'mode'} and
                    actual['owner'] == owners[0] and all(actual[k] == original[k] for k in original),
                    'historical helper output differs from original metadata')
        require(set(history['helpers'][arch]) == set(expected), 'historical helper inventory mismatch')
        if inventory is not None: require(expected == inventory, 'historical helper architectures disagree')
        inventory = expected
        producer = history['producers'][arch]
        receipt, scope = loads(producer['receipt']), loads(producer['source_scope'])
        artifact = history['artifacts'][arch]
        run_id = artifact['workflow_run']['id']
        require(type(artifact['id']) is int and artifact['id'] > 0 and type(run_id) is int and run_id > 0 and
                re.fullmatch(r'workbench-evidence-helper-build-' + arch + r'-[0-9a-f]{32}', artifact['name']) and
                re.fullmatch('sha256:[0-9a-f]{64}', artifact['digest']) and
                artifact['workflow_run']['head_sha'] == plan['framework_sha'] and scope['run_id'] == str(run_id),
                'historical helper producer artifact identity mismatch')
        require(receipt['framework_sha'] == plan['framework_sha'] and receipt['arch'] == arch and
                receipt['conclusion'] == 'success' and receipt['mode'] == 'build' and
                receipt['cache']['WB_CACHE_COVERAGE'] == 'aggregate-helpers' and
                all(command['exit_code'] == 0 for command in receipt['commands']) and
                scope['repository'] == plan['baseline']['repository'], 'historical helper producer evidence mismatch')
    return {name: {'name': name, 'owner': owner, 'env': None, 'cases': [], 'historical': True}
            for name, owner in inventory.items()}


def validate(contract):
    require(isinstance(contract, dict) and set(contract) == ({'schema', 'owners', 'history'} if contract.get('schema') == 2 else {'schema', 'owners'})
            and type(contract['schema']) is int and contract['schema'] in (1, 2)
            and isinstance(contract['owners'], dict) and set(contract['owners']) == set(OWNERS),
            'missing or invalid trusted helper contract')
    inventory, aliases = {}, set()
    historical = historical_inventory(contract) if contract['schema'] == 2 else {}
    for name in historical:
        alias = LEGACY_ALIASES.get(name)
        if alias is not None:
            require(alias not in aliases, 'duplicate historical helper environment')
            aliases.add(alias)
    for owner, row in contract['owners'].items():
        require(isinstance(row, dict) and set(row) == ({'source', 'historical'} if row.get('historical') is True else
                {'source', 'declaration', 'declaration_sha256', 'declaration_blob', 'recipe_sha256', 'recipe_blob'}),
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
        if row.get('historical') is True:
            require(contract['schema'] == 2 and owner != 'platform', 'unauthorized historical helper source')
            for name, item in historical.items():
                if item['owner'] == owner:
                    require(name not in inventory, 'duplicate helper across owners')
                    inventory[name] = item
            continue
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
    return helpers



def selected(contract, cases):
    selection = {name: item for name, item in validate(contract).items()
            if (item.get('historical') is True and cases) or
            any(fnmatch.fnmatchcase(case, pattern) for case in cases for pattern in item['cases'])}
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
    inventory = validate(contract)
    names = set(inventory) if names is None else set(names)
    require(names <= set(inventory), 'undeclared helper request')
    files = {p.name for p in root.iterdir()}
    require(files == names, 'unexpected or missing built helper')
    records = {name: dict(check_binary(root / name, arch), owner=inventory[name]['owner'],
                      source_sha=contract['owners'][inventory[name]['owner']]['source']['sha']) for name in sorted(names)}
    for name, record in records.items():
        if inventory[name].get('historical'):
            require(record == contract['history']['helpers'][arch][name], 'historical helper binary changed')
    return records


def verify(root, arch, contract, records, names=None, *, metadata=False, private_extraction=False):
    inventory = validate(contract)
    names = set(inventory) if names is None else set(names)
    require(names <= set(inventory) and isinstance(records, dict) and set(records) == names,
            'helper receipt differs from trusted inventory')
    require(not root.is_symlink() and (root.is_dir() or (not names and not metadata and not root.exists())),
            'invalid helper output directory')
    files = {p.name for p in root.iterdir()} if root.is_dir() else set()
    require(files == names | ({'helpers.json'} if metadata else set()),
            'undeclared or missing helper package output')
    for name, record in records.items():
        owner = inventory[name]['owner']
        expected = dict(check_binary(root / name, arch), owner=owner,
                        source_sha=contract['owners'][owner]['source']['sha'])
        if inventory[name].get('historical'):
            require(record == contract['history']['helpers'][arch][name], 'historical helper receipt changed')
        if private_extraction:
            original_mode = record.get('mode')
            require(type(original_mode) is int and 0 <= original_mode <= 0o7777 and
                    original_mode & 0o500 == 0o500 and not original_mode & 0o7022 and
                    expected['mode'] & ~original_mode == 0, 'unsafe private helper extraction mode')
            expected['mode'] = original_mode
        require(record == expected, 'helper output identity differs from trusted source/bytes: ' + name)
    return records
