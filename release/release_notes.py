"""Reviewed release prose and evidence-aware narrative budgets.

Only the human-readable Release prefix is adapted. Machine evidence remains in
publication_body, with its existing schema and registry reservation.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

import publication_body

ROOT = Path(__file__).resolve().parents[1]
NOTES = ROOT / 'release/notes'
HEADER = '<!-- kuasar-release-notes '
UNITS = ('accelerator', 'connector', 'sandboxer', 'orchestrator', 'runtime', 'vmlinux')
HEADINGS = ('Highlights', 'Component versions', 'Supported environment', 'Quick Start',
            'Production deployment', 'Known limitations', 'Security',
            'Versioning and release channels', 'Component updates')
COMMIT = re.compile(r'^- `([0-9a-f]{12})` [^\n]+ \(\[commit\]\(https://github.com/kuasar-sandbox/([^/]+)/commit/([0-9a-f]{40})\)\)$')
SELECTED = re.compile(r'^Selected version: `([^`\s]+)` → `([^`\s]+)`\.$', re.M)
COMPARISON = re.compile(r'^\[Full comparison\]\((https://github.com/kuasar-sandbox/[^/]+/compare/[^\s)]+)\)$', re.M)


def validate(text, version, units):
    lines = text.splitlines()
    required = [f'# Kuasar Sandbox {version}', *('## ' + h for h in HEADINGS)]
    required += [f'| `{unit}` | `{units[unit]}` |' for unit in UNITS]
    required += ['### ' + unit for unit in UNITS]
    if publication_body.MARKER in text or any(lines.count(line) != 1 for line in required):
        raise ValueError('release notes must retain the exact version, headings, component table and update sections')


def compact_commits(text):
    """Replace only generated commit rows that retain their full comparison."""
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


def immutable_notes_url(path):
    """Link only to the exact committed prose; never invent a source URL."""
    relative = path.resolve().relative_to(ROOT).as_posix()
    revision = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('reviewed notes need an exact publisher commit')
    committed = subprocess.check_output(['git', '-C', str(ROOT), 'show', f'{revision}:{relative}'])
    if committed != path.read_bytes():
        raise ValueError('cannot link uncommitted release notes')
    return f'https://github.com/kuasar-sandbox/kuasar-sandbox/blob/{revision}/{relative}'


def linked_notes(version, units, url):
    """Keep the delivery contract inline; retain all prose at an immutable URL."""
    docs = f'https://github.com/kuasar-sandbox/kuasar-sandbox/blob/{version}/docs'
    lines = [f'# Kuasar Sandbox {version}', '',
        f'[Full release summary and compatibility notes]({url}).', '',
        '## Highlights', '', f'See the [reviewed changes]({url}#highlights).', '',
        '## Component versions', '', '| Release unit | Version |', '|---|---|']
    lines += [f'| `{unit}` | `{units[unit]}` |' for unit in UNITS]
    lines += ['', '## Asset categories', '',
        'Verify every explicit asset with `SHA256SUMS`. Extract native products and platform-release together; '
        'import workbench separately with `docker load -i`. Never extract the image archive into products.', '',
        '## Supported environment', '',
        'Native Linux x86_64 and aarch64. System tests need the documented KVM/TUN, UFFD/BPF, cgroup v2 '
        'and Docker prerequisites. Workbench system mode uses trusted administrator privileged access, '
        'not hostile-root confinement. The binding below records executed cases and qualification_scope. '
        'ARM artifact-only qualification and planned cases do not establish system, network or snapshot execution.', '',
        '## Quick Start', '', f'[Quick Start]({docs}/quickstart.md).', '',
        '## Production deployment', '', f'[Deployment prerequisites and operational requirements]({docs}/deployment.md).', '',
        '## Known limitations', '', f'Read [compatibility and limitations]({url}#known-limitations) before upgrading. '
        'Demo credentials and self-signed certificates are for evaluation only.', '',
        '## Security', '', '[Report vulnerabilities privately](https://github.com/kuasar-sandbox/kuasar-sandbox/security/advisories/new).', '',
        '## Versioning and release channels', '',
        ('Preview prerelease; does not replace Stable.' if '-preview.' in version else 'Stable, non-prerelease release. Preview remains the development channel.'), '',
        '## Component updates']
    for unit in UNITS:
        lines += ['', '### ' + unit, '', f'Selected version: `{units[unit]}`. [Changes and comparison]({url}#{unit}).']
    return '\n'.join(lines) + '\n'


class Narrative:
    def __init__(self, text, version, revision, units, override=None):
        self.version, self.units = version, units
        self.path = override if override is not None else NOTES / f'{version}.md'
        self.reviewed = self.path.is_file()
        if override is not None and not self.reviewed:
            raise ValueError('reviewed release notes file is missing')
        if self.reviewed:
            raw = self.path.read_text()
            header, separator, text = raw.partition('\n')
            if not separator or not header.startswith(HEADER) or not header.endswith(' -->'):
                raise ValueError('reviewed notes require an identity header')
            identity = json.loads(header[len(HEADER):-4])
            if identity != {'version': version, 'aggregate_sha': revision, 'components': units}:
                raise ValueError('reviewed notes source/version/component selection differs from validated stage')
            validate(text, version, units)
        self.text = text

    def fit(self, prefix, available):
        compact = compact_commits(prefix)
        if len(compact) <= available:
            print(f'Release notes: generated commit rows replaced by comparisons; '
                  f'original={len(prefix)}, adjusted={len(compact)}, budget={available}', file=sys.stderr)
            return compact
        if self.reviewed:
            compact = linked_notes(self.version, self.units, immutable_notes_url(self.path)) + '\n'
            validate(compact, self.version, self.units)
            if len(compact) <= available:
                print(f'Release notes: full reviewed prose retained at its publisher commit; '
                      f'original={len(prefix)}, adjusted={len(compact)}, budget={available}', file=sys.stderr)
                return compact
        # Give publication_body the unchanged text so its final limit reports
        # exact total and block sizes, even if evidence alone exceeds the limit.
        return prefix


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('notes', type=Path)
    args = parser.parse_args()
    original = args.notes.read_text()
    adjusted = original if len(original) <= publication_body.MAX_CHARACTERS else compact_commits(original)
    publication_body.checked(adjusted)
    if adjusted != original:
        args.notes.write_text(adjusted)
