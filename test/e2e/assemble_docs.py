#!/usr/bin/env python3
"""Assemble Markdown and rebase navigation without editing executable content."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from html import unescape
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from package_inputs import guide_inputs, unit_guide_inputs, regular_input
from urllib.parse import quote, unquote, urlsplit, urlunsplit

OWNERS = ('platform', 'accelerator', 'connector', 'guest-runtime', 'sandboxer', 'orchestrator')
REPOS = {owner: ('kuasar-sandbox' if owner == 'platform' else owner) for owner in OWNERS}
REPOS['vmlinux'] = 'guest-runtime'
INLINE = re.compile(r'(!?\[[^\]\n]*\]\(\s*)(<[^>\n]+>|[^\s]+?)(\s+(?:"[^"\n]*"|\x27[^\x27\n]*\x27))?(\s*\))')
REFERENCE = re.compile(r'^(\s{0,3}\[[^\]]+\]:\s*)(<[^>]+>|\S+)')
HTML_LINK = re.compile(r'\b(href|src)=("|\x27)(.*?)\2')
CODE = re.compile(r'(`+).*?\1')
FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})')
HEADING = re.compile(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$')
EXPLICIT = re.compile(r'<(?:a|[a-z][\w-]*)\b[^>]*\b(?:id|name)=["\x27]([^"\x27]+)["\x27]', re.I)
# Only dotted chapter prefixes, not meaningful leading numbers such as "32 MiB".
CHAPTER = re.compile(r'^(?:\d+\.\s+|\d+(?:\.\d+)+\.?\s+)')


LEGACY_RELEASE_GUIDANCE = re.compile(r'SHA256SUMS|SOURCES\.tsv|GO-BUILD-INFO\.tsv|GO-MODULES\.tsv|MATERIALS\.sha256|kuasar-(?:preview-binding|release-source)')


def release_erratum(text: str, owner: str, path: Path, dest: Path, mapping) -> str:
    """Keep selected-tag documentation, with an explicit current distribution erratum."""
    if owner == 'platform' or dest.parts[:2] == ('guide', 'licenses') or not LEGACY_RELEASE_GUIDANCE.search(text):
        return ''
    chinese = path.stem.endswith('_zh')
    suffix = '_zh' if chinese else ''
    guide = (mapping.get(('platform', f'docs/installation{suffix}.md')) or
             mapping.get(('platform', f'docs/download{suffix}.md')))
    if guide is None:
        raise ValueError('missing aggregate download guide for historical documentation erratum')
    link = quote(os.path.relpath(guide, dest.parent), safe='/._-')
    if chinese:
        return ('> **本聚合发行文档勘误：** 本页保留所选组件 Tag 的原始说明。'
                '其中关于下载或审核独立 `SHA256SUMS`、Release 正文机器块、历史 Actions 验收记录、来源证明旁车的要求，对本聚合均不适用。'
                '本聚合不发布独立校验附件或正文机器记录；新建 platform、helper 和 Workbench 包不携带来源或历史测试记录。复用的历史组件包可能保留旧来源文件，消费者忽略这些文件，不以此判断资格。下载和离线复核统一按[本包下载指南](' + link + ')执行：从所选聚合 Tag 的清单取得精确包名，并校验 GitHub 原生资产大小和摘要。'
                '运行所需的 `vmlinux.sha256` 保留。本勘误不改变组件运行语义或许可证。\n\n')
    return ('> **Aggregate distribution erratum:** This page preserves the selected component tag\'s original guidance. '
            'Any requirement below to obtain or audit independent `SHA256SUMS`, machine blocks in release notes, historical Actions acceptance, or source-proof sidecars does not apply to this aggregate. '
            'This aggregate publishes no independent checksum attachment or machine records in its release notes. Newly built platform, helper and Workbench packages carry no source or historical test records. Reused historical component archives may retain old source files; consumers ignore them as eligibility evidence. Follow the [bundled download guide](' + link + ') for downloads and offline checks: resolve exact package names from the selected aggregate tag\'s manifest and verify native GitHub asset sizes and digests. '
            'The runtime file `vmlinux.sha256` remains required. This erratum does not change component runtime behavior or license text.\n\n')


def source_files(root: Path, owner: str):
    yield from (guide_inputs(root) if owner in OWNERS else unit_guide_inputs(root, owner))
    # Legal material is not filtered by audience or rewritten as user guidance.
    for path in sorted(root.iterdir()):
        if path.name in {'LICENSE', 'NOTICE', 'COPYING', 'COPYRIGHT'} or path.name.startswith(('LICENSE.', 'LICENSE_')):
            yield path.relative_to(root)
    licenses = root / 'LICENSES'
    if licenses.exists():
        if licenses.is_symlink():
            raise ValueError(f'symbolic link in legal inputs: {licenses}')
        for path in sorted(licenses.rglob('*')):
            if path.is_file() or path.is_symlink():
                yield path.relative_to(root)


def destination(owner: str, path: Path) -> Path:
    if path.parts[0] == 'LICENSES' or path.name in {'LICENSE', 'NOTICE', 'COPYING', 'COPYRIGHT'} or path.name.startswith(('LICENSE.', 'LICENSE_')):
        return Path('guide/licenses') / owner / path
    if owner == 'platform' and path.parts[0] in {'test', 'workbench'}:
        return path
    if owner == 'platform' and path.as_posix() in {'docs/README.md', 'docs/README_zh.md'}:
        return Path('guide') / ('index_zh.md' if path.stem.endswith('_zh') else 'index.md')
    if path.parts[:2] == ('test', 'e2e'):
        return Path('test/e2e') / owner / Path(*path.parts[2:])
    if owner == 'platform':
        return Path('guide') / (path.relative_to('docs') if path.parts[0] == 'docs' else path)
    relative = path.relative_to('docs') if path.parts[0] == 'docs' else path
    return Path('guide') / owner / relative


def source_ref(root: Path) -> str:
    try:
        # Avoid picking up a parent workspace's unrelated Git repository.
        if not (root / '.git').exists():
            return 'main'
        return subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        return 'main'


def document_anchors(text: str):
    """Return real anchors and unnumbered-heading aliases, without changing text."""
    lines, fence = [], None
    for number, line in enumerate(text.splitlines()):
        mark = FENCE.match(line)
        if mark:
            token = mark.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not line.strip()[len(token):].strip():
                fence = None
            continue
        if fence is None:
            lines.append((number, line))

    def slug(value):
        value = unescape(re.sub(r'<[^>]*>', '', value)).lower()
        value = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', value)
        return re.sub(r'[^\w\-\s]', '', value, flags=re.UNICODE).replace(' ', '-')

    found, seen, unnumbered = set(), Counter(), defaultdict(set)
    for index, (number, line) in enumerate(lines):
        found.update(EXPLICIT.findall(line))
        match = HEADING.match(line)
        heading = match.group(1) if match else None
        if heading is None and index + 1 < len(lines) and lines[index + 1][0] == number + 1 and re.fullmatch(r'\s{0,3}(?:=+|-+)\s*', lines[index + 1][1]) and line.strip():
            heading = line.strip()
        if heading:
            base = slug(heading)
            actual, suffix = base, seen[base]
            while actual in found:
                suffix += 1
                actual = f'{base}-{suffix}'
            found.add(actual)
            seen[base] = suffix
            stripped = CHAPTER.sub('', heading, count=1)
            if stripped != heading:
                unnumbered[slug(stripped)].add(actual)
    return found, unnumbered


def migration_reference(value, owner, roots):
    """A migration key is an owner-relative Markdown path plus optional fragment."""
    if not isinstance(value, str) or not value:
        raise ValueError('invalid documentation migration reference')
    url = urlsplit(value)
    path, fragment = unquote(url.path), unquote(url.fragment)
    if (url.scheme or url.netloc or url.query or not path.endswith('.md') or
            path.startswith('/') or '\\' in path or
            any(part in {'', '.', '..'} or part.startswith('.') for part in path.split('/')) or
            any(char.isspace() or ord(char) < 32 for char in path + fragment)):
        raise ValueError(f'unsafe documentation migration reference: {value}')
    relative = Path(path)
    if any((roots[owner] / parent).is_symlink() for parent in (relative, *relative.parents)):
        raise ValueError(f'symbolic link in documentation migration reference: {owner}/{path}')
    return owner, path, fragment


def guide_migrations(roots):
    """Read optional owner declarations; absent metadata is valid for old tags."""
    redirects, fallbacks = {}, {}

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate documentation migration field: {key}')
            result[key] = value
        return result

    for owner, root in roots.items():
        path = Path('release/guide-migrations.json')
        if not (root / path).exists() and not (root / path).is_symlink():
            continue
        source = regular_input(root, path)
        data = json.loads(source.read_text(encoding='utf-8'), object_pairs_hook=unique_object)
        if (not isinstance(data, dict) or type(data.get('schema')) is not int or
                data['schema'] not in {1, 2} or
                set(data) != ({'schema', 'redirects'} if data['schema'] == 1 else
                              {'schema', 'redirects', 'fallbacks'}) or
                not isinstance(data['redirects'], list) or
                (data['schema'] == 2 and not isinstance(data['fallbacks'], list))):
            raise ValueError(f'invalid documentation migration schema: {owner}/{path}')
        for row in data['redirects']:
            if (not isinstance(row, dict) or not {'from', 'to'} <= set(row) or
                    set(row) - {'from', 'to', 'to_owner'}):
                raise ValueError(f'invalid documentation migration entry: {owner}/{path}')
            target_owner = row.get('to_owner', owner)
            if not isinstance(target_owner, str) or target_owner not in roots:
                raise ValueError(f'unselected documentation migration owner: {target_owner}')
            start = migration_reference(row['from'], owner, roots)
            end = migration_reference(row['to'], target_owner, roots)
            if start in redirects:
                raise ValueError(f'duplicate documentation migration: {start}')
            redirects[start] = end

        for row in data.get('fallbacks', []):
            if (not isinstance(row, dict) or not {'from_owner', 'from', 'to'} <= set(row) or
                    set(row) - {'from_owner', 'from', 'to_owner', 'to'}):
                raise ValueError(f'invalid documentation fallback entry: {owner}/{path}')
            source_owner = row['from_owner']
            target_owner = row.get('to_owner', source_owner)
            if (not isinstance(source_owner, str) or source_owner not in roots or
                    not isinstance(target_owner, str) or target_owner not in roots):
                raise ValueError(f'unselected documentation fallback owner: {source_owner} -> {target_owner}')
            start = migration_reference(row['from'], source_owner, roots)
            end = migration_reference(row['to'], target_owner, roots)
            if start in fallbacks:
                raise ValueError(f'duplicate documentation fallback: {start}')
            fallbacks[start] = end

    for start in redirects.keys() & fallbacks.keys():
        # The platform may carry the same move for an older owner tag that
        # predates its declaration. Distinct destinations remain ambiguous.
        if redirects[start] != fallbacks[start]:
            raise ValueError('documentation fallback conflicts with a declared migration')

    # Declarations point to final selected guides, not a redirect-page graph.
    # Reject cyclic declarations even when a still-valid old path would win.
    for rules, kind in ((redirects, 'migration'), (fallbacks, 'fallback')):
        for start in rules:
            seen, current = set(), start
            while current in rules:
                if current in seen:
                    raise ValueError(f'cyclic documentation {kind}: {start}')
                seen.add(current)
                current = rules[current]
    return redirects, fallbacks


def assemble(output: Path, roots: dict[str, Path], refs: dict[str, str], kernel: Path | None = None):
    mapping: dict[tuple[str, str], Path] = {}
    inputs: dict[Path, tuple[str, Path]] = {}
    if kernel is not None:
        roots = {**roots, 'vmlinux': kernel}
    refs = {owner: refs.get(owner, source_ref(root)) for owner, root in roots.items()}
    for owner, root in roots.items():
        paths = source_files(root, owner)
        for path in paths:
            if owner == 'guest-runtime' and kernel is not None and path.as_posix() in {'docs/vmlinux.md', 'docs/vmlinux_zh.md'}:
                continue
            if not (root / path).is_file():
                if owner == 'vmlinux' and path.name.endswith('_zh.md'):
                    continue  # Older selected kernel tags can be English-only.
                raise ValueError(f'missing documentation input: {owner}/{path}')
            regular_input(root, path)
            dest = destination(owner, path)
            if dest in inputs:
                raise ValueError(f'documentation collision at {dest}: {inputs[dest][0]} and {owner}')
            inputs[dest] = (owner, path)
            mapping[owner, path.as_posix()] = dest
            if owner == 'vmlinux' and path.as_posix() in {'docs/vmlinux.md', 'docs/vmlinux_zh.md'}:
                mapping['guest-runtime', path.as_posix()] = dest

    redirects, fallbacks = guide_migrations(roots)
    navigation = {}

    def selected_source(target):
        selected_owner, selected_path = inputs[target]
        return roots[selected_owner] / selected_path

    def anchor_data(source):
        if source not in navigation:
            navigation[source] = document_anchors(source.read_text(encoding='utf-8'))
        return navigation[source]

    def exact_fragment(source, fragment):
        return not fragment or source.suffix.lower() != '.md' or unquote(fragment) in anchor_data(source)[0]

    def resolve_fragment(source, fragment):
        if exact_fragment(source, fragment):
            return fragment
        matches = anchor_data(source)[1].get(unquote(fragment), set())
        if len(matches) == 1:
            return next(iter(matches))
        reason = 'ambiguous unnumbered heading' if matches else 'missing heading/anchor'
        raise ValueError(f'{reason}: {source}#{fragment}')

    for start, (target_owner, target_path, fragment) in redirects.items():
        target = mapping.get((target_owner, target_path))
        if target is None:
            raise ValueError(f'documentation migration target is not selected: {start} -> {target_owner}/{target_path}')
        resolve_fragment(selected_source(target), fragment)

    def reference_source(reference):
        owner, path, fragment = reference
        target = mapping.get((owner, path))
        return selected_source(target) if target is not None else roots[owner] / path

    def selected_reference_exists(reference):
        source = reference_source(reference)
        if not source.is_file():
            return False
        try:
            resolve_fragment(source, reference[2])
        except ValueError:
            return False
        return True

    def moved_reference(rules, key, fragment):
        result = rules.get((*key, unquote(fragment)))
        if result is None and fragment:
            page = rules.get((*key, ''))
            if page is not None:
                # A page move does not authorize dropping an unknown anchor.
                result = (*page[:2], unquote(fragment))
        return result

    def resolve_fallback(reference):
        seen = set()
        while not selected_reference_exists(reference):
            if reference in seen:
                raise ValueError(f'cyclic documentation fallback: {reference}')
            seen.add(reference)
            following = moved_reference(fallbacks, reference[:2], reference[2])
            if following is None:
                source = reference_source(reference)
                if source.is_file():
                    resolve_fragment(source, reference[2])
                raise ValueError(f'no selected documentation fallback endpoint: {reference}')
            reference = following
        return reference

    # A compatibility chain must end in a real document in the selected sources,
    # never a newer checkout or a synthesized contract. Unpackaged source pages
    # retain the normal immutable source-URL behavior below.
    for start in fallbacks:
        resolve_fallback(start)

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
            # Directory browsing has no explicit language. Preserve the source
            # page's language when possible; an explicit fragment keeps the
            # default README so its existing anchor contract does not change.
            readme = 'README_zh.md' if path.stem.endswith('_zh') and not url.fragment else 'README.md'
            target = mapping.get((target_owner, (target_path / readme).as_posix()))
            if target is None:
                target = mapping.get((target_owner, (target_path / 'README.md').as_posix()))
            if target is None and target_path == Path('docs'):
                target = mapping.get((target_owner, readme)) or mapping.get((target_owner, 'README.md'))
        fragment = url.fragment
        source = selected_source(target) if target is not None else original
        valid = (target is not None or original.exists()) and exact_fragment(source, fragment)
        if not valid:
            migration = moved_reference(redirects, key, fragment)
            # A page redirect carries the original fragment. If that fragment
            # moved too, only an explicitly declared fallback may repair it.
            if migration is None or not selected_reference_exists(migration):
                fallback = moved_reference(fallbacks, key, fragment)
                if fallback is not None:
                    migration = resolve_fallback(fallback)
            if migration is not None:
                target_owner, migrated_path, fragment = migration
                target_path = Path(migrated_path)
                target = mapping.get((target_owner, migrated_path))
                original = roots[target_owner] / target_path
                source = selected_source(target) if target is not None else original
        if target is not None:
            fragment = resolve_fragment(source, fragment)
            result = urlunsplit(('', '', quote(os.path.relpath(target, dest.parent), safe='/._-'), url.query, fragment))
        else:
            if not original.exists():
                if url.scheme:
                    return raw  # A selected source need not contain a file from a newer main URL.
                raise ValueError(f'missing source link: {owner}/{path}: {value}')
            kind = 'tree' if original.is_dir() else 'blob'
            fragment = resolve_fragment(source, fragment)
            result = urlunsplit(('https', 'github.com', f'/kuasar-sandbox/{REPOS[target_owner]}/{kind}/{refs[target_owner]}/' + quote(target_path.as_posix(), safe='/._-'), url.query, fragment))
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
            text = source.read_text(encoding='utf-8')
            target.write_text(release_erratum(text, owner, path, dest, mapping) +
                              rewrite(text, owner, path, dest), encoding='utf-8')
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
