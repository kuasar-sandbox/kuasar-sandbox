"""Closed, opt-in Orchestrator source stages; discovery never executes owner code.

v2 runs five owner gates, including one explicitly selected race-case gate.
It avoids four cold instrumented compilations. v1 keeps its original evidence
meaning for already selected historical sources; it is not reinterpreted as v2.

For historical v1, the owner entry declares the protocol in leading comments
and owns the complete no-argument gate. That protocol has eight stages.
The platform runs its four race partitions itself so their literal commands and
complete package results can be checked independently at finalization. Other
stages call the same frozen owner entry; no arbitrary command is accepted in a
declaration. Missing/unknown versions retain the complete historical gate.
"""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

OWNER = 'orchestrator'
REPOSITORY = 'kuasar-sandbox/orchestrator'
MODULE = 'github.com/' + REPOSITORY
ENTRY = 'scripts/ci-source-checks.sh'
PROTOCOL = 'orchestrator-source-v1'
RACE_POLICY = 'orchestrator-race-four-v1'
RACE_STAGES = ('race-1', 'race-2', 'race-3', 'race-4')
STAGES = ('normal', *RACE_STAGES, 'databases', 'services', 'vet-and-helpers')
GROUPS = tuple('owner-orchestrator--' + stage for stage in STAGES)
CASE_PROTOCOL = 'orchestrator-source-v2'
CASE_STAGES = ('normal', 'race', 'databases', 'services', 'vet-and-helpers')
CASE_POLICY = 'orchestrator-race-cases-v2'
PROTOCOLS = {PROTOCOL: (STAGES, RACE_POLICY), CASE_PROTOCOL: (CASE_STAGES, CASE_POLICY)}
ALL_STAGES = tuple(dict.fromkeys((*STAGES, *CASE_STAGES)))
ALL_GROUPS = tuple('owner-orchestrator--' + stage for stage in ALL_STAGES)
EXECUTOR = '/inputs/release/ci/integration/run-source-checks.py'
ENTRY_LIMIT = 128 * 1024
JSON_LIMIT = 32 * 1024 * 1024
PACKAGE_LIMIT = 4096
HEX40 = re.compile(r'[0-9a-f]{40}')
HEX64 = re.compile(r'[0-9a-f]{64}')
PACKAGE = re.compile(re.escape(MODULE) + r'(?:/[A-Za-z0-9_.-]+)*')
GO_ENV = ('GOVERSION', 'GOOS', 'GOARCH', 'CGO_ENABLED', 'GOFLAGS', 'GOWORK')
LIST_COMMAND = ['go', 'list', '-race', '-json', './...']
ENV_COMMAND = ['go', 'env', '-json', *GO_ENV]
RACE_COMMAND = ['go', 'test', '-json', '-race', '-p=1', '-count=1', '-timeout=5m']

