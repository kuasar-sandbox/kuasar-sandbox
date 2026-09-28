#!/usr/bin/env python3
"""Qualify the staged native image with the same public offline prepare/run."""
import argparse
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import threading
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'ci/integration'), str(ROOT / 'release')]
import artifacts
import selection
import workbench_assets


def module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


launcher = module('release_launcher', ROOT / 'workbench/workbench')
lifecycle = module('release_lifecycle', ROOT / 'workbench/test-system.py')
apparmor = module('release_apparmor', ROOT / 'workbench/test-apparmor.py')


def qualify(plan, stage, root, cpus, memory_gib):
    artifacts.check_plan(plan)
    artifacts.require(plan['mode'] == 'exact-assets' and plan['baseline'].get('delivery') == selection.DELIVERY,
                      'workbench qualification requires the exact new-contract stage')
    artifacts.require(subprocess.check_output(['git', '-C', ROOT, 'rev-parse', 'HEAD'], text=True).strip()
                      == plan['framework_sha'], 'workbench executor differs from trusted framework')
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'CALLER_TOKEN', 'KUASAR_CI_APP_PRIVATE_KEY'):
        artifacts.require(not os.environ.get(key), f'workbench qualification must not receive {key}')
    arch = platform.machine()
    version, revision = plan['baseline']['version'], plan['baseline']['sha']
    root.mkdir(parents=True, exist_ok=False)
    receipt = workbench_assets.validate(stage / 'assets', version, arch, revision, receipt_directory=stage / 'workbench')
    image = receipt['image_id']
    started = time.monotonic()
    result = {key: receipt[key] for key in ('archive', 'image_id', 'sha256', 'arch', 'aggregate_version', 'source_revision', 'size', 'compression_seconds')}
    result.update(conclusion='failure', plan_id=artifacts.identity(plan), framework_sha=plan['framework_sha'],
                  test_revisions=plan['test_revisions'], host_apparmor_enabled=launcher.apparmor_enabled(),
                  input_assets={item['name']: item['digest'] for item in plan['baseline']['assets'] if item['name'] != 'SHA256SUMS'})
    command = [sys.executable, '-B', ROOT / 'workbench/workbench', '--root', root / 'instances', '--name', 'offline']
    state = root / 'instances/offline'
    disk = {'scope': 'observed host filesystem usage', 'sample_seconds': 1,
            'used_before': shutil.disk_usage(root).used, 'peak_used': 0}
    stopped = threading.Event()
    def sample_disk():
        while True:
            disk['peak_used'] = max(disk['peak_used'], shutil.disk_usage(root).used)
            if stopped.wait(1):
                return
    monitor = threading.Thread(target=sample_disk, daemon=True)
    monitor.start()
    try:
        imported = time.monotonic()
        launcher.docker('load', '-i', stage / 'assets' / receipt['archive'], timeout=900)
        result['import_seconds'] = time.monotonic() - imported
        info = launcher.inspect('image', image)
        artifacts.require(info and info['Id'] == image, 'imported image differs from staged config')
        release = root / 'release'
        release.mkdir()
        seen = {}
        records = {item['name']: item for item in plan['baseline']['assets']}
        inputs = {artifacts.archive_name(unit, item['version'], arch): unit for unit, item in plan['baseline']['units'].items()}
        inputs['platform-' + version + '.tar.gz'] = 'platform'
        for name, owner in inputs.items():
            path = stage / 'assets' / name
            artifacts.require(path.stat().st_size == records[name]['size'] and 'sha256:' + artifacts.digest(path) == records[name]['digest'],
                              'release input bytes differ from exact stage: ' + name)
            artifacts.unpack(path, release, owner, seen)
        cases = artifacts.workbench_cases(plan['case_files'], arch)
        result['cases'] = cases
        start = time.monotonic()
        subprocess.run([*command, 'start', '--image', image, '--inputs', release, '--network', 'none',
                        '--cpus', str(cpus), '--memory-gib', str(memory_gib), '--timeout', '90'], check=True)
        result['start_seconds'] = time.monotonic() - start
        instance = json.loads((state / 'instance.json').read_text())
        result['preflight'] = instance['preflight']
        def execute(*args, timeout=900):
            return subprocess.check_output([*map(str, command), 'exec', '--', *map(str, args)], text=True, timeout=timeout)
        artifacts.require(execute('docker', 'image', 'ls', '-q').strip() == ''
                          and execute('docker', 'ps', '-aq').strip() == '', 'private Docker state was not empty')
        artifacts.require(not execute('ip', '-4', 'route', 'show', 'default').strip(), 'offline qualification has an external route')
        result['empty_private_daemon'] = True
        # The selected official input image archives are already inside the
        # staged image. No host daemon/socket or registry transport is available.
        arguments = [item for case in cases for item in ('--include', case)]
        execute('python3', '-B', '/inputs/release/test/e2e/e2e', 'prepare', '--release-dir', '/inputs/release',
                '--workdir', '/work/prepared', '--arch', arch, '--deps-dir', '/opt/workbench/deps', '--offline', *arguments)
        result['offline'] = True
        execute('python3', '-B', '/work/prepared/test/e2e/e2e', 'run', '--workdir', '/work/prepared', '--arch', arch,
                '--run-root', '/work/cases', '--out-root', '/output/cases', '--result', '/output/public-result.json',
                *arguments, timeout=10800)
        report = json.loads((state / 'output/public-result.json').read_text())
        artifacts.require(report['conclusion'] == 'success' and report['arch'] == arch and report['cases'] == cases,
                          'public workbench execution differs from declared selection')
        artifacts.check_timings(report['timings'], cases)
        result.update(timings=report['timings'], provenance_sha256=report['provenance_sha256'])
        subprocess.run([*command, 'stop'], check=True)
        result['isolation'] = lifecycle.exercise(argparse.Namespace(image=image, root=root / 'isolation', protect_container=[]))
        if result['host_apparmor_enabled']:
            result['apparmor'] = apparmor.qualify(image, root / 'apparmor', inner_archive=state / 'work/prepared/images/busybox.tar')
        result['conclusion'] = 'success'
    except BaseException as error:
        result['error'] = str(error)
        raise
    finally:
        if (state / 'instance.json').exists():
            try:
                subprocess.run([*command, 'cleanup'], check=True, timeout=420)
            except Exception as error:
                result.update(conclusion='failure', cleanup_error=str(error))
        stopped.set()
        monitor.join(timeout=5)
        artifacts.require(not monitor.is_alive(), 'disk sampler did not stop')
        disk['peak_increase'] = max(0, disk['peak_used'] - disk['used_before'])
        result['disk'] = disk
        result['wall_seconds'] = time.monotonic() - started
        # Report actual allocated task storage, not an implied filesystem quota.
        result['retained_task_bytes'] = int(launcher.host_command(['du', '-s', '-B1', str(root)]).split()[0])
        (root / 'result.json').write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    artifacts.require(result['conclusion'] == 'success', 'workbench cleanup failed')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--cpus', type=int, default=2)
    parser.add_argument('--memory-gib', type=int, default=8)
    args = parser.parse_args()
    qualify(json.loads(args.plan.read_text()), args.stage.resolve(), args.root.resolve(), args.cpus, args.memory_gib)
