"""Editable release prose and evidence-aware publication budgets.

Only the human-readable Release prefix is adapted. Machine evidence remains in
publication_body, with its existing schema and registry reservation.
"""
import argparse
import base64
from pathlib import Path
import re
import subprocess
import sys

import publication_body
import selection

ROOT = Path(__file__).resolve().parents[1]
NOTICE_START = '<!-- kuasar-release-notice -->'
NOTICE_END = '<!-- /kuasar-release-notice -->'
UNITS = ('accelerator', 'connector', 'sandboxer', 'orchestrator', 'runtime', 'vmlinux')
HEADINGS = ('Highlights', 'Component versions', 'Supported environment', 'Quick Start',
            'Production deployment', 'Known limitations', 'Security',
            'Versioning and release channels', 'Component updates')
COMMIT = re.compile(r'^- `([0-9a-f]{12})` [^\n]+ \(\[commit\]\(https://github.com/kuasar-sandbox/([^/]+)/commit/([0-9a-f]{40})\)\)$')
INVENTORY_COMMIT = re.compile(r'^- [^\n]+ \((?:\[#([0-9]+)\]\(https://github.com/kuasar-sandbox/([^/]+)/pull/([0-9]+)\), )?\[commit ([0-9a-f]{12})\]\(https://github.com/kuasar-sandbox/([^/]+)/commit/([0-9a-f]{40})\)\)\.$')
SELECTED = re.compile(r'^Selected version: `([^`\s]+)` → `([^`\s]+)`\.$', re.M)
COMPARISON = re.compile(r'^\[Full comparison\]\((https://github.com/kuasar-sandbox/[^/]+/compare/[^\s)]+)\)$', re.M)


def inline_notice(text):
    """A marked top-level upgrade notice must survive every narrative fallback."""
    if NOTICE_START not in text and NOTICE_END not in text:
        return ''
    if text.count(NOTICE_START) != 1 or text.count(NOTICE_END) != 1:
        raise ValueError('release notice requires exactly one matching marker pair')
    before, _, tail = text.partition(NOTICE_START)
    notice, _, after = tail.partition(NOTICE_END)
    if (before.strip() != text.splitlines()[0] or not before.startswith('# Kuasar Sandbox ')
            or not notice.startswith('\n## ') or not notice.endswith('\n')
            or NOTICE_END in before or (after and not after.startswith('\n'))):
        raise ValueError('release notice must be a complete block immediately after the title')
    return NOTICE_START + notice + NOTICE_END


def validate(text, version, units):
    lines = text.splitlines()
    required = [f'# Kuasar Sandbox {version}', *('## ' + h for h in HEADINGS)]
    required += [f'| `{unit}` | `{units[unit]}` |' for unit in UNITS]
    required += ['### ' + unit for unit in UNITS]
    if publication_body.MARKER in text or any(lines.count(line) != 1 for line in required):
        raise ValueError('release notes must retain the exact version, headings, component table and update sections')
    inline_notice(text)


def compact_commits(text):
    """Replace only generated commit rows that retain their full comparison."""
    notice = inline_notice(text)
    if notice:
        before, _, after = text.partition(notice)
        return before + notice + compact_commits(after)
    parts = re.split(r'(?m)(^### [^\n]+\n)', text)
    for index in range(2, len(parts), 2):
        unit = parts[index - 1].removeprefix('### ').strip()
        repository = 'guest-runtime' if unit in ('runtime', 'vmlinux') else unit
        section = parts[index]
        selected = SELECTED.findall(section)
        if unit not in UNITS or len(selected) != 1:
            continue
        previous, current = selected[0]
        expected = f'https://github.com/kuasar-sandbox/{repository}/compare/{previous}...{current}'
        comparisons = COMPARISON.findall(section)
        if len(comparisons) != 1 or (comparisons[0] != expected and re.fullmatch(
                re.escape(f'https://github.com/kuasar-sandbox/{repository}/compare/') +
                r'[0-9a-f]{40}\.\.\.[0-9a-f]{40}', comparisons[0]) is None):
            continue
        lines = section.splitlines(keepends=True)
        def generated(line):
            commit = COMMIT.fullmatch(line.rstrip('\n'))
            if commit is not None:
                return commit[2] == repository and commit[3].startswith(commit[1])
            commit = INVENTORY_COMMIT.fullmatch(line.rstrip('\n'))
            return (commit is not None and commit[5] == repository and commit[6].startswith(commit[4])
                    and (commit[1] is None or (commit[2] == repository and commit[1] == commit[3])))
        parts[index] = ''.join(line for line in lines if not generated(line)
            and line.rstrip('\n') != '- GitHub truncated the commit list; use the full comparison below.')
    return ''.join(parts)


