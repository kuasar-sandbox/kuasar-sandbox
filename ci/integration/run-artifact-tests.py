#!/usr/bin/env python3
"""Invoke the shared public runner for one predeclared suite shard."""
import argparse
import codecs
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import time

import artifacts
import execution
import capability_routing


DIAGNOSTIC_BYTES = 8 * 1024**2
DIAGNOSTIC_FILE_BYTES = 1024**2
DIAGNOSTIC_INDEX_BYTES = 64 * 1024
DIAGNOSTIC_FILES = 64  # Includes the index and its atomic replacement file.
DIAGNOSTIC_CASES = 8
DIAGNOSTIC_ENTRIES = 1024
DIAGNOSTIC_CONFIG_BYTES = 64 * 1024
DIAGNOSTIC_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
DIAGNOSTIC_REASONS = {'entry_limit', 'payload_limit', 'case_limit', 'unsafe_or_unreadable_file',
                      'unsafe_or_missing_output', 'unsafe_or_missing_config', 'unsafe_or_missing_work'}


@contextmanager
def diagnostic_directory(path, dir_fd=None):
    if dir_fd is None:
        path = Path(path).absolute()
        artifacts.require(path.resolve() == path, 'linked diagnostic directory')
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def diagnostic_read(directory, name, limit, *, tail=False):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        artifacts.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                          'diagnostic is not a single-link regular file')
        artifacts.require(tail or info.st_size <= limit, 'excessive diagnostic metadata')
        offset = max(0, info.st_size - limit) if tail else 0
        stream.seek(offset)
        content = stream.read(min(limit, info.st_size))
    artifacts.require(b'\0' not in content, 'binary diagnostic content')
    # A bounded tail may start or end in a UTF-8 code point. Keep all complete
    # characters instead of rejecting an otherwise ordinary Unicode log.
    if offset:
        while content and content[0] & 0xc0 == 0x80:
            content = content[1:]
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    text = decoder.decode(content, final=not tail)
    return text.encode('utf-8'), info.st_size


def diagnostic_write(directory, name, content):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), 0o644)
        stream.write(content)


def diagnostic_budget(destination):
    # Reserve both the index and its atomic replacement, even during writes.
    # Eight case IDs, fixed counters and 62 short log names bound metadata too.
    return {'destination': Path(destination), 'fd': None, 'bytes': DIAGNOSTIC_BYTES - 2 * DIAGNOSTIC_INDEX_BYTES,
            'files': DIAGNOSTIC_FILES - 2, 'entries': DIAGNOSTIC_ENTRIES, 'cases': [], 'skipped': {}}


def diagnostic_skip(budget, reason, count=1):
    budget['skipped'][reason] = budget['skipped'].get(reason, 0) + count


def diagnostic_destination(budget):
    if budget['fd'] is None:
        destination = budget['destination']
        with diagnostic_directory(destination.parent) as parent:
            # The instance's parent is task-private. Readable child files let
            # its ordinary host owner collect outputs produced by container root.
            os.mkdir(destination.name, 0o755, dir_fd=parent)
            budget['fd'] = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.fchmod(budget['fd'], 0o755)
    return budget['fd']


def write_diagnostic_index(budget):
    index = {'schema': 1, 'stage': 'export' if 'collection' in budget else 'collection',
        'cases': budget['cases'], 'skipped': budget['skipped'],
        'limits': {'bytes': DIAGNOSTIC_BYTES, 'files': DIAGNOSTIC_FILES, 'cases': DIAGNOSTIC_CASES,
                   'directory_entries': DIAGNOSTIC_ENTRIES, 'log_bytes': DIAGNOSTIC_FILE_BYTES,
                   'index_bytes': DIAGNOSTIC_INDEX_BYTES}}
    if 'collection' in budget:
        index['collection'] = budget['collection']
    content = artifacts.canonical(index) + b'\n'
    artifacts.require(len(content) <= DIAGNOSTIC_INDEX_BYTES, 'excessive diagnostic index')
    directory = diagnostic_destination(budget)
    diagnostic_write(directory, '.index.tmp', content)
    os.replace('.index.tmp', 'index.json', src_dir_fd=directory, dst_dir_fd=directory)


