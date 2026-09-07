#!/usr/bin/env python3
"""Assemble Markdown and rebase navigation without editing executable content."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import quote, unquote, urlsplit, urlunsplit

OWNERS = ('platform', 'accelerator', 'connector', 'guest-runtime', 'sandboxer', 'orchestrator')
REPOS = {owner: ('kuasar-sandbox' if owner == 'platform' else owner) for owner in OWNERS}
REPOS['vmlinux'] = 'guest-runtime'
INLINE = re.compile(r'(!?\[[^\]\n]*\]\(\s*)(<[^>\n]+>|[^\s]+?)(\s+(?:"[^"\n]*"|\x27[^\x27\n]*\x27))?(\s*\))')
REFERENCE = re.compile(r'^(\s{0,3}\[[^\]]+\]:\s*)(<[^>]+>|\S+)')
HTML_LINK = re.compile(r'\b(href|src)=("|\x27)(.*?)\2')
CODE = re.compile(r'(`+).*?\1')
FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})')


def source_files(root: Path):
    for directory, dirs, files in os.walk(root):
        relative = Path(directory).relative_to(root)
        dirs[:] = sorted(d for d in dirs if d not in {
            '.git', 'node_modules', 'vendor', 'third_party', 'third-party', '__pycache__'
        } and relative / d not in {Path('build'), Path('bin'), Path('native-deps/build'), Path('native-deps/bin')})
        for d in dirs:
            if (Path(directory) / d).is_symlink():
                raise ValueError(f'symbolic link in documentation source: {relative / d}')
        for name in sorted(files):
            path = relative / name
            if path.suffix.lower() == '.md' or path.parts[0] in {'docs', 'LICENSES'} or path.name in {'LICENSE', 'NOTICE'}:
                if (root / path).is_symlink():
                    raise ValueError(f'symbolic link in documentation source: {path}')
                yield path


def destination(owner: str, path: Path) -> Path:
    if path.parts[0] == 'docs':
        return path
    if owner == 'platform' and path.parts[0] == 'test':
        return path
    if path.parts[:2] == ('test', 'e2e'):
        return Path('test/e2e') / owner / Path(*path.parts[2:])
    if owner != 'platform' and path.as_posix() in {'README.md', 'README_zh.md'}:
        return Path('docs') / (owner + ('_zh' if path.stem.endswith('_zh') else '') + '.md')
    return Path('docs') / ('project' if owner == 'platform' else owner) / path


def source_ref(root: Path) -> str:
    try:
        # Avoid picking up a parent workspace's unrelated Git repository.
        if not (root / '.git').exists():
            return 'main'
        return subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        return 'main'


def assemble(output: Path, roots: dict[str, Path], refs: dict[str, str], kernel: Path | None = None):
    mapping: dict[tuple[str, str], Path] = {}
    inputs: dict[Path, tuple[str, Path]] = {}
    if kernel is not None:
        roots = {**roots, 'vmlinux': kernel}
    refs = {owner: refs.get(owner, source_ref(root)) for owner, root in roots.items()}
    for owner, root in roots.items():
        paths = [Path('docs/vmlinux.md'), Path('docs/vmlinux_zh.md')] if owner == 'vmlinux' else source_files(root)
        for path in paths:
            if owner == 'guest-runtime' and kernel is not None and path.as_posix() in {'docs/vmlinux.md', 'docs/vmlinux_zh.md'}:
                continue
            if not (root / path).is_file():
                if owner == 'vmlinux' and path.name.endswith('_zh.md'):
                    continue  # Older selected kernel tags can be English-only.
                raise ValueError(f'missing documentation input: {owner}/{path}')
            dest = destination(owner, path)
            if dest in inputs:
                raise ValueError(f'documentation collision at {dest}: {inputs[dest][0]} and {owner}')
            inputs[dest] = (owner, path)
            mapping[owner, path.as_posix()] = dest
            if owner == 'vmlinux':
                mapping['guest-runtime', path.as_posix()] = dest

    def rewrite_url(raw: str, owner: str, path: Path, dest: Path) -> str:
        angle = raw.startswith('<') and raw.endswith('>')
        value = raw[1:-1] if angle else raw
        url = urlsplit(value)
        target_owner, target_path = owner, None
        if url.scheme or url.netloc:
            if url.scheme != 'https' or url.netloc != 'github.com':
                return raw
            for candidate, repo in REPOS.items():
                if candidate not in roots:
                    continue
                for kind in ('blob', 'tree'):
                    for ref in {'main', refs[candidate]}:
                        prefix = f'/kuasar-sandbox/{repo}/{kind}/{ref}/'
                        if url.path.startswith(prefix):
                            target_owner = candidate
                            target_path = Path(unquote(url.path[len(prefix):]))
                            break
                    if target_path is not None:
                        break
                if target_path is not None:
                    break
            if target_path is None:
                return raw  # Preserve historical revisions and other external URLs.
        else:
            if not url.path:
                return raw
            resolved = (roots[owner] / path.parent / unquote(url.path)).resolve()
            if not resolved.is_relative_to(roots[owner]):
                raise ValueError(f'link escapes source repository: {owner}/{path}: {value}')
            target_path = resolved.relative_to(roots[owner])
        key = target_owner, target_path.as_posix()
        target = mapping.get(key)
        original = roots[target_owner] / target_path
        if target is None and original.is_dir():
            target = mapping.get((target_owner, (target_path / 'README.md').as_posix()))
            if target is None and target_path == Path('docs'):
                target = mapping.get((target_owner, 'README.md'))
        if target is not None:
            result = urlunsplit(('', '', quote(os.path.relpath(target, dest.parent), safe='/._-'), url.query, url.fragment))
        elif url.scheme:
            return raw  # A selected source need not contain a file from a newer main URL.
        else:
            if not original.exists():
                raise ValueError(f'missing source link: {owner}/{path}: {value}')
            kind = 'tree' if original.is_dir() else 'blob'
            result = urlunsplit(('https', 'github.com', f'/kuasar-sandbox/{REPOS[target_owner]}/{kind}/{refs[target_owner]}/' + quote(target_path.as_posix(), safe='/._-'), url.query, url.fragment))
        return f'<{result}>' if angle else result

    def rewrite(text: str, owner: str, path: Path, dest: Path) -> str:
        fence = None
        result = []
        for line in text.splitlines(keepends=True):
            mark = FENCE.match(line)
            if mark:
                token = mark.group(1)
                if fence is None:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence) and not line.strip()[len(token):].strip():
                    fence = None
                result.append(line)
                continue
            if fence is not None:
                result.append(line)
                continue
            spans = [m.span() for m in CODE.finditer(line)]
            replacements = []
            matches = list(INLINE.finditer(line))
            ref = REFERENCE.match(line)
            if ref:
                matches.append(ref)
            for match in matches:
                start, end = match.span(2)
                if not any(a <= start < b for a, b in spans):
                    replacements.append((start, end, rewrite_url(match.group(2), owner, path, dest)))
            for match in HTML_LINK.finditer(line):
                start, end = match.span(3)
                if not any(a <= start < b for a, b in spans):
                    replacements.append((start, end, rewrite_url(match.group(3), owner, path, dest)))
            for start, end, value in sorted(set(replacements), reverse=True):
                line = line[:start] + value + line[end:]
            result.append(line)
        return ''.join(result)

    for dest, (owner, path) in sorted(inputs.items()):
        source, target = roots[owner] / path, output / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() == '.md':
            target.write_text(rewrite(source.read_text(encoding='utf-8'), owner, path, dest), encoding='utf-8')
            shutil.copymode(source, target)
        else:
            shutil.copy2(source, target)
    print(f'Assembled {len(inputs)} documentation files; executable content unchanged.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('sources', nargs=6, type=Path, metavar='SOURCE')
    args = parser.parse_args()
    refs = {}
    if os.environ.get('DOCS_SOURCE_REFS'):
        for line in Path(os.environ['DOCS_SOURCE_REFS']).read_text().splitlines():
            owner, ref = line.split('\t')
            refs['guest-runtime' if owner == 'runtime' else owner] = ref
    kernel = Path(os.environ['DOCS_VMLINUX_SOURCE']).resolve() if os.environ.get('DOCS_VMLINUX_SOURCE') else None
    assemble(args.output.resolve(), dict(zip(OWNERS, (p.resolve() for p in args.sources))), refs, kernel)


if __name__ == '__main__':
    main()