def replace_manifest_body(text, relative, body):
    """Change only the literal body, retaining selection, policy and comments."""
    _, block = selection.split_notes(text, relative)
    fields = selection.parse_notes(block, relative)
    if not fields.get('body'):
        raise ValueError('automatic compaction requires a committed notes.body')
    lines = re.findall(r'[^\n]*\n|[^\n]+$', block)
    start = next(i for i, line in enumerate(lines) if re.match(r'  body: +\|', line))
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith('    ')):
        end += 1
    style = '|' if body.endswith('\n') else '|-'
    header = re.sub(r'(  body: +)\|-?', lambda match: match[1] + style, lines[start])
    newline = '\r\n' if header.endswith('\r\n') else '\n'
    replacement = header.rstrip('\r\n') + newline + ''.join(
        '    ' + line + newline for line in body.removesuffix('\n').split('\n'))
    revised = ''.join(lines[:start]) + replacement + ''.join(lines[end:])
    result = text.replace(block, revised, 1)
    checked = selection.read_simple_yaml(result, relative)
    original = selection.read_simple_yaml(text, relative)
    expected = dict(original, notes=dict(fields, body=body))
    if checked != expected:
        raise ValueError('compaction changed fields other than notes.body')
    return result


def persist_compacted_notes(fields, body, version, source_ref, source_url):
    """CAS the same channel on its live maintained branch, then verify readback.

    Uses the existing manifest writer. No retry on a denied/uncertain mutation,
    no changes to the admitted product source, and no branch-protection bypass.
    """
    import preview_coordinator as coordinator
    if not source_ref or not (source_ref == 'main' or re.fullmatch(r'release/v[0-9]+\.[0-9]+\.x', source_ref)):
        raise ValueError('automatic compaction requires the maintained source branch')
    relative = 'releases/daily-preview.yaml' if '-preview.' in version else 'releases/release.yaml'
    provenance = re.fullmatch(
        re.escape(f'https://github.com/{coordinator.PLATFORM_REPOSITORY}/blob/') +
        r'([0-9a-f]{40})/' + re.escape(relative) + r'#L[0-9]+-L[0-9]+', source_url or '')
    if provenance is None:
        raise ValueError('automatic compaction requires the canonical committed notes snapshot')
    head = coordinator.branch_sha(coordinator.PLATFORM_REPOSITORY, source_ref)
    def read(revision):
        record = coordinator.api(f'repos/{coordinator.PLATFORM_REPOSITORY}/contents/{relative}?ref={revision}')
        return base64.b64decode(record['content']).decode()
    current = read(head)
    snapshot = selection.read_simple_yaml(read(provenance[1]), relative)
    live = selection.read_simple_yaml(current, relative)
    if (snapshot.get('notes') != fields or
            {key: value for key, value in snapshot.items() if key != 'notes'} !=
            {key: value for key, value in live.items() if key != 'notes'}):
        raise ValueError('manifest selection advanced; rescan before automatic compaction')
    selected, _, _ = selection.parse_manifest(current, relative, '-preview.' in version)
    if selected != version or selection.read_simple_yaml(current, relative).get('notes') != fields:
        raise ValueError('manifest advanced or notes changed; rescan before automatic compaction, or edit and commit notes manually')
    revised = replace_manifest_body(current, relative, body)
    sha = coordinator.commit_manifest(relative, revised, version, source_ref, head)
    if coordinator.branch_sha(coordinator.PLATFORM_REPOSITORY, source_ref) != sha or read(sha) != revised:
        raise ValueError('compacted manifest commit was not confirmed; rescan before publication')
    committed = selection.read_simple_yaml(read(sha), relative)['notes']
    if committed != dict(fields, body=body):
        raise ValueError('committed compacted notes differ; publication stopped')
    return committed['body']


def stale_warnings(text, version, units):
    """Warn only on recognizable generated selection references, not prose."""
    warnings = []
    titles = re.findall(r'^# Kuasar Sandbox (release-v[^\s]+)\s*$', text, re.M)
    if any(title != version for title in titles):
        warnings.append('release title names a different aggregate version')
    rows = re.findall(r'^\| `([^`]+)` \| `([^`]+)` \|[ \t]*$', text, re.M)
    for unit, tag in rows:
        if unit in units and tag != units[unit]:
            warnings.append(f'component table names a different {unit} version')
    docs = re.findall(r'\[(?:Quick Start|Deployment)\]\(https://github\.com/kuasar-sandbox/kuasar-sandbox/blob/(release-v[^/\s)]+)/docs/(?:quickstart|deployment)\.md(?:#[^\s)]*)?\)', text)
    if any(tag != version for tag in docs):
        warnings.append('documentation link names a different aggregate version')
    return warnings


