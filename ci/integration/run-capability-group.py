#!/usr/bin/env python3
"""Execute a planned E2E group using the existing native or Workbench entry.

The host only verifies/transports inputs and controls the owned container. It
does not run product cases, build products, or expose its Docker socket or
runner credentials inside the supplemental execution environment.
"""
import argparse
import errno
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

import artifacts
import image_pull
import capability_routing as routing
import runtime_archive

ROOT = Path(__file__).resolve().parents[2]


def run_module():
    spec = importlib.util.spec_from_file_location('run_artifact_tests', Path(__file__).with_name('run-artifact-tests.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def instance(root):
    # The state path already binds runner/run/attempt/arch/group. Docker names
    # must also differ when a machine hosts more than one runner service.
    name = 'e2e-' + hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:20]
    return root / name


def command(root, *arguments):
    return [sys.executable, '-B', str(ROOT / 'workbench/workbench'), '--root', str(root),
            '--name', instance(root).name, *map(str, arguments)]


def cleanup(root, delete_output=False):
    if (instance(root) / 'instance.json').exists():
        subprocess.run(command(root, 'cleanup', *(['--delete-output'] if delete_output else [])), check=True, stdin=subprocess.DEVNULL, timeout=600)


def copy_input(source, target):
    try:
        os.link(source, target)
    except OSError as error:
        if error.errno not in (errno.EXDEV, errno.EOPNOTSUPP):
            raise
        shutil.copy2(source, target)
    return str(target)


def read_owned(path, limit=2 * 1024 * 1024):
    """Never follow candidate-created output links during host collection."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        artifacts.require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, 'unsafe or excessive execution evidence')
        content = stream.read(limit + 1)
        artifacts.require(len(content) <= limit, 'execution evidence grew beyond its limit')
        return content


def startup_daemons(container_id):
    """Read only this owned container's private units after startup has failed."""
    properties = ('Id', 'ActiveState', 'SubState', 'MainPID', 'Result', 'ExecMainCode', 'ExecMainStatus')
    signals = ('context deadline exceeded', 'context canceled', 'connection refused', 'connection reset',
               'broken pipe', 'unexpected EOF', 'i/o timeout', 'no space left on device', 'permission denied',
               'failed to start daemon', 'failed to dial', 'failed to mount', 'failed to initialize',
               'failed to connect', 'timeout', 'timed out', 'fatal', 'panic:', 'SIGKILL',
               'Starting', 'Started', 'Stopping', 'Stopped', 'API listen', 'initialization has completed')
    commands = {
        'units': ['systemctl', 'show', 'docker.service', 'containerd.service',
                  *['--property=' + key for key in properties]],
        'journal': ['journalctl', '--no-pager', '--quiet', '--output=json',
                    '--output-fields=MESSAGE,PRIORITY,_PID,__REALTIME_TIMESTAMP,_SYSTEMD_UNIT',
                    '--lines=200', '--unit=docker.service', '--unit=containerd.service'],
    }
    evidence = {'observed_ns': time.time_ns(), 'scope': 'owned container private Docker/containerd',
                'redaction': 'fixed error vocabulary and selected unit fields only'}
    for name, command in commands.items():
        item = {}
        # Temporary output bounds memory; at most 200 journal rows, no --all,
        # environment, command line, registry settings or client credentials.
        with tempfile.TemporaryFile() as stream:
            try:
                result = subprocess.run(['docker', 'exec', container_id, *command],
                    stdout=stream, stderr=subprocess.DEVNULL, timeout=5)
                size = stream.tell()
                stream.seek(max(0, size - 65536))
                text = stream.read(65536).decode('utf-8', 'replace')
                item.update(exit_code=result.returncode, tail_truncated=size > 65536)
            except (OSError, subprocess.TimeoutExpired) as error:
                evidence[name] = {'error': type(error).__name__}
                continue
        rows = []
        for line in text.splitlines():
            if name == 'units':
                key, _, value = line.partition('=')
                if key in properties and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', value):
                    rows.append({key: value})
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or not isinstance(event.get('MESSAGE'), str):
                continue
            terms = [term for term in signals if re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', event['MESSAGE'], re.I)]
            if not terms:
                continue
            row = {'terms': terms}
            for key in ('__REALTIME_TIMESTAMP', '_PID', 'PRIORITY'):
                value = event.get(key)
                if isinstance(value, str) and re.fullmatch(r'[0-9]{1,24}', value):
                    row[key] = value
            if event.get('_SYSTEMD_UNIT') in ('docker.service', 'containerd.service'):
                row['unit'] = event['_SYSTEMD_UNIT']
            rows.append(row)
        item['rows'] = rows
        evidence[name] = item
    return evidence


def workbench_launcher():
    loader = importlib.machinery.SourceFileLoader('workbench_failure_reader', str(ROOT / 'workbench/workbench'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def capture_startup_daemons(root):
    state = instance(root)
    if not (state / 'instance.json').exists():
        return
    # Reuse the launcher's exact UID/directory/container label and image checks.
    # No separate diagnostic adoption or host-daemon fallback is permitted.
    launcher = workbench_launcher()
    data = launcher.record(state)
    if data.get('status') != 'failed-start' or data.get('mode') != 'system':
        return
    container = launcher.owned_container(data)
    if not container or not container['State'].get('Running'):
        return
    evidence = startup_daemons(container['Id'])
    output = state / 'output'
    artifacts.require(output.resolve() == output, 'escaping diagnostic directory')
    output.mkdir(exist_ok=True)
    with (output / ('startup-daemons-' + str(time.time_ns()) + '.log')).open('x') as stream:
        stream.write(json.dumps(evidence) + '\n')


def diagnostics(root, destination):
    if not (instance(root) / 'instance.json').exists():
        return
    artifacts.require(not destination.exists(), 'diagnostic output must be fresh')
    destination.mkdir(parents=True, mode=0o700)
    paths = [instance(root) / 'instance.json']
    for pattern in ('container-*.json', 'startup-*.log'):
        paths.extend(sorted((instance(root) / 'output').glob(pattern))[-4:])
    for path in paths:
        artifacts.require(path.parent.resolve() in {instance(root), instance(root) / 'output'}, 'escaping diagnostic directory')
        (destination / path.name).write_bytes(read_owned(path))


def execute(args):
    plan = json.loads(args.plan.read_text())
    artifacts.check_plan(plan)
    artifacts.require(args.arch == platform.machine(), 'E2E group requires the selected native architecture')
    provenance = artifacts.verify_workspace(args.workspace, plan, args.arch)
    if plan['schema'] >= 4:
        group = plan['lanes'][args.arch]['groups'][args.group]
        profile = plan['runner_configuration']['value']['profiles'][group['profile']]
        artifacts.require(plan['runner_configuration']['value'] == routing.read_config(), 'executor profile differs from trusted framework')
        runner = routing.runner_identity(plan, args.arch, args.group)
        executor, runtime = profile['executor'], group['runtime']
    else:
        artifacts.require(args.group in artifacts.plan_shards(plan, args.arch), 'unselected historical shard')
        executor, runtime = 'native', 'runtime-only' if artifacts.requires_clean_runtime(args.arch, args.group) else 'system'
        runner = None
    if executor == 'native':
        image = runtime_archive.load(args.runtime, provenance) if runtime == 'runtime-only' else None
        return run_module().execute(plan, args.arch, args.group, args.workspace, args.result, image, runner)

    artifacts.require(not args.root.exists() and not args.inputs.exists(), 'supplemental E2E needs fresh task-owned directories')
    selected = plan['execution_images'][args.arch]
    existing = subprocess.run(['docker', 'image', 'inspect', selected['image_id']], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if existing.returncode:
        image_pull.pull_image(selected['reference'])
    # The launcher verifies native architecture, rootful Engine, image origin
    # and private namespaces. The exact config ID is fixed by the plan.
    subprocess.run([sys.executable, '-B', str(ROOT / 'workbench/workbench'), 'check',
                    '--image', selected['image_id'], '--mode', 'system'], check=True)
    args.inputs.mkdir(parents=True, mode=0o700)
    shutil.copytree(args.workspace, args.inputs / 'prepared', copy_function=copy_input)
    shutil.copytree(args.readers, args.inputs / 'readers', copy_function=copy_input)
    # Sparse trusted controller files are data inputs, separate from prepared
    # product inputs. No .git directory or checkout credentials are transported.
    for directory in ('ci/integration', 'ci/hosted', 'test/e2e/lib', 'release'):
        shutil.copytree(ROOT / directory, args.inputs / 'framework' / directory,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'), copy_function=copy_input)
    (args.inputs / 'framework/test/e2e').mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'test/e2e/package_inputs.py', args.inputs / 'framework/test/e2e/package_inputs.py')
    (args.inputs / 'plan.json').write_bytes(artifacts.canonical(plan) + b'\n')
    (args.inputs / 'runner.json').write_bytes(artifacts.canonical(runner) + b'\n')
    if runtime == 'runtime-only':
        artifacts.require(artifacts.digest(args.runtime) == provenance['preparation_environment']['archive_sha256'],
                          'transported runtime archive differs from preparation')
        copy_input(args.runtime, args.inputs / 'runtime.tar.gz')
    args.result.parent.mkdir(parents=True, exist_ok=True)
    result = {'conclusion': 'failure', 'arch': args.arch, 'shard': args.group, 'plan_id': artifacts.identity(plan),
              'cases': group['cases'], 'timings': []}
    failed = False
    try:
        subprocess.run(command(args.root, 'start', '--mode', 'system', '--image', selected['image_id'],
                               '--inputs', args.inputs, '--cpus', profile['resources']['cpus'],
                               '--memory-gib', profile['resources']['memory_gib']),
                       check=True, stdin=subprocess.DEVNULL, timeout=300)
        internal = ['/inputs/release/framework/ci/integration/run-capability-group.py', '--inside',
                    '--plan', '/inputs/release/plan.json', '--arch', args.arch, '--group', args.group,
                    '--workspace', '/inputs/release/prepared', '--runtime', '/inputs/release/runtime.tar.gz',
                    '--result', '/output/result.json', '--runner-identity', '/inputs/release/runner.json']
        subprocess.run(command(args.root, 'exec', '--', 'env',
                               'PATH=/inputs/release/readers:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                               'KUASAR_RUNTIME_READER=/inputs/release/readers/runtime-payloads.py',
                               'python3', '-B', *internal), check=True, stdin=subprocess.DEVNULL,
                       timeout=profile['resources']['timeout_minutes'] * 60)
        result = json.loads(read_owned(instance(args.root) / 'output/result.json'))
        routing.check_result(plan, args.arch, args.group, result)
        artifacts.verify_workspace(args.workspace, plan, args.arch)
        return result
    except BaseException as error:
        failed = True
        if not isinstance(error, (KeyboardInterrupt, InterruptedError)):
            try:
                capture_startup_daemons(args.root)
            except (OSError, ValueError, subprocess.SubprocessError) as diagnostic_error:
                # Keep the original start/case failure and always run owned cleanup.
                result['startup_diagnostic_error'] = type(diagnostic_error).__name__
        report = instance(args.root) / 'output/result.json'
        if report.exists() or report.is_symlink():
            try:
                result = json.loads(read_owned(report))
            except (OSError, ValueError) as evidence_error:
                result['evidence_error'] = str(evidence_error)
        result['conclusion'] = 'cancelled' if isinstance(error, (KeyboardInterrupt, InterruptedError)) else 'failure'
        result['error'] = str(error)
        raise
    finally:
        try:
            cleanup(args.root)
        except BaseException as error:
            result['conclusion'] = 'failure'
            result['cleanup_error'] = str(error)
            if not failed:
                raise
        finally:
            args.result.write_bytes(artifacts.canonical(result) + b'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('plan', 'workspace', 'runtime', 'result', 'readers', 'root', 'inputs', 'runner-identity', 'diagnostics'):
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--arch', choices=artifacts.ARCHES)
    parser.add_argument('--group')
    parser.add_argument('--inside', action='store_true')
    parser.add_argument('--cleanup', action='store_true')
    parser.add_argument('--delete-output', action='store_true')
    args = parser.parse_args()
    for key, value in vars(args).items():
        if isinstance(value, Path):
            setattr(args, key, value.resolve())
    if args.cleanup:
        try:
            cleanup(args.root, args.delete_output)
        finally:
            if args.diagnostics:
                diagnostics(args.root, args.diagnostics)
        return
    if args.inside:
        plan = json.loads(args.plan.read_text())
        group = plan['lanes'][args.arch]['groups'][args.group]
        provenance = artifacts.verify_workspace(args.workspace, plan, args.arch)
        image = runtime_archive.load(args.runtime, provenance) if group['runtime'] == 'runtime-only' else None
        return run_module().execute(plan, args.arch, args.group, args.workspace, args.result, image,
                                    json.loads(args.runner_identity.read_text()), plan['execution_images'][args.arch]['image_id'])
    def interrupted(signum, frame):
        raise InterruptedError('E2E group cancelled by signal ' + str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    return execute(args)


if __name__ == '__main__':
    main()