# Immutable v1 cost hints, not an inclusion list. Every package returned by the
# selected source's go list participates, including new and no-test packages.
# Changing weights/assignment requires a new policy while retaining v1 readers.
# Values are ceil(max(package elapsed)) from two observed source runs, not cold
# compilation budgets or promised savings. Unlisted packages have weight one.
RACE_WEIGHTS = {
    'app/activation': 26,
    'app/conductor': 2,
    'app/proxy': 2,
    'app/proxy/extension': 2,
    'app/telemetry': 2,
    'cmd/cluster-ctl': 5,
    'cmd/node-ctl': 33,
    'cmd/node-stub-ctl': 2,
    'config': 5,
    'examples/custom-telemetry': 3,
    'internal/api': 2,
    'internal/apikey': 2,
    'internal/appnet': 2,
    'internal/buildcfg': 2,
    'internal/builder': 3,
    'internal/cluster': 2,
    'internal/cluster/metadata': 2,
    'internal/cluster/metastore': 14,
    'internal/clustercfg': 2,
    'internal/clusterclient': 5,
    'internal/componentexec': 4,
    'internal/conductorapp': 2,
    'internal/conductorext': 3,
    'internal/configresolve': 2,
    'internal/configsock': 10,
    'internal/difftemplate': 2,
    'internal/envdsign': 2,
    'internal/execadmission': 2,
    'internal/execsession': 2,
    'internal/filestore': 2,
    'internal/helperexec': 8,
    'internal/hostmetrics': 2,
    'internal/idleclock': 2,
    'internal/keys': 2,
    'internal/launcher': 2,
    'internal/logtarget': 2,
    'internal/membergroup': 2,
    'internal/metrics': 2,
    'internal/migrationtoken': 2,
    'internal/mmds': 2,
    'internal/mmdsrpc': 2,
    'internal/mmdssvc': 4,
    'internal/nodectl': 44,
    'internal/nodegroup': 2,
    'internal/nodelink': 2,
    'internal/nodepath': 2,
    'internal/observebudget': 2,
    'internal/orch': 275,
    'internal/placement': 2,
    'internal/placer': 4,
    'internal/proxy': 2,
    'internal/proxyadmission': 5,
    'internal/proxyapp': 257,
    'internal/proxydrain': 3,
    'internal/proxyext': 2,
    'internal/proxyshm': 6,
    'internal/proxystats': 2,
    'internal/reflocation': 2,
    'internal/regcreds': 2,
    'internal/registry': 38,
    'internal/router': 4,
    'internal/routesync': 2,
    'internal/sandboxcfg': 2,
    'internal/sandboxproc': 3,
    'internal/secretbox': 2,
    'internal/storage': 2,
    'internal/storage/observe': 2,
    'internal/storage/sqlengine': 4,
    'internal/store': 53,
    'internal/strictjson': 2,
    'internal/taskartifact': 3,
    'internal/telemetry': 16,
    'internal/telemetryapp': 3,
    'internal/types': 2,
    'internal/util': 2,
    'internal/version': 2,
    'internal/vswitch': 2,
    'internal/workspace': 2,
    'internal/workspacedisk': 2,
    'internal/workspaceprobe': 2,
    'test/e2e/cluster_stub': 5,
    'test/e2e/telemetryprobe': 2,
}


def require(condition, message):
    if not condition:
        raise ValueError('source stages: ' + message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read_regular(root, relative, limit):
    """Open a bounded regular file without following a relative path component."""
    path = Path(relative)
    require(not path.is_absolute() and all(p not in ('', '.', '..') for p in relative.split('/')),
            'invalid input path')
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_size <= limit, 'input is not a bounded regular file')
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            require(len(data) <= limit and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), 'input changed during read')
            return data, '100755' if before.st_mode & 0o111 else '100644'
    finally:
        os.close(directory)


def declaration(data):
    found = []
    for line in data.decode('utf-8').splitlines():
        if line.strip() and not line.lstrip().startswith('#'):
            break
        if 'kuasar-source-stages' in line:
            match = re.fullmatch(r'# kuasar-source-stages: ([a-z][a-z0-9-]{0,63})', line)
            require(match is not None, 'malformed stage declaration')
            found.append(match[1])
    require(len(found) <= 1, 'duplicate stage declaration')
    return found[0] if found else None


def entry_record(data, mode):
    return dict(path=ENTRY, mode=mode, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                git_blob=hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest())


def discover(roots, identities):
    root = roots.get(OWNER)
    if root is None:
        return None
    try:
        data, mode = read_regular(root, ENTRY, ENTRY_LIMIT)
    except FileNotFoundError:
        return None
    protocol = declaration(data)
    if protocol not in PROTOCOLS:
        return None
    stages, policy = PROTOCOLS[protocol]
    source = {key: identities[OWNER][key] for key in ('repository', 'sha', 'tree')}
    require(source['repository'] == REPOSITORY and all(isinstance(source[key], str) and
            HEX40.fullmatch(source[key]) for key in ('sha', 'tree')), 'invalid discovery identity')
    # Git is the already admitted immutable input, not an owner-provided probe.
    tree = subprocess.check_output(['git', 'ls-tree', '-z', source['tree'], '--', ENTRY], cwd=root)
    record = entry_record(data, mode)
    require(tree == (mode + ' blob ' + record['git_blob'] + '\t' + ENTRY + '\0').encode(),
            'declaration is not the admitted regular Git blob')
    return dict(protocol=protocol, owner=OWNER, source=source, entry=record,
                stages=list(stages), race_partition=policy)


