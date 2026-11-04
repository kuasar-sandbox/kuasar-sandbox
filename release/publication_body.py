"""Check human Release notes before any publication writes."""
import re

MAX_CHARACTERS = 125000
WORKBENCH_REPOSITORY = 'ghcr.io/kuasar-sandbox/workbench'


def has_machine_records(text):
    return re.search(r'<!--\s*kuasar-(?:integration-validation|preview-binding|release-source)\b', text) is not None


def checked(text):
    if not text.strip():
        raise ValueError('Release notes are empty')
    if has_machine_records(text):
        raise ValueError('Release notes must not contain machine validation or source markers')
    if len(text) > MAX_CHARACTERS:
        raise ValueError(f'Release body exceeds {MAX_CHARACTERS} characters (actual={len(text)}, budget={MAX_CHARACTERS}); '
                         'notes must not be truncated; manual intervention required: edit and commit notes.body before retrying publication')
    return text


if __name__ == '__main__':
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('notes', type=Path)
    args = parser.parse_args()
    checked(args.notes.read_text())
