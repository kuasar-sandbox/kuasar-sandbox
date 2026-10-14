#!/usr/bin/env python3
"""Trusted, metadata-first documentation qualification; never execute candidates."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_docs

REPOSITORIES = {'kuasar-sandbox/' + name for name in
                ('accelerator', 'connector', 'guest-runtime', 'kuasar-sandbox', 'orchestrator', 'sandboxer')}
COMMON = {'README.md', 'README_zh.md', 'test/e2e/README.md', 'test/e2e/README_zh.md'}
PLATFORM = {f'{base}{suffix}.md' for base in ('workbench/README', 'ci/runner/README', 'test/demo/DEMO')
            for suffix in ('', '_zh')}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value), 'missing exact Git identity')
    return value


def api(endpoint):
    return json.loads(subprocess.check_output(['gh', 'api', endpoint]))


def request():
    primary = {'repository': os.environ['CANDIDATE_REPOSITORY'],
               'pull_request_number': int(os.environ['CANDIDATE_PR']),
               'candidate_sha': os.environ['CANDIDATE_SHA'], 'base_sha': os.environ['CANDIDATE_BASE_SHA'],
               'base_ref': os.environ['CANDIDATE_BASE_REF'], 'head_sha': os.environ['CANDIDATE_HEAD_SHA']}
    records = [primary] + json.loads(os.environ['COMPANION_CANDIDATES'])
    require(len(records) <= 6 and len({r['repository'] for r in records}) == len(records), 'duplicate source repository')
    for row in records:
        for name in ('candidate_sha', 'base_sha', 'head_sha'):
            sha(row[name])
        require(type(row['pull_request_number']) is int and row['pull_request_number'] > 0, 'invalid source PR')
        require(re.fullmatch(r'main|release/v\d+\.\d+\.x', row['base_ref']), 'invalid source base ref')
    return sorted(records, key=lambda row: row['repository'])


def allowed(repository, path):
    if repository not in REPOSITORIES:
        return False
    parts = PurePosixPath(path).parts
    if not parts or any(p in ('.', '..', '.git') for p in parts) or path.startswith('/'):
        return False
    return path in COMMON or (repository == 'kuasar-sandbox/kuasar-sandbox' and path in PLATFORM) or (
        path.endswith('.md') and (path.startswith('docs/') or path.startswith('test/e2e/guides/')))


def tree(repository, commit):
    doc = api(f'repos/{repository}/git/commits/{sha(commit)}')
    require(doc.get('sha') == commit, 'commit identity mismatch')
    root = sha(doc.get('tree', {}).get('sha'))
    data = api(f'repos/{repository}/git/trees/{root}?recursive=1')
    require(data.get('sha') == root and isinstance(data.get('tree'), list), 'missing exact Git tree')
    require(type(data.get('truncated')) is bool, 'unknown Git tree completeness')
    entries = {}
    for row in data['tree']:
        path = row.get('path')
        require(isinstance(path, str) and path and not path.startswith('/') and
                all(p not in ('', '.', '..', '.git') for p in path.split('/')) and path not in entries,
                'invalid or duplicate tree path')
        sha(row.get('sha'))
        require(row.get('type') in ('tree', 'blob', 'commit') and isinstance(row.get('mode'), str), 'invalid tree entry')
        entries[path] = {k: row[k] for k in ('mode', 'type', 'sha')}
    return {'sha': root, 'truncated': data['truncated'], 'entries': entries}


def classify(repository, before, after):
    require(before.get('sha') and after.get('sha'), 'missing exact tree identity')
    if repository not in REPOSITORIES or before['truncated'] or after['truncated']:
        return None
    changed = []
    for path in sorted(before['entries'].keys() | after['entries'].keys()):
        old, new = before['entries'].get(path), after['entries'].get(path)
        if old == new or (old and new and old['type'] == new['type'] == 'tree'):
            continue
        # New/deleted directories are accounted for by all their leaf entries.
        if (old is None and new['type'] == 'tree') or (new is None and old['type'] == 'tree'):
            continue
        if not allowed(repository, path) or any(row and (row['type'] != 'blob' or row['mode'] != '100644')
                                               for row in (old, new)):
            return None
        changed.append(path)
    return changed


def blob(repository, identity):
    row = api(f'repos/{repository}/git/blobs/{sha(identity)}')
    require(row.get('sha') == identity and row.get('encoding') == 'base64', 'missing exact documentation blob')
    data = base64.b64decode(row['content'], validate=False)
    require(hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() == identity,
            'documentation blob identity mismatch')
    return data


def check_tree(repository, head, changed):
    """Use the maintained checker on changed files and all reverse references.

    All tree paths are represented, but only Markdown and symlink text blobs
    are retrieved. No executable source, archive, assets or build environment.
    """
    with tempfile.TemporaryDirectory(prefix='docs-lane-') as temporary:
        root = Path(temporary).resolve()
        docs = []
        for name, row in sorted(head['entries'].items()):
            path = root / name
            if row['type'] == 'tree':
                path.mkdir(parents=True, exist_ok=True)
            elif row['type'] == 'blob' and row['mode'] != '120000':
                path.parent.mkdir(parents=True, exist_ok=True)
                document = name.lower().endswith('.md') and not any(p in check_docs.EXCLUDED for p in path.relative_to(root).parts)
                path.write_bytes(blob(repository, row['sha']) if document else b'')
                if document:
                    docs.append(path)
        # Create links last, never traverse them while writing materialized paths.
        for name, row in head['entries'].items():
            if row['mode'] == '120000':
                target = blob(repository, row['sha']).decode('utf-8')
                path = root / name
                if (path.parent / target).resolve().is_relative_to(root):
                    path.symlink_to(target)
                # Escaping links remain absent; the shared checker rejects any
                # reference without reading files outside this temporary tree.
        affected = {root / name for name in changed}
        selected = set(path for path in docs if path in affected)
        for path in docs:
            text = path.read_text(encoding='utf-8')
            for _, destination in check_docs.destinations(text):
                try:
                    url = urlsplit(destination)
                    target = (path.parent / unquote(url.path)).resolve() if url.path else path
                except (ValueError, RuntimeError):
                    selected.add(path)
                    continue
                if not url.scheme and not url.netloc and target in affected:
                    selected.add(path)
        findings = [issue for path in sorted(selected) for issue in check_docs.check_file(root, path)]
        return {'checked_files': [p.relative_to(root).as_posix() for p in sorted(selected)], 'findings': findings}


def qualify(records, policy_sha):
    result = {'schema': 1, 'lane': 'full', 'conclusion': 'failure', 'sources': records,
              'policy_sha': sha(policy_sha), 'run_id': os.environ['GITHUB_RUN_ID'],
              'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'], 'repositories': []}
    light = True
    trees = []
    for row in records:
        repository = row['repository']
        # Admission already verifies current PR refs and merge parents. Still
        # require all three exact commits/trees, never a truncated PR files API.
        before = tree(repository, row['base_sha'])
        after = tree(repository, row['candidate_sha'])
        head = tree(repository, row['head_sha'])
        changed = classify(repository, before, after)
        if head['truncated'] or classify(repository, before, head) is None:
            changed = None
        light = light and changed is not None
        result['repositories'].append({'repository': repository, 'base_tree': before['sha'],
                                       'candidate_tree': after['sha'], 'head_tree': head['sha'], 'changed': changed})
        trees.append(after)
    if not light or not any(row['changed'] for row in result['repositories']):
        result['conclusion'] = 'success'
        return result
    result['lane'] = 'docs'
    for row, head in zip(result['repositories'], trees):
        row.update(check_tree(row['repository'], head, row['changed']))
    result['conclusion'] = 'failure' if any(row['findings'] for row in result['repositories']) else 'success'
    return result


def verify(result, records, policy_sha):
    require(result.get('schema') == 1 and result.get('lane') == 'docs' and result.get('conclusion') == 'success',
            'missing successful docs evidence')
    require(result.get('sources') == records and result.get('policy_sha') == sha(policy_sha) and
            result.get('run_id') == os.environ['GITHUB_RUN_ID'] and
            result.get('run_attempt') == os.environ['GITHUB_RUN_ATTEMPT'], 'stale docs evidence identity')
    rows = result.get('repositories', [])
    require([row.get('repository') for row in rows] == [row['repository'] for row in records], 'incomplete docs evidence')
    require(any(row.get('changed') for row in rows), 'empty docs classification')
    for row in rows:
        for name in ('base_tree', 'candidate_tree', 'head_tree'):
            sha(row.get(name))
        require(isinstance(row.get('changed'), list) and all(allowed(row['repository'], p) for p in row['changed'])
                and row.get('findings') == [] and isinstance(row.get('checked_files'), list), 'invalid docs result')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--verify', type=Path)
    parser.add_argument('--policy-sha', required=True)
    args = parser.parse_args()
    records = request()
    if args.verify:
        verify(json.loads(args.verify.read_text()), records, args.policy_sha)
        return
    result = qualify(records, args.policy_sha)
    args.output.write_text(json.dumps(result, sort_keys=True) + '\n')
    if result['conclusion'] != 'success':
        raise ValueError('documentation checks failed: ' + json.dumps(result['repositories']))
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write('lane=' + result['lane'] + '\n')
        stream.write('artifact=integration-docs-' + os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT'] + '\n')


if __name__ == '__main__':
    main()