def contract(plan):
    if 'source_stages' not in plan:
        return None
    value = plan['source_stages']
    require(plan.get('mode') == 'source' and isinstance(value, dict) and
            set(value) == {'protocol', 'owner', 'source', 'entry', 'stages', 'race_partition'},
            'invalid stage contract')
    require(isinstance(value['protocol'], str) and value['protocol'] in PROTOCOLS, 'unknown stage protocol')
    stages, policy = PROTOCOLS[value['protocol']]
    require(value['owner'] == OWNER and value['stages'] == list(stages)
            and value['race_partition'] == policy, 'unknown stage protocol or incomplete stage set')
    source, entry = value['source'], value['entry']
    require(isinstance(source, dict) and set(source) == {'repository', 'sha', 'tree'} and
            source['repository'] == REPOSITORY and all(isinstance(source[k], str) and HEX40.fullmatch(source[k])
                                                     for k in ('sha', 'tree')), 'invalid frozen owner identity')
    selected = plan.get('test_revisions', {}).get(OWNER, {})
    require(all(source[k] == selected.get(k) for k in ('repository', 'sha')) and
            any(all(row.get(k) == v for k, v in source.items())
                for row in plan.get('run_inputs', {}).values()), 'owner identity differs from admitted inputs')
    require(isinstance(entry, dict) and set(entry) == {'path', 'mode', 'size', 'sha256', 'git_blob'} and
            entry['path'] == ENTRY and entry['mode'] in ('100644', '100755') and
            type(entry['size']) is int and 0 < entry['size'] <= ENTRY_LIMIT and
            isinstance(entry['sha256'], str) and HEX64.fullmatch(entry['sha256']) and
            isinstance(entry['git_blob'], str) and HEX40.fullmatch(entry['git_blob']), 'invalid frozen entry')
    return value


def verify_entry(plan, root):
    frozen = contract(plan)
    require(frozen is not None, 'stage requested without a supported declaration')
    data, mode = read_regular(root, ENTRY, ENTRY_LIMIT)
    require(entry_record(data, mode) == frozen['entry'] and declaration(data) == frozen['protocol'],
            'owner stage entry differs from the plan')
    return frozen


def stages_for(plan):
    frozen = contract(plan)
    return tuple(frozen['stages']) if frozen else STAGES


def groups_for(plan):
    return tuple('owner-orchestrator--' + stage for stage in stages_for(plan))


def stage_for(group):
    return ALL_STAGES[ALL_GROUPS.index(group)] if group in ALL_GROUPS else None


def check(stage, sources):
    require(stage in ALL_STAGES, 'unknown stage')
    return ('orchestrator-source-stage:' + stage,
            ['python3', '-B', EXECUTOR, '--source-stage', stage, '--plan', '/src/.source-plan.json',
             '--sources', '/src', '--output', '/output/source-stage.json'], sources / OWNER)


def expand(checks, sources, plan=None):
    """Replace only the declared whole owner gate; retain all extra checks."""
    result, count = [], 0
    for row in checks:
        if row[0] == 'orchestrator-unit-race-vet':
            require(row == (row[0], ['bash', ENTRY], sources / OWNER), 'unknown whole owner gate')
            result.extend(check(stage, sources) for stage in (stages_for(plan) if plan is not None else STAGES))
            count += 1
        else:
            result.append(row)
    require(count == 1, 'stages require exactly one whole owner gate')
    return result


def inventory(rows):
    require(isinstance(rows, list) and 0 < len(rows) <= PACKAGE_LIMIT, 'empty or oversized package inventory')
    for row in rows:
        require(isinstance(row, dict) and set(row) == {'import_path', 'has_tests'} and
                isinstance(row['import_path'], str) and PACKAGE.fullmatch(row['import_path']) and
                all(p not in ('.', '..') for p in row['import_path'].split('/')) and
                type(row['has_tests']) is bool, 'invalid package inventory member')
    names = [row['import_path'] for row in rows]
    require(names == sorted(set(names)), 'package inventory is not sorted and unique')
    return rows


def partition(rows):
    inventory(rows)
    groups, loads = [[] for _ in RACE_STAGES], [0] * len(RACE_STAGES)
    weight = lambda name: RACE_WEIGHTS.get(name.removeprefix(MODULE + '/'), 1)
    for name in sorted((row['import_path'] for row in rows), key=lambda name: (-weight(name), name)):
        index = min(range(len(groups)), key=lambda i: (loads[i], i))
        groups[index].append(name)
        loads[index] += weight(name)
    return {stage: sorted(names) for stage, names in zip(RACE_STAGES, groups)}