def failed_case_records(public, expected):
    if (not isinstance(expected, list) or len(expected) > 256 or not isinstance(public, dict)
            or public.get('cases') != expected):
        return []
    timings = public.get('timings')
    if not isinstance(timings, list) or len(timings) > len(expected):
        return []
    records, seen = [], set()
    for row in timings:
        if not isinstance(row, dict):
            return []
        name, code, seconds = row.get('case'), row.get('exit_code'), row.get('wall_seconds')
        if (not isinstance(name, str) or not DIAGNOSTIC_NAME.fullmatch(name) or not name.endswith('.sh')
                or name not in expected or name in seen or type(code) is not int or abs(code) > 65535
                or type(seconds) not in (float, int) or not 0 <= seconds <= 7 * 86400):
            return []
        seen.add(name)
        if code:
            records.append({'case': name, 'exit_code': code, 'wall_seconds': seconds})
    return records


def safe_config_scalar(key, value):
    if not isinstance(value, str) or len(value) > 48:
        return False
    if key in ('listen', 'health_listen', 'endpoint'):
        match = re.fullmatch(r'127\.0\.0\.1:([0-9]{1,5})', value)
        return bool(match and 1 <= int(match[1]) <= 65535)
    enums = {'mode': {'tiered'}, 'backend': {'fs', 's3'}, 'type': {'embedded', 'store'},
             'direct_reads': {'true', 'false'}, 'verify_content_key': {'true', 'false'}}
    if key in enums:
        return value in enums[key]
    return key in {'rpc_timeout', 'timeout', 'counters', 'reset_after', 'disk_bytes',
                   'mem_ratio', 'bloom_bits', 'pool', 'max_inflight'} and bool(
        re.fullmatch(r'[0-9]{1,9}(?:\.[0-9]{1,6})?(?:KiB|MiB|GiB|TiB|K|M|G|ms|s|m)?', value))


def config_summary(content, size):
    scalars = []
    for match in re.finditer(r'(?:^|[{},\n])\s*(?:-\s+)?([a-z_]+):\s*([A-Za-z0-9.:_-]{1,48})(?=\s*(?:[,}\n]|$))', content.decode()):
        if safe_config_scalar(*match.groups()):
            scalars.append({'key': match[1], 'value': match[2]})
            if len(scalars) == 24:
                break
    return {'size': size, 'sha256': hashlib.sha256(content).hexdigest(),
            'literal_scalars': scalars}  # Observed literals, not a general YAML parser.


def diagnostic_log_name(name):
    if not isinstance(name, str):
        return False
    leaf = name.removeprefix('runtime/')
    return bool(DIAGNOSTIC_NAME.fullmatch(leaf) and
                (leaf.endswith(('.log', '.journal')) or name == 'phases.jsonl'))


def copy_diagnostic_logs(source, destination, budget, prefix=''):
    names, runtime = [], False
    with os.scandir(source) as entries:
        while budget['entries']:
            entry = next(entries, None)
            if entry is None:
                break
            budget['entries'] -= 1
            if not prefix and entry.name == 'runtime':
                runtime = True
            elif diagnostic_log_name(prefix + entry.name):
                names.append(entry.name)
        else:
            diagnostic_skip(budget, 'entry_limit')
    copied = []
    for name in sorted(names):
        if budget['files'] == 0 or budget['bytes'] == 0:
            diagnostic_skip(budget, 'payload_limit')
            break
        try:
            content, size = diagnostic_read(source, name, min(DIAGNOSTIC_FILE_BYTES, budget['bytes']), tail=True)
        except (OSError, ValueError):
            diagnostic_skip(budget, 'unsafe_or_unreadable_file')
            continue
        # Reserve before writing: a partial write that fails still consumes
        # bounded space and cannot be retried into an uncounted extra file.
        budget['files'] -= 1
        budget['bytes'] -= len(content)
        diagnostic_write(destination, name, content)
        copied.append({'name': prefix + name, 'observed_size': size, 'retained_bytes': len(content),
                       'truncated': len(content) < size})
    if runtime:
        # Orchestrator cleanup copies its private WORK logs into OUT/runtime.
        # This is the only nested directory; it shares every global budget.
        try:
            with diagnostic_directory('runtime', source) as runtime_source:
                os.mkdir('runtime', 0o755, dir_fd=destination)
                with diagnostic_directory('runtime', destination) as runtime_target:
                    os.fchmod(runtime_target, 0o755)
                    copied.extend(copy_diagnostic_logs(runtime_source, runtime_target, budget, 'runtime/'))
        except (OSError, ValueError):
            diagnostic_skip(budget, 'unsafe_or_missing_output')
    return copied


