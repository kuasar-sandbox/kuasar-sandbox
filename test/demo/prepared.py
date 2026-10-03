#!/usr/bin/env python3
"""Use the existing Demo with inputs produced by the public E2E prepare command."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'e2e/lib'))
import workspace

CASE = 'basic.demo.sh'


def separate_path(value: Path, root: Path) -> Path:
    path = value.absolute()
    workspace.require(path == path.resolve(), 'mutable path must be canonical without symlink parents')
    workspace.require(not path.is_relative_to(root) and not root.is_relative_to(path),
                      'mutable paths must be separate from prepared inputs')
    return path


def demo_inputs(value: Path):
    root = value.absolute()
    workspace.require(root == root.resolve(), 'prepared path must be canonical without symlink parents')
    provenance = workspace.verify(root)
    arch = provenance.get('arch')
    workspace.require(arch in ('x86_64', 'aarch64') and arch == platform.machine(),
                      'Demo requires the prepared native architecture')
    workspace.require(CASE in provenance.get('prepared_cases', []), 'prepare basic.demo.sh before running the Demo')
    sdk = provenance.get('python', {}).get('demo', {})
    workspace.require(sdk.get('directory') == 'fixtures/demo-sdk' and (root / 'fixtures/demo-sdk/e2b').is_dir(),
                      'missing prepared Demo SDK; use the public prepare command')
    workspace.require(sdk.get('python') == f'{sys.version_info.major}.{sys.version_info.minor}',
                      'Demo requires the Python version of its prepared wheels (Python 3.12)')
    for name in workspace.required_helpers([CASE]):
        record = provenance.get('helpers', {}).get(name)
        path = root / 'fixtures/bin' / name
        workspace.require(record and path.is_file() and workspace.digest(path) == record['sha256'],
                          f'missing or changed prepared Demo helper: {name}')
        workspace.check_helper(path, arch)
    image = provenance.get('images', {}).get('python', {})
    expected = 'linux/' + {'x86_64': 'amd64', 'aarch64': 'arm64'}[arch]
    workspace.require(image.get('platform') == expected and
                      re.fullmatch(r'sha256:[0-9a-f]{64}', image.get('image_id', '')),
                      'missing or wrong-platform prepared Demo image')
    return root, provenance


def python_launcher(root: Path, output: Path) -> Path:
    """Shared by the user adapter and the unchanged full product case."""
    output = separate_path(output, root)
    sdk = root / 'fixtures/demo-sdk'
    workspace.require((sdk / 'e2b').is_dir(), 'missing prepared Demo SDK')
    # -P omits the mutable working directory; -S omits global/site packages.
    # Only the prepared SDK and this interpreter's standard library are visible.
    text = ('#!/bin/sh\nunset PYTHONHOME PYTHONSTARTUP\n'
            f'export PYTHONPATH={shlex.quote(str(sdk))}\n'
            f'exec {shlex.quote(sys.executable)} -P -S -B "$@"\n')
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(text)
    subprocess.run([str(output), '-', str(root / 'test/demo/requirements.txt'), str(sdk)],
                   input=('from importlib.metadata import version\nfrom pathlib import Path\n'
                          'import sys, e2b\n'
                          'assert Path(sys.argv[1]).read_text().strip() == "e2b==" + version("e2b")\n'
                          'assert Path(e2b.__file__).resolve().is_relative_to(Path(sys.argv[2]))\n'),
                   text=True, check=True)
    return output


def demo_environment(root: Path, provenance: dict, data: Path) -> dict:
    # Do not inherit DEMO_QUICKSTART/NETDIAG, alternate products, proxy settings,
    # Python paths or a host Docker endpoint from the invoking environment.
    environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'TERM', 'TZ') if key in os.environ}
    environment.update(workspace.case_environment(root, provenance, CASE))
    environment.update(DEMO_DATA_DIR=str(data), PYTHONNOUSERSITE='1', DEMO_KEEP='1')
    return environment


def execute(args) -> int:
    root, provenance = demo_inputs(args.workdir)
    if args.command == 'python':
        python_launcher(root, args.output)
        return 0
    workspace.require(os.geteuid() == 0, 'run the prepared Demo inside workbench system mode, or as native root')
    data = separate_path(args.data_dir, root)
    workspace.require(re.fullmatch(r'/[A-Za-z0-9._/-]+', str(data)), 'Demo data path contains unsupported characters')
    workspace.require(data.name.startswith('kuasar-demo-'), 'use a task-owned kuasar-demo-<name> data directory')
    environment = demo_environment(root, provenance, data)
    script = root / 'test/demo/demo_prep.sh'
    if args.command in ('stop', 'reset'):
        # Let the existing ownership-aware lifecycle implementation decide what
        # can be stopped/deleted; never substitute a generic directory removal.
        return subprocess.call(['bash', str(script), args.command], env=environment)
    if args.quick:
        environment['DEMO_QUICKSTART'] = '1'
    if args.pause:
        workspace.require(sys.stdin.isatty(), '--pause requires an interactive terminal (workbench exec)')
        environment['DEMO_PAUSE'] = '1'
    data.parent.mkdir(parents=True, exist_ok=True)
    try:
        workspace.load_images(root, {'images': {'python': provenance['images']['python']}}, environment)
        with tempfile.TemporaryDirectory(prefix='kuasar-demo-python-', dir=data.parent) as directory:
            environment['PYTHON_BIN'] = str(python_launcher(root, Path(directory) / 'python'))
            status = subprocess.call(['bash', str(script)], env=environment)
            if status:
                return status
            return subprocess.call(['bash', str(root / 'test/demo/demo_e2b.sh')], env=environment)
    finally:
        workspace.verify(root)
        print(f'Demo state and retained logs: {data}; persistent preparation remains until stop/reset.', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for action in ('run', 'stop', 'reset', 'python'):
        item = commands.add_parser(action)
        item.add_argument('--workdir', required=True, type=Path, help='immutable public prepared workspace')
        if action == 'python':
            item.add_argument('--output', required=True, type=Path, help='new private SDK launcher outside inputs')
        else:
            item.add_argument('--data-dir', required=True, type=Path, help='separate task-owned kuasar-demo-<name> directory')
        if action == 'run':
            item.add_argument('--quick', action='store_true', help='short lifecycle, not full E2E acceptance')
            item.add_argument('--pause', action='store_true', help='pause between stages for interactive observation')
    return execute(parser.parse_args(argv))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(f'prepared Demo: {error}', file=sys.stderr)
        raise SystemExit(1)
