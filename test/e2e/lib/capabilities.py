"""Versioned, static E2E requirements. This module never evaluates case code.

The owner-local lib/requirements.json marker distinguishes new inputs from
historical packages. Requirements travel with case bytes, not a platform list.
"""
import hashlib
import json
from pathlib import Path
import re

SCHEMA = 1
CAPABILITIES = frozenset({
    'kvm', 'tun', 'userfaultfd', 'bpf-map', 'bpf-tc', 'netns', 'geneve',
    'systemd', 'journald', 'cgroup-v2', 'docker', 'podman', 'quadlet',
})
HEADER = '# e2e-requires:'
OWNERS = ('accelerator', 'connector', 'guest-runtime', 'sandboxer', 'orchestrator', 'platform')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def loads(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=unique)


def valid_marker(value):
    return isinstance(value, dict) and set(value) == {'schema'} and type(value['schema']) is int and value['schema'] == SCHEMA


def requirements(value):
    require(isinstance(value, list) and all(isinstance(item, str) for item in value),
            'E2E requirements must be a JSON string array')
    require(len(value) == len(set(value)), 'duplicate E2E capability')
    require(set(value) <= CAPABILITIES, 'unknown E2E capability: ' + repr(set(value) - CAPABILITIES))
    return sorted(value)


def parse(data, *, required=True):
    text = data.decode('utf-8')
    declarations = []
    in_header = True
    for line in text.splitlines():
        if 'e2e-requires' in line:
            require(in_header and line.startswith(HEADER), 'misplaced or malformed E2E requirement header')
            declarations.append(line[len(HEADER):].strip())
        if line.strip() and not line.startswith('#'):
            in_header = False
    require(len(declarations) <= 1, 'duplicate E2E requirement header')
    if not declarations:
        require(not required, 'missing E2E requirement header')
        return None
    return requirements(loads(declarations[0]))


def entry(path):
    require(path.is_file() and not path.is_symlink(), 'unsafe E2E requirement input: ' + str(path))
    data = path.read_bytes()
    return {'sha256': hashlib.sha256(data).hexdigest(), 'requires': parse(data)}


def discover(roots, case_files):
    """Return None only for a wholly historical input set.

    Mixed generations must be completed through ordinary selected-source
    updates; they cannot silently retain partial ARM coverage.
    """
    records, modern = {}, []
    for owner, names in sorted(case_files.items()):
        root = Path(roots[owner]) / ('test/e2e/platform' if owner == 'platform' else 'test/e2e')
        marker = root / 'lib/requirements.json'
        present = marker.exists() or marker.is_symlink()
        modern.append(present)
        if present:
            require(marker.is_file() and not marker.is_symlink() and valid_marker(loads(marker.read_text())),
                    'unsupported owner requirement format: ' + owner)
        for name in names:
            path = root / 'cases' / name
            if present:
                records[name] = entry(path)
            else:
                require(parse(path.read_bytes(), required=False) is None,
                        'E2E requirement header without owner format marker: ' + owner)
    require(not any(modern) or all(modern), 'mixed E2E requirement generations; update the selected owner inputs')
    return {'schema': SCHEMA, 'cases': records} if all(modern) else None


def validate(contract, case_files):
    require(isinstance(contract, dict) and set(contract) == {'schema', 'cases'} and
            type(contract['schema']) is int and contract['schema'] == SCHEMA, 'unsupported E2E requirement contract')
    names = [name for owner in case_files.values() for name in owner]
    require(len(names) == len(set(names)) and set(contract['cases']) == set(names),
            'E2E requirement inventory differs from selected source files')
    for name, record in contract['cases'].items():
        require(isinstance(record, dict) and set(record) == {'sha256', 'requires'} and
                isinstance(record['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', record['sha256']),
                'invalid E2E requirement identity: ' + name)
        require(requirements(record['requires']) == record['requires'], 'noncanonical E2E requirements: ' + name)
    return contract


def verify(directory, contract):
    for name, record in contract['cases'].items():
        require(Path(name).name == name, 'invalid E2E case identity')
        require(entry(Path(directory) / name) == record, 'E2E requirement/case bytes changed: ' + name)


def packaged(read, names):
    """Validate archive or filesystem bytes through a non-executing reader."""
    markers = ['test/e2e/lib/' + owner + '/requirements.json' for owner in OWNERS]
    present = [name in names for name in markers]
    require(not any(present) or all(present), 'incomplete packaged E2E requirement generation')
    modern = all(present)
    if modern:
        for name in markers:
            require(valid_marker(loads(read(name))), 'unsupported packaged E2E requirement format')
    cases = {}
    for name in sorted(names):
        if name.startswith('test/e2e/cases/'):
            data = read(name)
            required = parse(data, required=modern)
            require(modern or required is None, 'packaged header lacks its owner format marker')
            if modern:
                cases[Path(name).name] = {'sha256': hashlib.sha256(data).hexdigest(), 'requires': required}
    require(cases or not modern, 'new E2E package has no requirements')
    return {'schema': SCHEMA, 'cases': cases} if modern else None


def workspace_contract(root, provenance):
    names = [str(path.relative_to(root)) for path in (root / 'test/e2e').rglob('*') if path.is_file()]
    contract = packaged(lambda name: (root / name).read_bytes(), names)
    if 'case_requirements' in provenance:
        require(contract == provenance['case_requirements'], 'prepared E2E requirement contract changed')
    elif contract is not None:
        provenance['case_requirements'] = contract
    return contract