def capture_failed_diagnostics(state, public, expected, budget):
    failures = failed_case_records(public, expected)
    if not failures:
        return
    destination = diagnostic_destination(budget)
    with diagnostic_directory(state) as source:
        for position, record in enumerate(failures):
            if len(budget['cases']) == DIAGNOSTIC_CASES:
                diagnostic_skip(budget, 'case_limit', len(failures) - position)
                break
            name = record['case']
            os.mkdir(name, 0o755, dir_fd=destination)
            item = dict(record, logs=[], configs={})
            budget['cases'].append(item)
            with diagnostic_directory(name, destination) as target:
                os.fchmod(target, 0o755)
                try:
                    with diagnostic_directory('out', source) as out, diagnostic_directory(name, out) as case:
                        item['logs'] = copy_diagnostic_logs(case, target, budget)
                except (OSError, ValueError):
                    diagnostic_skip(budget, 'unsafe_or_missing_output')
                try:
                    with diagnostic_directory('cases', source) as work, diagnostic_directory(name, work) as case:
                        for config in ('cache.yaml', 'store.yaml'):
                            try:
                                content, size = diagnostic_read(case, config, DIAGNOSTIC_CONFIG_BYTES)
                                item['configs'][config] = config_summary(content, size)
                            except (OSError, ValueError):
                                diagnostic_skip(budget, 'unsafe_or_missing_config')
                except (OSError, ValueError):
                    diagnostic_skip(budget, 'unsafe_or_missing_work')
    write_diagnostic_index(budget)


