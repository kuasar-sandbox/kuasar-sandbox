#!/usr/bin/env python3
"""Prepare complete, editable prose in the selected channel manifest.

Generation records source-pinned merge inventories before component publication.
The coordinators provide selected source SHAs, so future tags are not required.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import release_notes
import selection
import change_inventory

ROOT = Path(__file__).resolve().parents[1]


def without_legacy_metadata(block):
    # Drop obsolete metadata when rewriting old manifests; it has no semantics.
    return re.sub(r'(?m)^  (aggregate_sha|selection_sha256): [^\n]*\n?', '', block)


def require_prepared(text, relative):
    selection.parse_manifest(text, relative, relative.endswith('daily-preview.yaml'))
    config = selection.read_simple_yaml(text, relative)
    selection.check_notes_input(config, relative)


def selection_fields(config):
    """Future version policy does not change the selected release or its prose."""
    return {key: value for key, value in config.items()
            if key not in {'notes', 'component_next_versions'}}


def render(root, text, relative, previous_units=None, *, source_shas=None):
    preview = relative.endswith('daily-preview.yaml')
    version, previous, units = selection.parse_manifest(text, relative, preview)
    if previous_units is None:
        previous_units = selection.resolve(root, previous)[1] if previous else {}
    inventory = change_inventory.Inventory()
    source_shas = source_shas or {}
    with tempfile.TemporaryDirectory(prefix='release-notes-generation-') as directory:
        temporary = Path(directory)
        selected = temporary / 'selection.tsv'
        selected.write_text(''.join(f'{unit}\t{units[unit]}\n' for unit in selection.UNITS))
        updates = temporary / 'updates'
        updates.mkdir()
        for unit in selection.UNITS:
            tag = units[unit]
            old = previous_units.get(unit)
            value = inventory.render(unit, old, tag, source_shas.get(unit))
            (updates / f'{unit}.md').write_text(value)
        output = temporary / 'notes.md'
        subprocess.run(['bash', str(ROOT / 'release/aggregate-release.sh'), 'render-notes',
                        version, previous or '', str(selected), str(updates), str(output)], check=True)
        return output.read_text()


def prepare(current, selected, relative, root, *, previous_units=None, source_shas=None):
    """Retain hand edits on retries, regenerate auto text for a new selection."""
    before = selection.read_simple_yaml(current, relative)
    selected, edited_block = selection.split_notes(selected, relative)
    version, _, units = selection.parse_manifest(selected, relative, relative.endswith('daily-preview.yaml'))
    after = selection.read_simple_yaml(selected, relative)
    # The CLI may include an edited notes block with the pending selection.
    # Coordinators pass only their selected fields, retaining committed notes.
    _, original_block = selection.split_notes(current, relative)
    block = edited_block or original_block
    fields = selection.parse_notes(block, relative) if block else {}
    unchanged = selection_fields(before) == selection_fields(after)
    if fields.get('mode') == 'fixed':
        for warning in release_notes.stale_warnings(fields.get('body', ''), version, units):
            print('release notes warning: ' + warning, file=sys.stderr)
        return selected + without_legacy_metadata(block)
    if fields.get('mode') == 'auto' and unchanged:
        return selected + without_legacy_metadata(block)
    body = render(root, selected, relative, previous_units, source_shas=source_shas)
    if fields and 'mode' not in fields:
        # One explicit migration retains BOTH formerly authored prose fields.
        # Legacy notice markers are an internal wrapper, not authored content.
        body = release_notes.Narrative(body, fields).text
        body = body.replace(release_notes.NOTICE_START + '\n', '').replace(release_notes.NOTICE_END, '')
    body = body.rstrip('\n') + '\n'
    note_block = ('notes:\n  mode: auto\n  body: |\n' +
                  ''.join(('    ' + line if line else '') + '\n' for line in body[:-1].split('\n')))
    result = selected + note_block
    selection.parse_manifest(result, relative, relative.endswith('daily-preview.yaml'))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('platform', type=Path)
    parser.add_argument('channel', choices=('release', 'daily-preview'))
    parser.add_argument('--check', action='store_true', help='check the manifest text input without changing it')
    parser.add_argument('--source-sha', action='append', default=[], metavar='UNIT=SHA',
                        help='selected source for a component tag that is not published yet (repeatable)')
    args = parser.parse_args()
    root = args.platform.resolve()
    relative = f'releases/{args.channel}.yaml'
    path = root / relative
    selected = path.read_text()
    if args.check:
        require_prepared(selected, relative)
        return
    source_shas = {}
    for value in args.source_sha:
        unit, separator, sha = value.partition('=')
        if not separator or unit not in selection.UNITS or unit in source_shas or not change_inventory.SHA.fullmatch(sha):
            parser.error('--source-sha requires one unique known UNIT=40-character-SHA')
        source_shas[unit] = sha
    committed = subprocess.run(['git', '-C', str(root), 'show', f'HEAD:{relative}'],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    current = committed.stdout if committed.returncode == 0 else selected
    before = selection.read_simple_yaml(current, relative)
    pending = selection.read_simple_yaml(selected, relative)
    fields = pending.get('notes', {})
    if (fields.get('mode') == 'auto' and fields != before.get('notes', {}) and
            selection_fields(before) != selection_fields(pending)):
        parser.error('selection and auto notes are both modified relative to HEAD; '
                     'review and commit them together; refusing to overwrite the edited notes')
    content = prepare(current, selected, relative, root, source_shas=source_shas)
    if content != selected:
        path.write_text(content)
    print(f'Prepared {relative}; edit the complete notes as needed and commit the manifest.')


if __name__ == '__main__':
    main()