def decode_inventory(text):
    decoder, rows = json.JSONDecoder(), []
    while text.strip():
        value, end = decoder.raw_decode(text.lstrip())
        text = text.lstrip()[end:]
        require(isinstance(value, dict) and not value.get('Error') and not value.get('DepsErrors') and
                not value.get('Incomplete') and not value.get('ForTest'), 'go list reported an incomplete package')
        rows.append(dict(import_path=value.get('ImportPath'),
                         has_tests=bool(value.get('TestGoFiles') or value.get('XTestGoFiles'))))
        require(len(rows) <= PACKAGE_LIMIT, 'oversized package inventory')
    return inventory(sorted(rows, key=lambda row: row['import_path'] or ''))


def terminals(path, selected, rows):
    """Only package terminal events count; individual test skips keep Go semantics."""
    observed = {}
    with path.open('rb') as stream:
        for line in stream:
            require(len(line) <= JSON_LIMIT, 'oversized test event')
            event = json.loads(line)
            require(isinstance(event, dict), 'invalid race event')
            # Newer Go versions also emit build events, including dependency
            # compiler diagnostics. They never substitute a test terminal.
            if event.get('Action') == 'build-output' and isinstance(event.get('ImportPath'), str):
                continue
            require(event.get('Package') in selected, 'unexpected race package event')
            if not event.get('Test') and event.get('Action') in ('pass', 'fail', 'skip'):
                name = event['Package']
                require(name not in observed, 'duplicate package terminal event')
                observed[name] = event['Action']
    has_tests = {row['import_path']: row['has_tests'] for row in rows}
    require(set(observed) == set(selected), 'missing package terminal result')
    require(all(action == 'pass' or action == 'skip' and not has_tests[name]
                for name, action in observed.items()), 'failed or skipped tested package')
    return [{'import_path': name, 'action': observed[name]} for name in sorted(observed)]


def command_record(argv, root, env, records, *, output=None):
    row = dict(command=argv, directory=str(root), exit_code=127)
    start = time.monotonic()
    try:
        if output is None:
            completed = subprocess.run(argv, cwd=root, env=env)
        else:
            with output.open('wb') as stream:
                completed = subprocess.run(argv, cwd=root, env=env, stdout=stream)
        row['exit_code'] = completed.returncode
        if completed.returncode:
            raise subprocess.CalledProcessError(completed.returncode, argv)
    finally:
        row['wall_seconds'] = time.monotonic() - start
        records.append(row)


def go_context(value, root):
    require(isinstance(value, dict) and set(value) == set(GO_ENV) and
            all(isinstance(v, str) and len(v) <= 8192 for v in value.values()) and
            value['CGO_ENABLED'] == '1' and value['GOOS'] == 'linux' and value['GOARCH'] == 'amd64' and
            re.fullmatch(r'go[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[a-z0-9.]+)?', value['GOVERSION']),
            'invalid race toolchain context')
    # Workspace paths are private to a job. Bind its bytes rather than treating
    # a random filesystem location as a different module selection.
    work = value['GOWORK']
    require(work == 'off' or Path(work).is_absolute(), 'race workspace must be explicit')
    if work == 'off':
        work_digest = 'off'
    else:
        path = Path(work)
        data, _ = read_regular(path.parent, path.name, JSON_LIMIT)
        # The fixed Workbench /src layout makes generated local replacements
        # identical; arbitrary path substitution in a receipt is not accepted.
        work_digest = hashlib.sha256(data).hexdigest()
    return {**{key: value[key] for key in GO_ENV if key != 'GOWORK'}, 'workspace_sha256': work_digest}


def execute(plan, sources, stage, output, env):
    frozen = contract(plan)
    require(frozen is not None and stage in stages_for(plan), 'unknown execution stage')
    root = sources / OWNER
    proof = dict(schema=1, plan_id=digest(plan), protocol=frozen['protocol'], stage=stage,
                 source_contract=contract(plan), commands=[], conclusion='failure', exit_code=1)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        verify_entry(plan, root)
        if stage in RACE_STAGES:
            race_env = dict(env, CGO_ENABLED='1')
            prefix = output.parent / ('source-' + stage)
            context_path = prefix.with_suffix('.env.json')
            list_path = prefix.with_suffix('.packages.json')
            events_path = prefix.with_suffix('.events.jsonl')
            command_record(ENV_COMMAND, root, race_env, proof['commands'], output=context_path)
            context = go_context(json.loads(read_regular(context_path.parent, context_path.name, JSON_LIMIT)[0]), root)
            command_record(LIST_COMMAND, root, race_env, proof['commands'], output=list_path)
            rows = decode_inventory(read_regular(list_path.parent, list_path.name, JSON_LIMIT)[0].decode())
            selected = partition(rows)[stage]
            proof['race'] = dict(policy=RACE_POLICY, context=context, inventory=rows, selected=selected, results=[])
            if selected:
                command_record(RACE_COMMAND + selected, root, race_env, proof['commands'], output=events_path)
                proof['race']['results'] = terminals(events_path, selected, rows)
            # An empty partition is permitted only when the entire inventory
            # has fewer than four packages; never invoke go test without args.
            verify_entry(plan, root)
        else:
            command_record(['bash', ENTRY, '--ci-stage', stage], root, env, proof['commands'])
            verify_entry(plan, root)
        proof.update(conclusion='success', exit_code=0)
        return proof
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        proof.update(error=str(error), exit_code=getattr(error, 'returncode', 1) or 1)
        raise
    finally:
        output.write_text(json.dumps(proof, sort_keys=True) + '\n')


