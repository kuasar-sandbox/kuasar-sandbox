"""Source-time delivery conventions; never a runtime capability registry."""
from pathlib import Path
import shutil

# Whole maintained user documents. Chinese counterparts are copied when present;
# the source documentation checker owns the translation policy.
GUIDES = {
    'platform': ('README', 'docs/quickstart', 'docs/download', 'docs/deployment',
                 'docs/kuasar-sandbox', 'docs/terminology', 'test/README',
                 'test/QUICKSTART', 'test/demo/DEMO', 'workbench/README'),
    'accelerator': ('README', 'docs/cache', 'docs/cache-redis', 'docs/store',
                    'docs/manifest', 'docs/file-artifacts', 'test/e2e/README'),
    'connector': ('README', 'docs/tapfd', 'docs/vswitch-operations'),
    'guest-runtime': ('README', 'docs/flatten', 'docs/sandbox-runtime',
                      'docs/vmlinux', 'test/e2e/README'),
    'sandboxer': ('README', 'docs/sandbox', 'docs/sandbox-init'),
    'orchestrator': ('README', 'docs/node', 'docs/node-build', 'docs/node-proxy',
                     'docs/node-resource', 'docs/node-journald', 'docs/telemetry'),
    'vmlinux': ('docs/vmlinux',),
}

PLATFORM_RUNTIME = (
    'test/e2e/e2e', 'test/e2e/lib/common.sh', 'test/e2e/lib/workspace.py',
    'test/e2e/lib/demo_wheels.py', 'test/demo/demo_common.sh',
    'test/demo/demo_e2b.sh', 'test/demo/demo_prep.sh', 'test/demo/prepared.py',
    'test/demo/requirements.txt', 'test/demo/requirements.lock',
    'workbench/workbench',
)

def library_inputs(root: Path):
    """Discover an owner's complete runtime tree, preserving nested paths.

    lib/ is the delivery boundary. test_* entries and Python cache directories
    are source-only by convention; all other regular files are runtime inputs.
    Validate even excluded entries so exclusions cannot conceal unsafe paths.
    """
    library = root / 'lib'
    if library.is_symlink() or not library.is_dir():
        raise ValueError(f'missing or symbolic-link runtime library directory: {library}')
    inputs = []
    for source in sorted(library.rglob('*')):
        path = source.relative_to(root)
        if source.is_symlink():
            raise ValueError(f'symbolic link in package input: {source}')
        if source.is_dir():
            continue
        regular_input(root, path)
        if any(part.startswith('test_') or part in {'__pycache__', '.pytest_cache'}
               for part in path.parts[1:]) or source.suffix in {'.pyc', '.pyo'}:
            continue
        inputs.append(path)
    if not inputs:
        raise ValueError(f'no runtime library inputs: {library}')
    return inputs


def regular_input(root: Path, path: Path):
    if path.is_absolute() or '..' in path.parts:
        raise ValueError(f'unsafe package input: {path}')
    source = root / path
    if any((root / parent).is_symlink() for parent in (path, *path.parents)):
        raise ValueError(f'symbolic link in package input: {source}')
    if not source.is_file():
        raise ValueError(f'missing package input: {source}')
    return source


def copy_input(root: Path, path: Path, target: Path):
    source = regular_input(root, path)
    if target.exists() or target.is_symlink():
        raise ValueError(f'package input collision: {target}')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
