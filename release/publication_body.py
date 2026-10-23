"""Bound the complete Release body without discarding validation evidence."""
import json
import re
import sys

# GitHub Release API's body validation limit; fail locally before submission.
# https://github.com/cli/cli/issues/7705
MAX_CHARACTERS = 125000
MARKER = '<!-- kuasar-integration-validation '
WORKBENCH_REPOSITORY = 'ghcr.io/kuasar-sandbox/workbench'
WORKBENCH_ARCHES = ('x86_64', 'aarch64')


def checked(text, *, blocks=None):
    if len(text) > MAX_CHARACTERS:
        detail = f'actual={len(text)}, budget={MAX_CHARACTERS}'
        if blocks is not None:
            detail += ', blocks=' + json.dumps(blocks, sort_keys=True, separators=(',', ':'))
        raise ValueError(f'Release body exceeds {MAX_CHARACTERS} characters ({detail}); evidence must not be truncated')
    return text


def binding_text(prefix, binding, suffix, *, reserve_registry_version=None, fit_notes=None):
    """Budget evidence first, then adapt only the human-readable prefix."""
    def encoded(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'))

    def render(record):
        return prefix + MARKER + encoded(record) + ' -->' + suffix

    plan = binding.get('validation_plan', {})
    duplicate_cases = ('case_files' in binding and isinstance(plan, dict) and 'case_files' in plan)
    if duplicate_cases and encoded(binding['case_files']) != encoded(plan['case_files']):
        raise ValueError('publication case_files differs from validation_plan; refusing to discard conflicting evidence')

    record = dict(binding)
    budget = dict(binding)
    if reserve_registry_version is not None:
        workbench = binding.get('workbench')
        if not isinstance(workbench, dict) or set(workbench) != set(WORKBENCH_ARCHES):
            raise ValueError('registry capacity check requires both workbench architectures')
        for result in workbench.values():
            if (not isinstance(result, dict)
                    or not isinstance(result.get('image_id'), str)
                    or not isinstance(result.get('sha256'), str)
                    or re.fullmatch(r'sha256:[0-9a-f]{64}', result['image_id']) is None
                    or re.fullmatch(r'[0-9a-f]{64}', result['sha256']) is None):
                raise ValueError('registry capacity check requires exact image and archive identities')
    if reserve_registry_version is not None and 'registry' not in binding:
        # Digests have fixed width. Manifest sizes come from len(raw), whose
        # maximum is sys.maxsize. This is only a capacity check: never persist
        # these placeholders as registry evidence.
        budget['registry'] = {
            'reference': WORKBENCH_REPOSITORY + ':' + reserve_registry_version.removeprefix('release-'),
            'digest': 'sha256:' + 'f' * 64,
            'architectures': {
                arch: {'digest': 'sha256:' + 'f' * 64, 'size': sys.maxsize,
                       'image_id': workbench[arch]['image_id'], 'archive_sha256': workbench[arch]['sha256']}
                for arch in WORKBENCH_ARCHES}}
    if len(render(budget)) > MAX_CHARACTERS and duplicate_cases:
        # Existing readers derive this optional inventory from validation_plan.
        # Other projections (notably source_records) are required by readers.
        del record['case_files']
        del budget['case_files']
    if len(render(budget)) > MAX_CHARACTERS and fit_notes is not None:
        available = MAX_CHARACTERS - len(render(budget)) + len(prefix)
        prefix = fit_notes(prefix, available)
        if not isinstance(prefix, str) or MARKER in prefix:
            raise ValueError('notes adjustment must return only human-readable text')
    blocks = {'notes': len(prefix) + len(suffix),
              **{key: len(encoded(value)) for key, value in budget.items()}}
    checked(render(budget), blocks=blocks)
    return render(record)


if __name__ == '__main__':
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('notes', type=Path)
    parser.add_argument('--reserve-registry-version')
    args = parser.parse_args()
    text = args.notes.read_text()
    if args.reserve_registry_version is not None:
        if text.count(MARKER) != 1:
            raise ValueError('registry capacity check requires one validation binding')
        prefix, tail = text.split(MARKER)
        encoded, suffix = tail.split(' -->', 1)
        binding_text(prefix, json.loads(encoded), suffix,
                     reserve_registry_version=args.reserve_registry_version)
    else:
        checked(text)