def export_failed_diagnostics(source, destination, public):
    """Reapply the same bounds to the stopped instance's retained evidence."""
    expected = public.get('cases', []) if isinstance(public, dict) else []
    failures = {row['case']: row for row in failed_case_records(public, expected)}
    if not failures:
        return
    budget = diagnostic_budget(destination)
    try:
        with diagnostic_directory(source) as source_fd:
            content, _ = diagnostic_read(source_fd, 'index.json', DIAGNOSTIC_INDEX_BYTES)
            index = json.loads(content)
            artifacts.require(isinstance(index, dict) and index.get('schema') == 1 and
                isinstance(index.get('cases'), list) and len(index['cases']) <= DIAGNOSTIC_CASES,
                'invalid failure diagnostic index')
            # Re-reading an already retained tail cannot reveal the original
            # log size or earlier omissions. Preserve only bounded, typed
            # collection facts, separately from this export's own observations.
            skipped = index.get('skipped', {})
            limits = index.get('limits', {})
            artifacts.require(isinstance(skipped, dict) and isinstance(limits, dict),
                              'invalid diagnostic collection summary')
            maxima = {'bytes': DIAGNOSTIC_BYTES, 'files': DIAGNOSTIC_FILES, 'cases': DIAGNOSTIC_CASES,
                      'directory_entries': DIAGNOSTIC_ENTRIES, 'log_bytes': DIAGNOSTIC_FILE_BYTES,
                      'index_bytes': DIAGNOSTIC_INDEX_BYTES}
            budget['collection'] = {
                'skipped': {key: skipped[key] for key in DIAGNOSTIC_REASONS if key in skipped
                            and type(skipped[key]) is int and 0 <= skipped[key] <= 4096},
                'limits': {key: limits[key] for key, maximum in maxima.items() if key in limits
                           and type(limits[key]) is int and 0 <= limits[key] <= maximum}}
            collection_logs = 0
            target = diagnostic_destination(budget)
            seen = set()
            for entry in index['cases']:
                name = entry.get('case') if isinstance(entry, dict) else None
                artifacts.require(isinstance(name, str) and name in failures and name not in seen,
                                  'unselected failure diagnostics')
                seen.add(name)
                item = dict(failures[name], logs=[], configs={})
                logs = entry.get('logs')
                artifacts.require(isinstance(logs, list), 'invalid diagnostic collection logs')
                collection_logs += len(logs)
                artifacts.require(collection_logs <= DIAGNOSTIC_FILES - 2,
                                  'excessive diagnostic collection logs')
                item['collection_logs'] = []
                log_names = set()
                for log in logs:
                    if not isinstance(log, dict):
                        continue
                    filename = log.get('name')
                    size, kept, truncated = log.get('observed_size'), log.get('retained_bytes'), log.get('truncated')
                    if (not diagnostic_log_name(filename) or filename in log_names
                            or type(size) is not int or not 0 <= size <= 2**63 - 1
                            or type(kept) is not int or not 0 <= kept <= min(size, DIAGNOSTIC_FILE_BYTES)
                            or type(truncated) is not bool or truncated != (kept < size)):
                        continue
                    log_names.add(filename)
                    item['collection_logs'].append({'name': filename, 'observed_size': size,
                        'retained_bytes': kept, 'truncated': truncated})
                configs = entry.get('configs')
                artifacts.require(isinstance(configs, dict) and len(configs) <= 2, 'invalid diagnostic config summaries')
                for config, summary in configs.items():
                    if config not in ('cache.yaml', 'store.yaml') or not isinstance(summary, dict):
                        continue
                    digest, size, scalars = summary.get('sha256'), summary.get('size'), summary.get('literal_scalars')
                    if (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                            or type(size) is not int or not 0 <= size <= DIAGNOSTIC_CONFIG_BYTES
                            or not isinstance(scalars, list) or len(scalars) > 24):
                        continue
                    safe = [row for row in scalars if isinstance(row, dict) and set(row) == {'key', 'value'}
                            and isinstance(row['key'], str) and safe_config_scalar(row['key'], row['value'])]
                    item['configs'][config] = {'sha256': digest, 'size': size, 'literal_scalars': safe}
                os.mkdir(name, 0o755, dir_fd=target)
                with diagnostic_directory(name, target) as case_target:
                    os.fchmod(case_target, 0o755)
                    try:
                        with diagnostic_directory(name, source_fd) as case_source:
                            item['logs'] = copy_diagnostic_logs(case_source, case_target, budget)
                    except (OSError, ValueError):
                        diagnostic_skip(budget, 'unsafe_or_missing_output')
                budget['cases'].append(item)
            write_diagnostic_index(budget)
    finally:
        if budget['fd'] is not None:
            os.close(budget['fd'])


