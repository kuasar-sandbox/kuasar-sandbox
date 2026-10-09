#!/usr/bin/env python3
"""Bind existing Workbench lifecycle operations to one admitted CI selection."""
from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import signal
import shutil
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ci/integration'))
import artifacts

REGISTRY = 'ghcr.io/kuasar-sandbox/workbench'
PLATFORMS = {'x86_64': 'amd64', 'aarch64': 'arm64'}
CACHE_PATHS = ('build/native-cache', 'home/go/pkg/mod', 'home/go-cache',
               'home/.cargo/registry', 'home/.cargo/git', 'home/.cargo/release-materials')


def module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    loader.exec_module(loaded)
    return loaded


launcher = module('ci_workbench_launcher', ROOT / 'workbench/workbench')
cache_policy = module('ci_workbench_cache_scope', ROOT / 'ci/hosted/cache-scope.py')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def output(command, **kwargs):
    return subprocess.check_output(list(map(str, command)), text=True, **kwargs).strip()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
    temporary.replace(path)


def framework_sha():
    return output(['git', '-C', ROOT, 'rev-parse', 'HEAD'])


def from_aggregate(aggregate, framework):
    require(aggregate.get('delivery') == 'workbench-v1' and not aggregate.get('staged'),
            'builds require a published and admitted Workbench')
    binding = {'registry': aggregate['registry'], 'workbench': aggregate['workbench']}
    artifacts.check_registry_binding(aggregate['version'], binding)
    selected = {'framework_sha': framework, 'aggregate_version': aggregate['version'],
                'source_revision': aggregate['sha'], 'validation_run': aggregate['validation_run'],
                'registry_index': aggregate['registry']['digest'], 'architectures': {}}
    for arch, row in aggregate['registry']['architectures'].items():
        validated = aggregate['workbench'][arch]
        selected['architectures'][arch] = {
            'reference': REGISTRY + '@' + row['digest'], 'image_id': row['image_id'],
            'archive': validated['archive'], 'archive_sha256': row['archive_sha256'],
            'archive_size': validated['size'], 'qualification_scope': validated['qualification_scope']}
    return check_selection(selected)


def check_selection(selected, framework=None):
    for key in ('framework_sha', 'source_revision'):
        require(re.fullmatch('[0-9a-f]{40}', selected.get(key, '')), 'missing exact ' + key)
    require(framework is None or selected['framework_sha'] == framework,
            'Workbench selection belongs to a different trusted framework')
    require(re.fullmatch(r'release-v\d+\.\d+\.\d+(?:-preview\.\d{8}(?:\.\d+)?)?',
                         selected.get('aggregate_version', '')), 'invalid Workbench release')
    require(set(selected.get('architectures', {})) == set(PLATFORMS),
            'Workbench selection must fix both native architectures')
    require(re.fullmatch('sha256:[0-9a-f]{64}', selected.get('registry_index', '')),
            'missing immutable registry index')
    for arch, row in selected['architectures'].items():
        require(re.fullmatch(re.escape(REGISTRY) + '@sha256:[0-9a-f]{64}', row.get('reference', ''))
                and re.fullmatch('sha256:[0-9a-f]{64}', row.get('image_id', ''))
                and re.fullmatch('[0-9a-f]{64}', row.get('archive_sha256', ''))
                and type(row.get('archive_size')) is int and row['archive_size'] > 0,
                'incomplete immutable Workbench identity: ' + arch)
        require(row.get('qualification_scope') in ({'native-full'} if arch == 'x86_64' else {'artifact-only', 'native-full'}),
                'Workbench lacks its declared architecture qualification')
    return selected


