#!/usr/bin/env python3
"""Offline checks for first-party Markdown. No network or candidate execution.

This checks ordinary inline/reference links and GitHub-style heading fragments;
semantic translation completeness and external URLs still require review.
"""
from __future__ import annotations
import argparse
from collections import Counter
from html import unescape
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

HAN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]')
FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})')
HEADING = re.compile(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$')
INLINE = re.compile(r'!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s]+?)(?:\s+[\"\'][^\n]*?[\"\'])?\s*\)')
REFERENCE = re.compile(r'^\s{0,3}\[[^\]]+\]:\s*(<[^>]+>|\S+)')
EXPLICIT = re.compile(r'<(?:a|[a-z][\w-]*)\b[^>]*\b(?:id|name)=[\"\']([^\"\']+)[\"\']', re.I)
EXCLUDED = {'vendor', 'third_party', 'third-party', 'LICENSES', '.git', 'node_modules'}


def prose(text: str):
    """Yield numbered lines outside fenced code; keep line numbers for findings."""
    fence = None
    for n, line in enumerate(text.splitlines(), 1):
        m = FENCE.match(line)
        if m:
            mark = m.group(1)
            if fence is None:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence) and not line.strip()[len(mark):].strip():
                fence = None
            continue
        if fence is None:
            yield n, line


def slug(text: str) -> str:
    text = unescape(re.sub(r'<[^>]*>', '', text)).lower()
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'[^\w\-\s]', '', text, flags=re.UNICODE)
    return text.replace(' ', '-')


def anchors(text: str) -> set[str]:
    found, seen = set(), Counter()
    lines = list(prose(text))
    for i, (_, line) in enumerate(lines):
        found.update(EXPLICIT.findall(line))
        match = HEADING.match(line)
        heading = match.group(1) if match else None
        if heading is None and i + 1 < len(lines) and lines[i + 1][0] == lines[i][0] + 1 and re.fullmatch(r'\s{0,3}(?:=+|-+)\s*', lines[i + 1][1]) and line.strip():
            heading = line.strip()
        if heading:
            base = slug(heading)
            candidate = base
            suffix = seen[base]
            while candidate in found:
                suffix += 1
                candidate = f'{base}-{suffix}'
            found.add(candidate)
            seen[base] = suffix
    return found


def destinations(text: str):
    for n, line in prose(text):
        # Inline code may deliberately demonstrate a nonexistent link.
        line = re.sub(r'`+[^`]*`+', '', line)
        match = REFERENCE.match(line)
        if match:
            yield n, match.group(1).strip('<>')
        for match in INLINE.finditer(line):
            yield n, match.group(1).strip('<>')


def check_file(root: Path, path: Path) -> list[str]:
    rel = path.relative_to(root).as_posix()
    if any(part in EXCLUDED for part in path.relative_to(root).parts):
        return []
    issues = []
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        return [f'{rel}: unreadable UTF-8: {exc}']
    chinese = path.stem.endswith('_zh')
    peer = path.with_name(path.stem[:-3] + path.suffix) if chinese else path.with_name(path.stem + '_zh' + path.suffix)
    if chinese and not peer.is_file():
        issues.append(f'{rel}: missing English counterpart {peer.name}')
    if peer.is_file():
        for item in (path, peer):
            header = '\n'.join(item.read_text(encoding='utf-8').splitlines()[:5])
            other = peer if item == path else path
            targets = {urlsplit(dest).path for _, dest in destinations(header)}
            if other.name not in targets:
                issues.append(f'{item.relative_to(root)}: missing reciprocal language link to {other.name} in first five lines')
    if not chinese:
        for n, line in prose(text):
            if n <= 5 and '[English]' in line and '[简体中文]' in line:
                continue
            cleaned = re.sub(r'`+[^`]*`+', '', line)
            cleaned = re.sub(r'<[^>]*>', '', cleaned)
            # Exact-line exception is reviewable and cannot mask an entire file.
            if HAN.search(cleaned) and '<!-- docs:allow-han -->' not in line:
                issues.append(f'{rel}:{n}: Chinese prose in English default (translate or justify this line)')
    for n, destination in destinations(text):
        try:
            url = urlsplit(destination)
        except ValueError:
            issues.append(f'{rel}:{n}: invalid link target')
            continue
        if url.scheme or url.netloc:
            continue  # External access is a separately recorded acceptance check.
        target = (path.parent / unquote(url.path)).resolve() if url.path else path
        if not target.is_relative_to(root):
            issues.append(f'{rel}:{n}: local link escapes repository: {destination}; use an explicit cross-repository URL')
            continue
        if not target.exists():
            issues.append(f'{rel}:{n}: missing link target {destination}')
        elif url.fragment and target.is_file() and target.suffix.lower() == '.md':
            try:
                available = anchors(target.read_text(encoding='utf-8'))
            except (OSError, UnicodeError):
                issues.append(f'{rel}:{n}: unreadable target {destination}')
                continue
            fragment = unquote(url.fragment)
            if fragment not in available and fragment.removeprefix('user-content-') not in available:
                issues.append(f'{rel}:{n}: missing heading/anchor {destination}')
    return list(dict.fromkeys(issues))


def tracked(root: Path, base: str | None) -> list[Path]:
    args = ['git', '-C', str(root)]
    args += ['diff', '--name-only', '-z', '--diff-filter=ACMR', base, 'HEAD', '--'] if base else ['ls-files', '-z']
    result = subprocess.run(args, check=True, capture_output=True)
    paths = [root / p for p in result.stdout.decode('utf-8').split('\0') if p and p.lower().endswith('.md')]
    return [p for p in paths if p.is_file()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--changed-base', help='check documents changed between this trusted base revision and HEAD')
    parser.add_argument('--paths', nargs='*', help='explicit repository-relative Markdown paths')
    parser.add_argument('--json', action='store_true', help='machine-readable report')
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        paths = [root / p for p in args.paths] if args.paths is not None else tracked(root, args.changed_base)
        if any(not p.resolve().is_relative_to(root) for p in paths):
            raise ValueError('all input paths must stay within --root')
        findings = [issue for p in sorted(set(paths)) for issue in check_file(root, p)]
    except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'documentation check failed: {exc}', file=sys.stderr)
        return 2
    report = {'checked_files': len(set(paths)), 'findings': findings, 'external_links': 'not checked', 'semantic_translation_review': 'not automated'}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for finding in findings:
            print(finding)
        print(f'Checked {report["checked_files"]} Markdown files; {len(findings)} findings.')
        print('External access and semantic translation completeness require separate review.')
    return 1 if findings else 0


if __name__ == '__main__':
    raise SystemExit(main())