def execute(plan, arch, shard, workspace, result_path, clean_image=None, runner=None, executor_image=None):
    provenance = artifacts.verify_workspace(workspace, plan, arch)
    artifacts.require(platform.machine() == arch, "E2E requires the selected native target runner")
    groups = artifacts.plan_shards(plan, arch)
    artifacts.require(shard in groups, "unselected E2E shard")
    result = execution.result_record(plan, arch, provenance, workspace)
    result.update(shard=shard, cases=groups[shard], environment={"kind": "native-host"})
    if plan['schema'] >= 4:
        group = plan['lanes'][arch]['groups'][shard]
        profile = plan['runner_configuration']['value']['profiles'][group['profile']]
        runner = runner if runner is not None else capability_routing.runner_identity(plan, arch, shard)
        capability_routing.check_runner(plan, arch, profile, runner)
        artifacts.require((profile['executor'] == 'workbench-system') == bool(executor_image), 'wrong E2E executor')
        artifacts.require((group['runtime'] == 'runtime-only') == bool(clean_image), 'wrong E2E runtime environment')
        result['execution'] = {'profile': group['profile'], 'executor': profile['executor'], 'runner': runner,
                               'configuration_sha256': plan['runner_configuration']['sha256']}
        if executor_image:
            artifacts.require(re.fullmatch(r'sha256:[0-9a-f]{64}', executor_image), 'invalid local Workbench image')
            result['execution']['image_id'] = executor_image
            result['execution']['reference'] = plan['execution_images'][arch]['reference']
            result['environment'] = {'kind': 'workbench-system', 'image_id': executor_image}
    started = time.monotonic()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    diagnostic = diagnostic_budget(result_path.parent / 'case-diagnostics')
    failure = None
    batches = [result['cases']] if result['cases'] else []
    if all(case.split('.', 1)[0] == 'snapshot' for case in result['cases']):
        batches = [[case] for case in result['cases']]
    try:
        # Snapshot cases retain disks and full-RAM observations in WORK and OUT.
        # Reclaim their private state before the next case. Other suites keep
        # their existing single invocation and cross-case template preparation.
        # The shipped runner, prepared inputs and selected coverage are unchanged.
        for batch in batches:
            with execution.scratch_directory() as state:
                environment, prepared = execution.environment(state)
                report = state / "result.json"
                selection = [value for case in batch for value in ('--include', case)]
                command = ["python3", "-B", str(workspace / "test/e2e/e2e"), "run",
                           "--workdir", str(workspace), "--arch", arch,
                           "--run-root", str(state / "cases"), "--out-root", str(state / "out"),
                           "--result", str(report), *selection]
                if clean_image:
                    arguments = ['--arch', arch, *selection]
                    command = execution.clean_command(clean_image, workspace, state, arguments)
                    result['environment'] = execution.runtime_identity(clean_image)
                else:
                    command = execution.privileged_command(command, prepared)
                public = diagnostic_public = None
                try:
                    completed = subprocess.run(command, cwd=state, env=environment)
                    artifacts.require(report.is_file(), "public runner did not write execution evidence")
                    public = json.loads(report.read_text())
                    artifacts.require(public["arch"] == arch and public["cases"] == batch and
                                      public["provenance_sha256"] == result["provenance_sha256"],
                                      "public runner result differs from selected inputs")
                    diagnostic_public = public
                    result["timings"].extend(public["timings"])
                    if public["conclusion"] != "success":
                        failure = "public runner reported failure"
                    elif completed.returncode:
                        failure = "shared public runner failed"
                    else:
                        artifacts.check_timings(public["timings"], batch)
                        if plan['schema'] >= 4:
                            observed = public.get('capabilities')
                            required = {cap for case in batch for cap in plan['case_requirements']['cases'][case]['requires']}
                            previous = result.setdefault('capabilities', {})
                            artifacts.require(isinstance(observed, dict) and set(observed) == set(required) and
                                              all(cap not in previous or previous[cap] == value
                                                  for cap, value in observed.items()),
                                              'missing or changed case capability startup evidence')
                            previous.update(observed)
                finally:
                    # The scratch context removes even failed cases. Export only
                    # explicit failed timing rows before that existing cleanup.
                    try:
                        capture_failed_diagnostics(state, diagnostic_public, batch, diagnostic)
                    except (OSError, ValueError) as error:
                        errors = result.setdefault('diagnostic_errors', {})
                        kind = type(error).__name__
                        errors[kind] = errors.get(kind, 0) + 1
        artifacts.require(failure is None, failure)
        artifacts.check_timings(result["timings"], result["cases"])
        artifacts.verify_workspace(workspace, plan, arch)
        if plan['schema'] >= 4:
            capability_routing.check_result(plan, arch, shard, result)
        if clean_image and result['cases']:
            result['environment']['verified'] = True
        result["conclusion"] = "success"
    finally:
        if diagnostic['fd'] is not None:
            os.close(diagnostic['fd'])
        result["wall_seconds"] = time.monotonic() - started
        result_path.write_bytes(artifacts.canonical(result) + b"\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--arch", required=True, choices=artifacts.ARCHES)
    parser.add_argument("--shard", required=True)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--clean-image", help="immutable runtime-only image for source/compiler-free acceptance")
    parser.add_argument('--runner-identity', type=Path, help='trusted host runner receipt for Workbench execution')
    parser.add_argument('--executor-image', help='verified immutable Workbench image ID')
    args = parser.parse_args()
    execute(json.loads(args.plan.read_text()), args.arch, args.shard, args.workspace.resolve(), args.result.resolve(),
            args.clean_image, json.loads(args.runner_identity.read_text()) if args.runner_identity else None, args.executor_image)


if __name__ == "__main__":
    main()
