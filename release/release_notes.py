"""Reviewed release prose and evidence-aware narrative budgets.

Only the human-readable Release prefix is adapted. Machine evidence remains in
publication_body, with its existing schema and registry reservation.
"""
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
HEADINGS = ('Highlights', 'Component versions', 'Supported environment', 'Quick Start',
            'Production deployment', 'Known limitations', 'Security',
            'Versioning and release channels', 'Component updates')
COMMIT = re.compile(r'^- `([0-9a-f]{12})` [^\n]+ \(\[commit\]\(https://github.com/kuasar-sandbox/([^/]+)/commit/([0-9a-f]{40})\)\)$')
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
        if COMPARISON.findall(section) != [expected]:
            continue
        lines = section.splitlines(keepends=True)
        def generated(line):
            commit = COMMIT.fullmatch(line.rstrip('\n'))
            return commit is not None and commit[2] == repository and commit[3].startswith(commit[1])
        parts[index] = ''.join(line for line in lines if not generated(line)
            and line.rstrip('\n') != '- GitHub truncated the commit list; use the full comparison below.')
    return ''.join(parts)


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


def committed_manifest(root, revision, relative):
    text = subprocess.check_output(['git', '-C', str(root), 'show', f'{revision}:{relative}']).decode('utf-8')
    return text, selection.read_simple_yaml(text, relative)


def publication_notes(platform, version, revision, units, publisher):
    preview = '-preview.' in version
    relative = 'releases/daily-preview.yaml' if preview else 'releases/release.yaml'
    text, admitted = committed_manifest(platform, revision, relative)
    selected, _, components = selection.parse_manifest(text, relative, preview)
    if (selected, components) != (version, units):
        raise ValueError('admitted manifest differs from validated release selection')
    # Default to the exact tested source. A later correction must explicitly
    # target the admitted SHA and may change only notes in the parsed manifest.
    publisher_sha = subprocess.check_output(['git', '-C', str(publisher), 'rev-parse', 'HEAD'], text=True).strip()
    latest_text, latest = committed_manifest(publisher, publisher_sha, relative)
    config, source_sha = admitted, revision
    if latest.get('notes', {}).get('aggregate_sha') is not None:
        latest_version, _, _ = selection.parse_manifest(latest_text, relative, preview)
        if latest_version == version or latest['notes']['aggregate_sha'] == revision:
            if ({k: v for k, v in latest.items() if k != 'notes'} !=
                    {k: v for k, v in admitted.items() if k != 'notes'}):
                raise ValueError('notes revision changes the admitted manifest')
            config, text, source_sha = latest, latest_text, publisher_sha
    fields = config.get('notes', {})
    if fields.get('aggregate_sha', revision) != revision:
        raise ValueError('notes.aggregate_sha differs from the validated source')
    if not fields:
        return {}, None
    start = next(i for i, line in enumerate(text.splitlines(), 1) if line.startswith('notes:'))
    _, block = selection.split_notes(text, relative)
    end = start + len(block.rstrip().splitlines()) - 1
    return fields, f'https://github.com/kuasar-sandbox/kuasar-sandbox/blob/{source_sha}/{relative}#L{start}-L{end}'


def legacy_admitted_source(platform, revision):
    """Read-only bridge for stages admitted before manifest preparation existed.

    New admission always requires complete notes. Dynamic publisher tooling can
    also finish an older, already validated stage; it must not retroactively
    require fields absent from that original admission contract. The absence
    check is a compatibility discriminator, not independent age/authentication
    proof: safety relies on the trusted workflow enforcing require_prepared
    for every new admission, even when a selected source omits this tool.
    """
    value = subprocess.check_output(['git', '-C', str(platform), 'ls-tree', revision,
                                     '--', 'release/prepare_notes.py'], text=True)
    return not value.strip()


def delivery_summary(version, units, url, notice, generated):
    """Retain delivery instructions, selection and comparison links within the body budget."""
    docs = f'https://github.com/kuasar-sandbox/kuasar-sandbox/blob/{version}/docs'
    lines = [f'# Kuasar Sandbox {version}', '']
    if notice:
        lines += [notice, '']
    lines += ['## Highlights', '', f'[Full reviewed release notes]({url}).', '',
              '## Component versions', '', '| Release unit | Version |', '|---|---|']
    lines += [f'| `{unit}` | `{units[unit]}` |' for unit in UNITS]
    lines += ['', '## Asset categories', '', 'Verify each archive with SHA256SUMS. Extract products and platform together. '
              'Import the Workbench image separately with `docker load -i`.', '',
              '## Supported environment', '', 'Native Linux x86_64 and aarch64. System tests require the documented '
              'KVM/TUN, UFFD/BPF, cgroup v2 and Docker prerequisites.', '',
              '## Quick Start', '', f'[Quick Start]({docs}/quickstart.md).', '',
              '## Production deployment', '', f'[Deployment]({docs}/deployment.md).', '',
              '## Known limitations', '', 'See qualification_scope and executed cases below. '
              'ARM artifact-only Workbench qualification does not establish system execution.', '',
              '## Security', '', '[Private vulnerability reporting](https://github.com/kuasar-sandbox/kuasar-sandbox/security/advisories/new).', '',
              '## Versioning and release channels', '', 'Preview prerelease.' if '-preview.' in version else 'Stable release.', '',
              '## Component updates']
    sections = re.split(r'(?m)^### (\w+)\n', generated)
    updates = dict(zip(sections[1::2], sections[2::2]))
    for unit in UNITS:
        lines += ['', '### ' + unit, '', f'Selected version: `{units[unit]}`.']
        lines += [f'[Full comparison]({link}).' for link in COMPARISON.findall(updates.get(unit, ''))]
    summary = '\n'.join(lines) + '\n'
    validate(summary, version, units)
    return summary


