#!/usr/bin/env python3
"""Prepare complete, editable prose in the selected channel manifest.

Generation describes selected tags and links their comparison. It does not
claim to have fetched future tags or their commit inventories.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import publication_body
import release_notes
import selection

ROOT = Path(__file__).resolve().parents[1]


def identity(config):
    """Generation bookkeeping only, never a release admission/source identity."""
    return selection.notes_selection_sha256(config)


def without_legacy_pin(block):
    # Drop obsolete metadata when rewriting old manifests; it has no semantics.
    return re.sub(r'(?m)^  aggregate_sha: [^\n]*\n?', '', block)


def require_prepared(text, relative):
    selection.parse_manifest(text, relative, relative.endswith('daily-preview.yaml'))
    config = selection.read_simple_yaml(text, relative)
    selection.check_notes_input(config, relative)
    publication_body.checked(config['notes']['body'])


def render(root, text, relative, previous_units=None):
    preview = relative.endswith('daily-preview.yaml')
    version, previous, units = selection.parse_manifest(text, relative, preview)
    if previous_units is None:
        previous_units = selection.resolve(root, previous)[1] if previous else {}
    with tempfile.TemporaryDirectory(prefix='release-notes-generation-') as directory:
        temporary = Path(directory)
        selected = temporary / 'selection.tsv'
        selected.write_text(''.join(f'{unit}\t{units[unit]}\n' for unit in selection.UNITS))
        updates = temporary / 'updates'
        updates.mkdir()
        for unit in selection.UNITS:
            tag = units[unit]
            old = previous_units.get(unit)
            repository = 'guest-runtime' if unit in ('runtime', 'vmlinux') else unit
            if old is None:
                value = f'Initial selected version: `{tag}`.\n'
            elif old == tag:
                value = f'Selected version unchanged: `{tag}`.\n'
            else:
                value = (f'Selected version: `{old}` → `{tag}`.\n\n'
                         f'[Full comparison](https://github.com/kuasar-sandbox/{repository}/compare/{old}...{tag})\n')
                if unit == 'vmlinux':
                    value += ('\nThe comparison covers the guest-runtime repository. Kernel release inputs are '
                              '`native-deps/deps/vmlinux/*.config` and `native-deps/deps/linux-patches/`.\n')
            (updates / f'{unit}.md').write_text(value)
        output = temporary / 'notes.md'
        subprocess.run(['bash', str(ROOT / 'release/aggregate-release.sh'), 'render-notes',
                        version, previous or '', str(selected), str(updates), str(output)], check=True)
        return output.read_text()


def prepare(current, selected, relative, root, *, previous_units=None):
    """Retain hand edits on retries, regenerate auto text for a new selection."""
    before = selection.read_simple_yaml(current, relative)
    selected, _ = selection.split_notes(selected, relative)
    version, _, units = selection.parse_manifest(selected, relative, relative.endswith('daily-preview.yaml'))
    after = selection.read_simple_yaml(selected, relative)
    fields = before.get('notes', {})
    _, block = selection.split_notes(current, relative)
    if fields.get('mode') == 'fixed':
        for warning in release_notes.stale_warnings(fields.get('body', ''), version, units):
            print('release notes warning: ' + warning, file=sys.stderr)
        return selected + without_legacy_pin(block)
    if fields.get('mode') == 'auto' and fields.get('selection_sha256') == identity(after):
        return selected + without_legacy_pin(block)
    body = render(root, selected, relative, previous_units)
    if fields and 'mode' not in fields:
        # One explicit migration retains BOTH formerly authored prose fields.
        # Legacy notice markers are an internal wrapper, not authored content.
        body = release_notes.Narrative(body, fields).text
        body = body.replace(release_notes.NOTICE_START + '\n', '').replace(release_notes.NOTICE_END, '')
    publication_body.checked(body)
    body = body.rstrip('\n') + '\n'
    note_block = ('notes:\n  mode: auto\n  selection_sha256: ' + identity(after) + '\n  body: |\n' +
                  ''.join(('    ' + line if line else '') + '\n' for line in body[:-1].split('\n')))
    result = selected + note_block
    selection.parse_manifest(result, relative, relative.endswith('daily-preview.yaml'))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('platform', type=Path)
    parser.add_argument('channel', choices=('release', 'daily-preview'))
    parser.add_argument('--check', action='store_true', help='check the manifest text input without changing it')
    args = parser.parse_args()
    root = args.platform.resolve()
    relative = f'releases/{args.channel}.yaml'
    path = root / relative
    selected = path.read_text()
    if args.check:
        require_prepared(selected, relative)
        return
    content = prepare(selected, selected, relative, root)
    if content != selected:
        path.write_text(content)
    print(f'Prepared {relative}; edit the complete notes as needed and commit the manifest.')


if __name__ == '__main__':
    main()