def select(args):
    require(framework_sha() == args.framework_sha, 'selector differs from trusted framework revision')
    resolver = module('ci_workbench_resolver', ROOT / 'ci/integration/resolve-artifacts.py')
    if args.plan:
        plan = json.loads(args.plan.read_text())
        artifacts.check_plan(plan)
        require(plan['framework_sha'] == args.framework_sha, 'plan framework mismatch')
        aggregate = plan['baseline'] if plan['mode'] == 'source' else resolver.baseline(args.framework_sha, 'main')
    else:
        aggregate = resolver.baseline(args.framework_sha, 'main')
    require(not args.output.exists(), 'image selection is immutable; output already exists')
    write(args.output, from_aggregate(aggregate, args.framework_sha))


def verify_selection(args):
    require(framework_sha() == args.framework_sha, 'verifier differs from the trusted framework')
    check_selection(json.loads(args.selection.read_text()), args.framework_sha)


def image_check(selected, arch, image):
    row = selected['architectures'][arch]
    require(image and image['Id'] == row['image_id'] and image['Os'] == 'linux'
            and image['Architecture'] == PLATFORMS[arch], 'Workbench image/config/architecture mismatch')
    labels = image['Config'].get('Labels') or {}
    require(labels.get('org.opencontainers.image.version') == selected['aggregate_version']
            and labels.get('org.opencontainers.image.revision') == selected['source_revision']
            and labels.get('org.opencontainers.image.source') == 'https://github.com/kuasar-sandbox/kuasar-sandbox',
            'Workbench image release/source mismatch')


def acquire(selected, arch):
    require(platform.machine() == arch, 'Workbench builds require the selected native runner')
    row = selected['architectures'][arch]
    started = time.monotonic()
    existing = launcher.inspect('image', row['image_id'])
    if existing is None:
        subprocess.run(['docker', 'pull', row['reference']], check=True, timeout=1800)
    imported = launcher.inspect('image', row['image_id'])
    image_check(selected, arch, imported)
    return {'image_present_before': existing is not None, 'acquire_seconds': time.monotonic() - started,
            'local_image_size': imported['Size'], **row}


def command(root, *args):
    return [sys.executable, '-B', str(ROOT / 'workbench/workbench'), '--root', str(root), '--name', 'ci', *map(str, args)]


def start(args):
    selected = check_selection(json.loads(args.selection.read_text()), framework_sha())
    require(os.getuid() != 0, 'ordinary CI builds require an ordinary invoking UID')
    sources, root = args.sources.resolve(), args.root.absolute()
    require(sources.is_dir() and not ROOT.is_relative_to(sources) and not sources.is_relative_to(ROOT),
            'private candidate sources must be separate from the trusted framework')
    require(not root.exists(), 'CI state requires a fresh task directory')
    root.mkdir(parents=True, mode=0o700)
    receipt = {'conclusion': 'failure', 'framework_sha': selected['framework_sha'], 'selection': selected,
               'arch': args.arch, 'cpus': args.cpus, 'memory_gib': args.memory_gib,
               'owner_uid': os.getuid(), 'mode': args.mode, 'sources': str(sources),
               'started_ns': time.time_ns(), 'commands': []}
    write(root / 'receipt.json', receipt)
    try:
        receipt['image'] = acquire(selected, args.arch)
        options = ['start', '--image', receipt['image']['image_id'], '--mode', args.mode,
                   '--cpus', args.cpus, '--memory-gib', args.memory_gib, '--network', args.network,
                   '--inputs', ROOT]
        if args.mode == 'build':
            options += ['--source', sources]
        subprocess.run(command(root / 'instances', *options), check=True)
        if args.mode == 'system':
            state = root / 'instances/ci'
            data = launcher.record(state)
            container = launcher.owned_container(data)
            require(container and container['State']['Running'], 'system instance did not become ready')
            subprocess.run(command(root / 'instances', 'exec', '--', 'mkdir', '/src'), check=True)
            # System checks operate on a private container copy, without a
            # writable mount of the host source tree or any host credentials.
            subprocess.run(['docker', 'cp', str(sources) + '/.', container['Id'] + ':/src'],
                           check=True, timeout=600)
        receipt['conclusion'] = 'running'
    finally:
        write(root / 'receipt.json', receipt)
    if args.output:
        state = root / 'instances/ci'
        with args.output.open('a') as stream:
            stream.write('root=' + str(root) + '\nstate=' + str(state) + '\n')


