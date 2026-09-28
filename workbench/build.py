#!/usr/bin/env python3
"""Build and save one native workbench for the selected aggregate version."""
import argparse
import gzip
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'test/e2e/lib'))
sys.path.insert(0, str(ROOT / 'release'))
import workspace
import selection
from workbench_assets import check_size


def build(version, arch, cases, deps, output, cpus=2, memory_gib=6):
    workspace.require(selection.AGGREGATE_RE.fullmatch(version), 'workbench requires the selected aggregate version')
    workspace.require(arch == platform.machine(), 'workbench images must be built on their native architecture')
    workspace.require(1 <= cpus <= len(os.sched_getaffinity(0)) and memory_gib >= 2, 'invalid image build resource budget')
    revision = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    subprocess.run(['git', '-C', str(ROOT), 'diff', '--exit-code', 'HEAD', '--', 'workbench', 'test/e2e/lib'], check=True)
    workspace.require(not subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '--others', '--exclude-standard',
                                                   '--', 'workbench', 'test/e2e/lib'], text=True), 'commit image build inputs before staging')
    spec = importlib.util.spec_from_file_location('workbench_inputs', ROOT / 'workbench/collect-inputs.py')
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    names = [case.name for case in collector.runner.discover(cases)]
    requests = workspace.external_image_requests(names, arch)
    local = workspace.local_image_inputs(requests, deps, True)
    output.mkdir(parents=True, exist_ok=True)
    suffix = version.removeprefix('release-')
    archive = output / f'workbench-{arch}-{suffix}.tar.gz'
    receipt = output / f'workbench-{arch}.json'
    workspace.require(not archive.exists() and not receipt.exists(), 'workbench stage already exists; verify and reuse its tested bytes')
    workspace.require(shutil.disk_usage(output).free >= 10 * 1024**3, 'image staging needs at least 10 GiB free; no disk quota is implied')
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='workbench-build-', dir=output) as directory:
        context = Path(directory)
        for name in ('Dockerfile', 'toolchains.json', 'install-toolchains.py', 'system-entrypoint.sh',
                     'check-system.py', 'daemon.json', 'containerd.toml'):
            shutil.copy2(ROOT / 'workbench' / name, context / name)
        shutil.copy2(ROOT / 'LICENSE', context / 'LICENSE')
        dependency_root = context / 'deps'
        dependency_root.mkdir()
        records = []
        for label, (source, record) in local.items():
            destination = dependency_root / (label + '.tar')
            subprocess.run(['cp', '--reflink=auto', '--', str(source), str(destination)], check=True)
            workspace.require(workspace.digest(destination) == record['sha256'], 'dependency changed while staging')
            destination.chmod(0o444)
            records.append({**record, 'archive': destination.name})
        (dependency_root / 'images.json').write_text(json.dumps(records, sort_keys=True) + '\n')
        (dependency_root / 'images.json').chmod(0o444)
        context_files = workspace.files(context)
        tag = f'kuasar-workbench-stage:{suffix}-{arch}-{revision[:12]}'
        subprocess.run(['docker', 'build', '--pull=false', '--memory', str(memory_gib) + 'g',
                        '--cpu-period=100000', '--cpu-quota=' + str(cpus * 100000),
                        '--build-arg', 'AGGREGATE_VERSION=' + version, '--build-arg', 'SOURCE_REVISION=' + revision,
                        '--tag', tag, str(context)], check=True, env={**os.environ, 'DOCKER_BUILDKIT': '0'})
        image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', tag]))[0]
        workspace.require(image['Architecture'] == {'x86_64': 'amd64', 'aarch64': 'arm64'}[arch], 'built image has the wrong architecture')
        canonical = 'ghcr.io/kuasar-sandbox/workbench:' + suffix
        previous = subprocess.run(['docker', 'image', 'inspect', canonical], capture_output=True, text=True)
        if previous.returncode == 0:
            workspace.require(json.loads(previous.stdout)[0]['Id'] == image['Id'], 'same-version local image differs; refusing replacement')
        subprocess.run(['docker', 'image', 'tag', image['Id'], canonical], check=True)
        temporary = archive.with_suffix('.part')
        compression_started = time.monotonic()
        with temporary.open('xb') as stream, gzip.GzipFile(fileobj=stream, mode='wb', mtime=0, compresslevel=1) as compressed:
            with subprocess.Popen(['docker', 'image', 'save', canonical], stdout=subprocess.PIPE) as process:
                shutil.copyfileobj(process.stdout, compressed)
                workspace.require(process.wait() == 0, 'Docker image export failed')
        compression_seconds = time.monotonic() - compression_started
        check_size(temporary)
        verified = workspace.verify_image_archive(temporary, 'linux/' + image['Architecture'])
        workspace.require(verified['image_id'] == image['Id'], 'exported image differs from the built image')
        temporary.rename(archive)
        record = {'aggregate_version': version, 'arch': arch, 'source_revision': revision,
                  'image_id': image['Id'], 'archive': archive.name, 'sha256': workspace.digest(archive),
                  'size': archive.stat().st_size, 'image_size': image['Size'], 'context_files': context_files,
                  'external_images': records, 'compression_seconds': compression_seconds, 'wall_seconds': time.monotonic() - started}
        receipt.write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True, help='aggregate release-v* version, with the same Preview suffix')
    parser.add_argument('--arch', choices=('x86_64', 'aarch64'), default=platform.machine())
    parser.add_argument('--cases', type=Path, required=True, help='assembled canonical E2E cases')
    parser.add_argument('--deps-dir', type=Path, required=True, help='verified collection for the staged release')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cpus', type=int, default=2)
    parser.add_argument('--memory-gib', type=int, default=6)
    args = parser.parse_args()
    print(json.dumps(build(args.version, args.arch, args.cases.resolve(), args.deps_dir.resolve(),
                           args.output.resolve(), args.cpus, args.memory_gib), sort_keys=True))
