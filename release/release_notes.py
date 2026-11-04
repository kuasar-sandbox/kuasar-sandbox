"""Editable human release notes, preserved verbatim through publication."""
import argparse
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
HEADINGS = ('Component versions', 'Component updates', 'Download and use',
            'Security')


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
    if publication_body.has_machine_records(text) or any(lines.count(line) != 1 for line in required):
        raise ValueError('release notes must retain the exact version, headings, component table and update sections')
    inline_notice(text)


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
    def __init__(self, text, fields=None, url=None, *, version=None, units=None):
        self.version, self.units = version, units
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


def export_notes(platform, version, output):
    fields, _ = publication_notes(platform, version)
    if not fields.get('body'):
        raise ValueError('release manifest has no notes.body to export')
    text = fields['body']
    if fields.get('upgrade_notice'):
        text = fields['upgrade_notice'].rstrip('\n') + '\n\n' + text
    output.write_text(text)


def validate_file(platform, version, notes_file):
    """Check staged human prose before the publication budget gate."""
    text = notes_file.read_text()
    if not text.strip() or publication_body.has_machine_records(text):
        raise ValueError('release notes must be nonempty human prose without machine records')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ('export', 'validate'):
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('command', choices=('export', 'validate'))
        parser.add_argument('platform', type=Path)
        parser.add_argument('version')
        parser.add_argument('notes', type=Path)
        args = parser.parse_args(argv)
        if args.command == 'export':
            export_notes(args.platform, args.version, args.notes)
        else:
            validate_file(args.platform, args.version, args.notes)
        return
    # Compatibility entry point; transport checks never rewrite human text.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('notes', type=Path)
    args = parser.parse_args(argv)
    publication_body.checked(args.notes.read_text())


if __name__ == '__main__':
    main()
