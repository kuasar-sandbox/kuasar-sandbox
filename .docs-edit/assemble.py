#!/usr/bin/env python3
"""Assemble reviewed Markdown changes and optionally create Git objects only.

No branch reference, PR, repository setting or release is changed here. The
caller must separately publish the returned commit through the ordinary PR flow.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
import urllib.error
import urllib.request
import difflib

FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})')
HEADING = re.compile(r'^(\s{0,3})(#{1,6})\s+(.+?)\s*#*\s*$')
HAN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]')
EXPLICIT = re.compile(r'<a\b[^>]*\b(?:id|name)=["\']([^"\']+)["\']', re.I)


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def doc_path(value: str) -> str:
    p = PurePosixPath(value)
    if not value or p.is_absolute() or '..' in p.parts or str(p) != value or not value.endswith('.md') or value.startswith(('.git/', '.docs-edit/')):
        raise ValueError(f'not an allowed Markdown output path: {value!r}')
    return value


def prose(text: str):
    fence = None
    for n, line in enumerate(text.splitlines(keepends=True)):
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
    if fence is not None:
        raise ValueError('unclosed fenced code block')


def slug(text: str) -> str:
    text = html.unescape(re.sub(r'<[^>]*>', '', text)).lower()
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    return re.sub(r'[^\w\-\s]', '', text).replace(' ', '-')


def headings(text: str):
    used = set()
    result = []
    for index, line in prose(text):
        m = HEADING.match(line)
        if m:
            base = slug(m.group(3))
            anchor = base
            count = 0
            while anchor in used:
                count += 1
                anchor = f'{base}-{count}'
            used.add(anchor)
            result.append((index, len(m.group(2)), anchor))
    return result


def retain_anchors(original: str, translated: str) -> str:
    old, new = headings(original), headings(translated)
    if [level for _, level, _ in old] != [level for _, level, _ in new]:
        raise ValueError('source and translation heading hierarchy differ; review explicitly before assembling')
    lines = translated.splitlines(keepends=True)
    explicit = set(EXPLICIT.findall(translated))
    existing = {anchor for _, _, anchor in new} | explicit
    inserts = {}
    for (_, _, anchor), (index, _, _) in zip(old, new):
        if anchor not in existing:
            inserts[index] = f'<a id="{html.escape(anchor, quote=True)}"></a>\n'
            existing.add(anchor)
    if not set(EXPLICIT.findall(original)).issubset(existing):
        raise ValueError('translation dropped an explicit source anchor')
    return ''.join(inserts.get(i, '') + line for i, line in enumerate(lines))


def replace_exact(text: str, replacements: list[dict]) -> str:
    for item in replacements:
        old, new, count = item['old'], item['new'], item['count']
        if not isinstance(count, int) or count < 1 or not old or text.count(old) != count:
            raise ValueError(f'exact replacement count mismatch for {old!r}')
        text = text.replace(old, new)
    return text


def read_input(root: Path, name: str) -> str:
    raw = root / name
    p = raw.resolve()
    if not name.startswith('.docs-edit/') or not p.is_relative_to(root.resolve()) or not p.is_file() or raw.is_symlink():
        raise ValueError(f'invalid draft input: {name!r}')
    return p.read_text(encoding='utf-8')


def assemble(plan: dict, input_root: Path, source_root: Path | None = None):
    parent = plan['parent_sha']
    if not re.fullmatch(r'[0-9a-f]{40}', parent):
        raise ValueError('parent_sha must be an exact SHA')
    originals, changes = {}, {}
    def get(path, expected):
        path = doc_path(path)
        if source_root is not None:
            p = source_root / path
            data = p.read_bytes() if p.exists() else None
        else:
            proc = subprocess.run(['git', 'show', f'{parent}:{path}'], capture_output=True)
            data = proc.stdout if proc.returncode == 0 else None
        if expected == 'absent':
            if data is not None:
                raise ValueError(f'{path}: expected absent file')
            originals[path] = None
            return ''
        if data is None or blob_sha(data) != expected:
            raise ValueError(f'{path}: original Git blob does not match pinned input')
        originals[path] = data
        return data.decode('utf-8')
    def put(path, text):
        path = doc_path(path)
        if path in changes or '\0' in text:
            raise ValueError(f'duplicate output or NUL in {path}')
        changes[path] = text.encode('utf-8')
    for item in plan.get('patches', []):
        before = get(item['path'], item['source_blob'])
        put(item['path'], replace_exact(before, item['replacements']))
    for item in plan.get('files', []):
        get(item['path'], item['source_blob'])
        put(item['path'], read_input(input_root, item['input']))
    for item in plan.get('translations', []):
        path = doc_path(item['path'])
        p = PurePosixPath(path)
        peer = str(p.with_name(p.stem + '_zh.md'))
        original = get(path, item['source_blob'])
        get(peer, 'absent')
        chinese = replace_exact(original, item.get('chinese_replacements', []))
        english = read_input(input_root, item['input'])
        if english.startswith('[English]') or chinese.startswith('[English]'):
            raise ValueError('translation inputs must not already contain a language selector')
        for number, line in prose(english):
            cleaned = re.sub(r'`+[^`]*`+', '', re.sub(r'<[^>]*>', '', line))
            if HAN.search(cleaned) and '<!-- docs:allow-han -->' not in line:
                raise ValueError(f'{path}:{number+1}: untranslated Chinese prose')
        english = retain_anchors(original, english)
        chinese = retain_anchors(original, chinese)
        selector = f'[English]({p.name}) | [简体中文]({PurePosixPath(peer).name})\n\n'
        put(path, selector + english)
        put(peer, selector + chinese)
    changes = {p: data for p, data in changes.items() if data != originals.get(p)}
    if not changes:
        raise ValueError('plan makes no changes')
    expected = plan.get('expected_outputs')
    actual = {p: blob_sha(data) for p, data in sorted(changes.items())}
    if expected is not None and actual != expected:
        raise ValueError('assembled output differs from locally reviewed Git blobs')
    return originals, changes, actual


def api(repo: str, endpoint: str, payload: dict) -> dict:
    token = os.environ['GH_TOKEN']
    request = urllib.request.Request(
        f'https://api.github.com/repos/{repo}/git/{endpoint}',
        data=json.dumps(payload).encode(), method='POST',
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28',
                 'User-Agent': 'kuasar-documentation-assembly'})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 502, 503, 504) or attempt == 3:
                raise RuntimeError(f'GitHub Git-object request failed: HTTP {exc.code}') from None
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError('unreachable')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, default=Path('.docs-edit/plan.json'))
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--publish-objects', action='store_true')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    repo = plan['repository']
    if not re.fullmatch(r'kuasar-sandbox/[A-Za-z0-9_.-]+', repo):
        raise ValueError('unexpected repository')
    if args.publish_objects and (args.source_root is not None or repo != os.environ['GITHUB_REPOSITORY']):
        raise ValueError('Git objects can only be published to this workflow repository from Git-verified sources')
    originals, changes, hashes = assemble(plan, Path.cwd(), args.source_root)
    args.output.mkdir(parents=True, exist_ok=True)
    diff = []
    for path, data in sorted(changes.items()):
        dest = args.output / 'files' / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        diff.extend(difflib.unified_diff((originals.get(path) or b'').decode().splitlines(keepends=True), data.decode().splitlines(keepends=True), fromfile=f'a/{path}', tofile=f'b/{path}'))
    (args.output/'changes.diff').write_text(''.join(diff))
    report = {'repository': repo, 'parent_sha': plan['parent_sha'], 'blobs': hashes,
              'semantic_review': 'not automated', 'references_modified': False}
    if args.publish_objects:
        entries = []
        for path, data in sorted(changes.items()):
            obj = api(repo, 'blobs', {'content': base64.b64encode(data).decode(), 'encoding': 'base64'})
            if obj['sha'] != hashes[path]:
                raise ValueError('GitHub returned a different blob SHA')
            entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': obj['sha']})
        base_tree = subprocess.check_output(['git','rev-parse',f"{plan['parent_sha']}^{{tree}}"],text=True).strip()
        tree = api(repo,'trees',{'base_tree':base_tree,'tree':entries})
        commit = api(repo,'commits',{'message':plan['message'],'tree':tree['sha'],'parents':[plan['parent_sha']]})
        report.update(commit_sha=commit['sha'], tree_sha=tree['sha'])
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'],'a') as stream:
                stream.write(f"sha={commit['sha']}\n")
    (args.output/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__ == '__main__':
    main()
