"""Source-time delivery conventions; never a runtime capability registry."""
from pathlib import Path
import shutil

def guide_inputs(root: Path):
    """Read the selected repository's declaration; never maintain owner lists here."""
    declaration = regular_input(root, Path('release/guide-inputs.txt'))
    inputs = []
    seen = set()
    for line in declaration.read_text(encoding='utf-8').splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith('#'):
            continue
        if (pattern.startswith('/') or '\\' in pattern or
                any(part in {'', '.', '..'} or part.startswith('.')
                    for part in pattern.rstrip('/').split('/'))):
            raise ValueError(f'unsafe documentation input pattern: {pattern}')
        matches = sorted(root.glob(pattern))
        if not matches:
            raise ValueError(f'missing documentation input: {pattern}')
        discovered = []
        for match in matches:
            relative = match.relative_to(root)
            if any((root / parent).is_symlink() for parent in (relative, *relative.parents)):
                raise ValueError(f'symbolic link in documentation input: {match}')
            if match.is_dir():
                entries = sorted(match.rglob('*'))
                for entry in entries:
                    if entry.is_symlink():
                        raise ValueError(f'symbolic link in documentation input: {entry}')
                    if entry.is_dir():
                        continue
                    path = entry.relative_to(root)
                    regular_input(root, path)
                    if entry.suffix == '.md':
                        discovered.append(path)
            else:
                regular_input(root, relative)
                if match.suffix != '.md':
                    raise ValueError(f'non-Markdown documentation input: {relative}')
                discovered.append(relative)
        if not discovered:
            raise ValueError(f'no Markdown documentation inputs: {pattern}')
        # Recursive directory globs can reach a file via multiple matching
        # ancestors. Only overlap between distinct declaration entries is an error.
        for path in sorted(set(discovered)):
            if path in seen:
                raise ValueError(f'duplicate documentation input: {path}')
            seen.add(path)
            inputs.append(path)
    if not inputs:
        raise ValueError(f'empty documentation declaration: {declaration}')
    return sorted(inputs)


def unit_guide_inputs(root: Path, unit: str):
    """Independent units use docs/<unit>.md and an optional translated peer.

    This directory/name convention also works at immutable older unit revisions
    without borrowing a declaration or document from a newer repository checkout.
    """
    if not unit or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in unit):
        raise ValueError(f'unsafe documentation unit: {unit}')
    primary = Path('docs') / (unit + '.md')
    regular_input(root, primary)
    yield primary
    translated = primary.with_name(unit + '_zh.md')
    if (root / translated).exists() or (root / translated).is_symlink():
        regular_input(root, translated)
        yield translated


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