def cache_key(args):
    # These are actual native input keys, computed by the matching compiler
    # image at the same fixed paths as the build. Daily labels/task IDs are not
    # compiler inputs. Restored entries still undergo native-cache verification.
    components = {'vmlinux': 'guest-runtime', 'erofs': 'guest-runtime', 'envd': 'guest-runtime',
                  'rocksdb': 'accelerator', 'cloud-hypervisor': 'sandboxer'}
    records = {}
    workspaces = [args.sources] + [args.sources / name for name in ('kernel-unit', 'test-helpers', 'test-overlays')
                                  if (args.sources / name).is_dir()]
    for workspace in workspaces:
        for name, owner in components.items():
            if (workspace / owner / 'Makefile').is_file():
                label = str(workspace.relative_to(args.sources)) + '/' + name
                records[label] = output([ROOT / 'ci/native-cache/native-cache.sh', 'key', name],
                                        env={**os.environ, 'KUASAR_WORKSPACE_ROOT': str(workspace)})
    for tool, argv in {'go': ['go', 'version'], 'rust': ['rustc', '-vV']}.items():
        records[tool] = output(argv)
    for workspace in workspaces:
        for owner in sorted(workspace.iterdir()):
            if not owner.is_dir():
                continue
            for name in ('go.mod', 'go.sum', 'Cargo.lock'):
                path = owner / name
                if path.is_file():
                    records[str(path.relative_to(args.sources))] = hashlib.sha256(path.read_bytes()).hexdigest()
    print(hashlib.sha256(artifacts.canonical(records)).hexdigest())


def cache_scope(args):
    result = cache_policy.decide(args.sources, args.receipt)
    print(result['namespace'])


def cache_coverage(args):
    coverage = module('ci_workbench_cache_coverage', ROOT / 'ci/hosted/cache-coverage.py')
    print(coverage.digest(coverage.identity(args.name, args.sources, args.arch)))