def validate_proof(plan, stage, proof, *, directory='/src/orchestrator'):
    frozen = contract(plan)
    require(stage in stages_for(plan) and isinstance(proof, dict) and frozen is not None and
            set(proof) == {'schema', 'plan_id', 'protocol', 'stage', 'source_contract', 'commands',
                           'conclusion', 'exit_code'} | ({'race'} if stage in RACE_STAGES else set()) and
            type(proof['schema']) is int and proof['schema'] == 1 and
            proof['plan_id'] == digest(plan) and proof['protocol'] == frozen['protocol'] and
            proof['stage'] == stage and proof['source_contract'] == frozen and
            proof['conclusion'] == 'success' and type(proof['exit_code']) is int and proof['exit_code'] == 0,
            'stage proof identity or success mismatch')
    expected = [['bash', ENTRY, '--ci-stage', stage]]
    if stage in RACE_STAGES:
        race = proof['race']
        require(isinstance(race, dict) and set(race) == {'policy', 'context', 'inventory', 'selected', 'results'}
                and race['policy'] == RACE_POLICY, 'unknown race proof')
        rows = inventory(race['inventory'])
        selected = partition(rows)[stage]
        require(race['selected'] == selected, 'race partition differs from complete inventory')
        context = race['context']
        require(isinstance(context, dict) and set(context) == (set(GO_ENV) - {'GOWORK'}) | {'workspace_sha256'} and
                all(isinstance(v, str) and len(v) <= 8192 for v in context.values()) and
                context['CGO_ENABLED'] == '1' and context['GOOS'] == 'linux' and context['GOARCH'] == 'amd64' and
                re.fullmatch(r'go[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[a-z0-9.]+)?', context['GOVERSION']) and
                (context['workspace_sha256'] == 'off' or HEX64.fullmatch(context['workspace_sha256'])),
                'invalid recorded race context')
        results = race['results']
        require(isinstance(results, list) and all(isinstance(row, dict) and
                set(row) == {'import_path', 'action'} for row in results) and
                [row['import_path'] for row in results] == selected, 'missing or duplicate race package result')
        has_tests = {row['import_path']: row['has_tests'] for row in rows}
        require(all(row['action'] == 'pass' or row['action'] == 'skip' and not has_tests[row['import_path']]
                    for row in results), 'failed or skipped tested package')
        expected = [ENV_COMMAND, LIST_COMMAND] + ([RACE_COMMAND + selected] if selected else [])
    commands = proof['commands']
    require(isinstance(commands, list) and all(isinstance(row, dict) for row in commands) and
            [row.get('command') for row in commands] == expected and
            all(row.get('directory') == directory and type(row.get('exit_code')) is int and row['exit_code'] == 0
                for row in commands), 'stage literal command coverage mismatch')


def validate_set(plan, proofs):
    require(set(proofs) == set(stages_for(plan)), 'missing or unexpected owner stage')
    for stage, proof in proofs.items():
        validate_proof(plan, stage, proof)
    if contract(plan)['protocol'] == CASE_PROTOCOL:
        return
    races = [proofs[stage]['race'] for stage in RACE_STAGES]
    first = races[0]
    require(all(row['inventory'] == first['inventory'] and row['context'] == first['context'] for row in races),
            'race jobs used different package inventories or toolchains')
    expected = Counter(row['import_path'] for row in first['inventory'])
    actual = Counter(name for race in races for name in race['selected'])
    require(actual == expected and all(count == 1 for count in actual.values()),
            'race package union is not complete and exactly once')