def publication_notes(publisher, version, fallback=None):
    """Read the newest channel prose for this version, independently of acceptance.

    The publisher may have advanced to another release. Search its first-parent
    manifest history rather than using that other version's notes. Maintenance
    branches absent from publisher history can supply their own manifest. None
    of these text sources selects or changes the product validation inputs.
    """
    preview = '-preview.' in version
    relative = 'releases/daily-preview.yaml' if preview else 'releases/release.yaml'
    roots = [publisher] + ([fallback] if fallback is not None and fallback != publisher else [])
    for root in roots:
        head = selection.git_commit(root, 'HEAD')
        if head is None:
            continue
        shown = subprocess.run(['git', '-C', str(root), 'show', f'{head}:{relative}'],
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        def snapshots():
            if shown.returncode == 0:
                yield head, shown.stdout
            yield from selection.git_snapshots(root, Path(relative))
        seen = set()
        for revision, text in snapshots():
            if revision in seen:
                continue
            seen.add(revision)
            # Read only routing fields; component selection is validated by the
            # product plan, not by the provenance of human-readable text.
            base = re.findall(r'(?m)^version: +([^ #\r\n]+)(?: +#.*)? *$', text)
            suffix = re.findall(r'(?m)^preview_version: +([^ #\r\n]+)(?: +#.*)? *$', text)
            selected = base[0] if len(base) == 1 else None
            if preview:
                selected = selected + '-' + suffix[0] if selected and len(suffix) == 1 else None
            if selected != version:
                continue
            _, block = selection.split_notes(text, relative)
            if not block:
                return {}, None
            fields = selection.parse_notes(block, relative)
            start = next(i for i, line in enumerate(text.splitlines(), 1) if line.startswith('notes:'))
            end = start + len(block.rstrip().splitlines()) - 1
            return fields, f'https://github.com/kuasar-sandbox/kuasar-sandbox/blob/{revision}/{relative}#L{start}-L{end}'
    return {}, None


class Narrative:
    def __init__(self, text, fields=None, url=None, *, version=None, units=None, persist=None):
        self.version, self.units = version, units
        self.persist = persist
        self.generated = text
        self.fields = fields or {}
        self.url = url
        selection.validate_complete_notes(self.fields, 'release narrative')
        self.complete = bool(self.fields)
        self.notice = self.fields.get('upgrade_notice', '')
        self.text = self.render(self.fields.get('body'))

    def render(self, body):
        text = self.generated if body is None else body
        if self.notice:
            text = self.notice.rstrip('\n') + '\n\n' + text
        return text

    def fit(self, prefix, available):
        if len(prefix) <= available or self.fields.get('overflow', 'manual') != 'compact':
            return prefix
        if available < 0:
            raise ValueError('Release evidence exceeds its budget; evidence must not be truncated; manual intervention required')
        if not self.fields.get('body'):
            raise ValueError('automatic compaction requires committed notes.body; evidence must not be truncated')
        compacted = compact_commits(self.fields['body'])
        proposed = self.render(compacted) + prefix[len(self.text):] if prefix.startswith(self.text) else None
        if proposed is None or len(proposed) > available:
            raise ValueError(f'Release notes remain over budget after safe compaction (budget={available}); '
                             'edit and commit notes.body manually; evidence must not be truncated')
        if self.persist is None:
            raise ValueError('automatic compaction needs manifest-write permission before publication; '
                             'edit and commit notes.body manually; evidence must not be truncated')
        committed = self.persist(self.fields, compacted)
        if committed != compacted:
            raise ValueError('compacted manifest readback differs; publication stopped')
        return proposed


def export_notes(platform, version, output):
    fields, _ = publication_notes(platform, version)
    if not fields.get('body'):
        raise ValueError('release manifest has no notes.body to export')
    text = fields['body']
    if fields.get('upgrade_notice'):
        text = fields['upgrade_notice'].rstrip('\n') + '\n\n' + text
    output.write_text(text)


def validate_file(platform, version, notes_file):
    """Check staged prose availability; budget only after evidence is known."""
    text = notes_file.read_text()
    if not text.strip() or text.count(publication_body.MARKER) > 1:
        raise ValueError('release notes must be nonempty with at most one validation binding')


def validate_history(notes_file):
    """Check only transport limits and an unambiguous machine-evidence envelope.

    Human text does not need to match a manifest, source commit or validation
    result. The caller independently verifies the products and machine evidence.
    """
    text = notes_file.read_text()
    if not text.strip() or text.count(publication_body.MARKER) > 1:
        raise ValueError('release notes must be nonempty with at most one validation binding')
    publication_body.checked(text)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ('export', 'validate', 'validate-history'):
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('command', choices=('export', 'validate', 'validate-history'))
        parser.add_argument('platform', type=Path)
        parser.add_argument('version')
        parser.add_argument('notes', type=Path)
        args = parser.parse_args(argv)
        if args.command == 'export':
            export_notes(args.platform, args.version, args.notes)
        elif args.command == 'validate':
            validate_file(args.platform, args.version, args.notes)
        else:
            validate_history(args.notes)
        return
    # Compatibility entry point; transport checks never rewrite human text.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('notes', type=Path)
    args = parser.parse_args(argv)
    publication_body.checked(args.notes.read_text())


if __name__ == '__main__':
    main()