def execute(args):
    root = args.root.resolve()
    receipt = json.loads((root / 'receipt.json').read_text())
    require(receipt['framework_sha'] == framework_sha(), 'executor framework changed')
    argv = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    require(argv, 'missing CI build command')
    started = time.monotonic()
    env = ['env', 'TARGET_ARCH=' + receipt['arch'], 'ORG=/src', 'KUASAR_WORKSPACE_ROOT=/src',
           'KUASAR_NATIVE_CACHE_ROOT=/build/native-cache', 'KUASAR_NATIVE_CACHE_METRICS=/output/native-cache.tsv',
           'PYTHONDONTWRITEBYTECODE=1', 'GIT_CONFIG_NOSYSTEM=1', 'GOTOOLCHAIN=local',
           'KUASAR_WORKBENCH_IMAGE_ID=' + receipt['image']['image_id'],
           'KUASAR_WORKBENCH_FRAMEWORK_SHA=' + receipt['framework_sha'],
           'GOPROXY=https://proxy.golang.org,direct', 'GOSUMDB=sum.golang.org']
    class Interrupted(Exception):
        def __init__(self, signum):
            self.signum = signum

    def interrupted(signum, _frame):
        raise Interrupted(signum)

    handlers = {signum: signal.signal(signum, interrupted) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        completed = subprocess.run(command(root / 'instances', 'exec', '--', *env, *argv), timeout=args.timeout)
        code = completed.returncode if completed.returncode >= 0 else 128 - completed.returncode
    except subprocess.TimeoutExpired:
        code = 124
    except Interrupted as error:
        code = 128 + error.signum
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        receipt['commands'].append({'argv': argv, 'exit_code': locals().get('code', 130),
                                    'wall_seconds': time.monotonic() - started})
        receipt['conclusion'] = 'success' if locals().get('code') == 0 else 'failure'
        write(root / 'receipt.json', receipt)
    if code in (124, 130, 143):
        # Killing the docker-exec client alone does not stop its container-side
        # process. Stop the recorded instance before returning cancellation.
        cleanup(args)
    return code


def cleanup(args):
    root = args.root.resolve()
    if not (root / 'receipt.json').exists():
        return 0
    receipt = json.loads((root / 'receipt.json').read_text())
    if (root / 'instances/ci/instance.json').exists():
        result = subprocess.run(command(root / 'instances', 'cleanup'))
        receipt['cleanup_exit_code'] = result.returncode
        if result.returncode:
            receipt['conclusion'] = 'failure'
    receipt['wall_seconds'] = (time.time_ns() - receipt['started_ns']) / 1e9
    write(root / 'receipt.json', receipt)
    return receipt.get('cleanup_exit_code', 0)


def check_outputs(args):
    code = cleanup(args)
    require(code == 0, 'cannot validate outputs of an instance that failed to stop')
    root = args.root.resolve()
    receipt = json.loads((root / 'receipt.json').read_text())
    try:
        sources = Path(receipt['sources'])
        exported = getattr(args, 'outputs', None)
        require(exported is None or exported.strip(), 'declared outputs cannot be empty')
        for name in exported.splitlines() if exported is not None else ['.']:
            path = sources / name
            require(name and not Path(name).is_absolute() and path.resolve().is_relative_to(sources),
                    'export must stay within task sources')
            require(not path.is_symlink(), 'export root cannot be a link')
            check_tree(path, path.resolve())
        for name in CACHE_PATHS:
            path = root / 'instances/ci' / name
            boundary = root / 'instances/ci' / name.split('/')[0]
            require(path.resolve().is_relative_to(boundary), 'cache ancestor escapes private state')
            if path.exists() or path.is_symlink():
                check_tree(path, path.absolute())
    except (ValueError, OSError, RuntimeError):
        receipt['conclusion'] = 'failure'
        write(root / 'receipt.json', receipt)
        raise


def check_tree(path, boundary):
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        require(path.resolve().is_relative_to(boundary), 'output link escapes task sources or cache: ' + str(path))
    elif stat.S_ISDIR(mode):
        for child in path.iterdir():
            check_tree(child, boundary)
    else:
        require(stat.S_ISREG(mode), 'special file in task outputs or cache: ' + str(path))


def finish(args):
    """Retain diagnostics, then delete only this action's recorded private state."""
    code = cleanup(args)
    root = args.root.absolute()
    if not (root / 'receipt.json').exists():
        return code
    require(root.resolve() == root and root.stat().st_uid == os.getuid(), 'foreign or linked task root')
    receipt = json.loads((root / 'receipt.json').read_text())
    require(receipt['owner_uid'] == os.getuid(), 'foreign task receipt')
    require(not args.evidence.exists() and not args.evidence.resolve().is_relative_to(root),
            'evidence requires a fresh directory outside instance state')
    args.evidence.mkdir(parents=True)
    state = root / 'instances/ci'
    if code and not resources_released(state):
        # The candidate may still be writing. Only the host-owned receipt is
        # safe to export until the recorded instance has stopped successfully.
        write(args.evidence / 'receipt.json', receipt)
        return code
    diagnostic_status = code
    try:
        for name in ('instance.json', 'output'):
            path = state / name
            if path.is_file():
                require(not path.is_symlink(), 'linked instance evidence')
                shutil.copy2(path, args.evidence / name)
            elif path.is_dir():
                copy_evidence(path, args.evidence / name)
    except (ValueError, OSError) as error:
        diagnostic_status = diagnostic_status or 1
        receipt.update(conclusion='failure', diagnostics_error=str(error))
    write(args.evidence / 'receipt.json', receipt)
    if (state / 'instance.json').exists():
        result = subprocess.run(command(root / 'instances', 'cleanup', '--delete-output'))
        receipt['delete_output_exit_code'] = result.returncode
        if result.returncode:
            receipt['conclusion'] = 'failure'
            diagnostic_status = diagnostic_status or result.returncode
        write(args.evidence / 'receipt.json', receipt)
        if result.returncode and not resources_released(state):
            return diagnostic_status
    shutil.rmtree(root)
    return diagnostic_status


def resources_released(state):
    # A diagnostic failure can report failure after successful removal. Only
    # the host-owned launcher record and current Docker state may establish
    # that no candidate writer remains. A failed deletion resets this marker.
    try:
        data = launcher.record(state)
        return (data.get('status') == 'cleaned' and launcher.owned_container(data) is None
                and (not data.get('network_id') or launcher.inspect('network', data['network_id']) is None))
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        return False


def copy_evidence(source, destination):
    # Artifact upload follows symlinks. Never let candidate diagnostics cause a
    # host-side uploader to dereference a host path or special file.
    mode = source.lstat().st_mode
    require(stat.S_ISREG(mode) or stat.S_ISDIR(mode), 'unsafe diagnostic file: ' + str(source))
    if stat.S_ISREG(mode):
        shutil.copyfile(source, destination)
    else:
        destination.mkdir()
        for child in source.iterdir():
            copy_evidence(child, destination / child.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    choose = sub.add_parser('select')
    choose.add_argument('--framework-sha', required=True)
    choose.add_argument('--output', required=True, type=Path)
    choose.add_argument('--plan', type=Path)
    verify_image = sub.add_parser('verify-selection')
    verify_image.add_argument('--selection', required=True, type=Path)
    verify_image.add_argument('--framework-sha', required=True)
    begin = sub.add_parser('start')
    begin.add_argument('--selection', required=True, type=Path)
    begin.add_argument('--arch', required=True, choices=PLATFORMS)
    begin.add_argument('--sources', required=True, type=Path)
    begin.add_argument('--root', required=True, type=Path)
    begin.add_argument('--cpus', type=int, default=2)
    begin.add_argument('--memory-gib', type=int, default=8)
    begin.add_argument('--mode', choices=('build', 'system'), default='build')
    begin.add_argument('--network', choices=('none', 'bridge'), default='bridge')
    begin.add_argument('--output', type=Path)
    key = sub.add_parser('cache-key')
    key.add_argument('--sources', type=Path, default=Path('/src'))
    coverage = sub.add_parser('cache-coverage')
    coverage.add_argument('--name', required=True)
    coverage.add_argument('--sources', type=Path, default=Path('/src'))
    coverage.add_argument('--arch', required=True, choices=PLATFORMS)
    scope = sub.add_parser('cache-scope')
    scope.add_argument('--sources', required=True, type=Path)
    scope.add_argument('--receipt', required=True, type=Path)
    run = sub.add_parser('exec')
    run.add_argument('--root', required=True, type=Path)
    run.add_argument('--timeout', type=int, default=10800)
    run.add_argument('arguments', nargs=argparse.REMAINDER)
    end = sub.add_parser('cleanup')
    end.add_argument('--root', required=True, type=Path)
    final = sub.add_parser('finish')
    final.add_argument('--root', required=True, type=Path)
    final.add_argument('--evidence', required=True, type=Path)
    verify = sub.add_parser('check-outputs')
    verify.add_argument('--root', required=True, type=Path)
    verify.add_argument('--outputs', help='newline-separated private source paths consumed by the host')
    args = parser.parse_args()
    return {'select': select, 'verify-selection': verify_selection, 'start': start, 'cache-key': cache_key, 'cache-scope': cache_scope,
            'cache-coverage': cache_coverage,
            'exec': execute, 'cleanup': cleanup, 'finish': finish, 'check-outputs': check_outputs}[args.operation](args) or 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print('CI Workbench: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