class Narrative:
    def __init__(self, text, fields=None, url=None, *, version=None, units=None):
        self.version, self.units = version, units
        self.generated = text
        self.fields = fields or {}
        self.url = url
        selection.validate_complete_notes(self.fields, 'release narrative')
        self.complete = 'mode' in self.fields
        self.notice = ''
        if self.fields.get('upgrade_notice'):
            self.notice = (NOTICE_START + '\n## Important upgrade notice\n\n' +
                           self.fields['upgrade_notice'].rstrip('\n') + '\n' + NOTICE_END)
        self.text = self.render(self.fields.get('body'))

    def render(self, body):
        if self.complete:
            return body
        text = self.generated
        if self.notice:
            title, newline, rest = text.partition('\n')
            if not newline or not title.startswith('# Kuasar Sandbox '):
                raise ValueError('missing release title for upgrade notice')
            text = title + '\n\n' + self.notice + '\n' + rest
        if body:
            heading = '\n## Highlights\n'
            if text.count(heading) != 1:
                raise ValueError('missing or duplicate release highlights')
            text = text.replace(heading, heading + '\n' + body.rstrip('\n') + '\n', 1)
        return text

    def fit(self, prefix, available):
        # Complete manifest prose is immutable at publication time. Let the
        # evidence-aware budget fail rather than compacting or replacing it.
        if self.complete:
            return prefix
        # Compact only generated inventory. Authored Markdown is never passed
        # to the commit-row reducer, even if it resembles generated inventory.
        compact = Narrative(compact_commits(self.generated), self.fields, self.url)
        if len(compact.text + '\n') <= available:
            return compact.text + '\n'
        if self.fields.get('body') and self.url:
            linked = compact.render(f'[Full reviewed release notes]({self.url}).') + '\n'
            if len(linked) <= available:
                return linked
        if self.fields and self.url and self.units is not None:
            minimum = delivery_summary(self.version, self.units, self.url, self.notice, self.generated) + '\n'
            if len(minimum) <= available:
                return minimum
        return prefix


def source_selection(platform, version):
    """Read only the selected committed channel, never an uncommitted edit."""
    revision = subprocess.check_output(['git', '-C', str(platform), 'rev-parse', 'HEAD'], text=True).strip()
    preview = '-preview.' in version
    relative = 'releases/daily-preview.yaml' if preview else 'releases/release.yaml'
    text, config = committed_manifest(platform, revision, relative)
    selected, _, units = selection.parse_manifest(text, relative, preview)
    if selected != version:
        raise ValueError('manifest differs from requested release selection')
    return revision, config, units


def export_notes(platform, version, output):
    _, config, units = source_selection(platform, version)
    fields = config.get('notes', {})
    selection.require_prepared_notes(config, 'committed release manifest')
    publication_body.checked(fields['body'])
    for warning in stale_warnings(fields['body'], version, units):
        print('release notes warning: ' + warning, file=sys.stderr)
    output.write_text(fields['body'])


def validate_file(platform, version, notes_file):
    revision, config, units = source_selection(platform, version)
    text = notes_file.read_text()
    fields = config.get('notes', {})
    bound = publication_body.MARKER in text
    if bound:
        if text.count(publication_body.MARKER) != 1:
            raise ValueError('release notes require exactly one validation binding')
        fields, _ = publication_notes(platform, version, revision, units, ROOT)
    if 'mode' not in fields:
        if not legacy_admitted_source(platform, revision):
            raise ValueError('publication requires complete manifest notes for this admitted source')
        validate(text.split(publication_body.MARKER, 1)[0] if bound else text, version, units)
        return
    selection.validate_complete_notes(fields, 'committed release manifest')
    prefix = text.split(publication_body.MARKER, 1)[0] if bound else text
    expected = fields['body'] + ('\n' if bound else '')
    if prefix != expected:
        raise ValueError('release notes human prefix differs from complete manifest notes.body')
    for warning in stale_warnings(prefix, version, units):
        print('release notes warning: ' + warning, file=sys.stderr)


def validate_history(notes_file):
    """Consume immutable historical prose without applying today's template.

    The aggregate caller still checks its resolved manifest selection, exact
    asset set, hashes, source and Workbench identities. Human prose can have
    come from a reviewed later correction whose commit is no longer main.
    """
    text = notes_file.read_text()
    if not text.strip() or text.count(publication_body.MARKER) > 1:
        raise ValueError('historical notes must be nonempty with at most one validation binding')
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
    # Compatibility for the historical generated-notes compaction command.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('notes', type=Path)
    args = parser.parse_args(argv)
    original = args.notes.read_text()
    adjusted = original if len(original) <= publication_body.MAX_CHARACTERS else compact_commits(original)
    publication_body.checked(adjusted)
    if adjusted != original:
        args.notes.write_text(adjusted)


if __name__ == '__main__':
    main()
