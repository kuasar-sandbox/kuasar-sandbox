#!/usr/bin/env python3
"""Create exact reviewed Git objects; never change a branch, setting, PR or release."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zlib


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def check_path(value: str) -> str:
    p = PurePosixPath(value)
    if (not value or p.is_absolute() or str(p) != value or ".." in p.parts
            or any(part.startswith('.') for part in p.parts)
            and value != '.github/PULL_REQUEST_TEMPLATE.md'
            or not (value.endswith(('.md', '.sh', '.go')) or value == 'go.mod')):
        raise ValueError('unapproved output path: ' + value)
    return value


def git(*args: str) -> bytes:
    return subprocess.check_output(['git', *args], stderr=subprocess.PIPE)


def assemble(plan: dict, load):
    seen, outputs, entries = set(), {}, []
    for item in plan['files']:
        path = check_path(item['p'])
        if path in seen:
            raise ValueError('duplicate path')
        seen.add(path)
        prior_mode, prior = load(path)
        before = item['b']
        if before is None:
            if prior is not None:
                raise ValueError('expected absent path: ' + path)
        elif prior is None or blob_sha(prior) != before:
            raise ValueError('source blob mismatch: ' + path)
        mode = item['m']
        if mode not in ('100644', '100755') or prior_mode and prior_mode != mode:
            raise ValueError('file-mode change: ' + path)
        if item.get('delete'):
            if prior is None:
                raise ValueError('absent deletion')
            outputs[path] = None
            entries.append({'path': path, 'mode': mode, 'type': 'blob', 'sha': None})
            continue
        parts = []
        for op in item['ops']:
            if isinstance(op, str):
                parts.append(op.encode('utf-8'))
            elif isinstance(op, list) and len(op) == 2 and all(type(x) is int for x in op):
                start, size = op
                if prior is None or start < 0 or size < 0 or start + size > len(prior):
                    raise ValueError('invalid source range')
                parts.append(prior[start:start + size])
            else:
                raise ValueError('invalid source operation')
        data = b''.join(parts)
        data.decode('utf-8')
        if b'\0' in data or len(data) > 2000000 or blob_sha(data) != item['h']:
            raise ValueError('reviewed output mismatch: ' + path)
        outputs[path] = data
        entries.append({'path': path, 'mode': mode, 'type': 'blob', 'sha': item['h']})
    if not entries:
        raise ValueError('empty plan')
    return outputs, entries


def request(repo: str, endpoint: str, payload: dict) -> dict:
    req = Request('https://api.github.com/repos/' + repo + '/git/' + endpoint,
                  data=json.dumps(payload).encode(), method='POST', headers={
                      'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                      'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                      'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'kuasar-reviewed-objects'})
    for attempt in range(4):
        try:
            with urlopen(req, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code not in (429, 502, 503, 504) or attempt == 3:
                raise RuntimeError('Git-object request failed: HTTP ' + str(exc.code)) from None
            time.sleep(2 ** attempt)
    raise RuntimeError('unreachable')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--plan', type=Path, required=True)
    ap.add_argument('--expected-plan-sha256', required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    encoded = args.plan.read_bytes()
    if len(encoded) > 1000000:
        raise ValueError('oversized plan')
    decompressor = zlib.decompressobj()
    raw = decompressor.decompress(base64.b64decode(encoded), 2000000)
    if not decompressor.eof or decompressor.unconsumed_tail or decompressor.unused_data:
        raise ValueError('invalid compressed plan')
    if hashlib.sha256(raw).hexdigest() != args.expected_plan_sha256:
        raise ValueError('plan transport checksum mismatch')
    plan = json.loads(raw)
    repo, parent = plan['repository'], plan['parent']
    if repo != os.environ['GITHUB_REPOSITORY'] or not re.fullmatch(r'kuasar-sandbox/[A-Za-z0-9_.-]+', repo):
        raise ValueError('wrong destination')
    if not all(re.fullmatch(r'[0-9a-f]{40}', plan[k]) for k in ('parent', 'base_tree', 'tree')):
        raise ValueError('exact parent and trees required')
    if git('rev-parse', parent + '^{tree}').decode().strip() != plan['base_tree']:
        raise ValueError('parent tree mismatch')
    def load(path):
        entry = git('ls-tree', parent, '--', path).decode().strip().split()
        if not entry:
            return None, None
        if entry[0] not in ('100644', '100755') or entry[1] != 'blob':
            raise ValueError('nonregular source: ' + path)
        return entry[0], git('show', parent + ':' + path)
    outputs, entries = assemble(plan, load)
    for entry in entries:
        data = outputs[entry['path']]
        if data is None:
            continue
        obj = request(repo, 'blobs', {'content': base64.b64encode(data).decode(), 'encoding': 'base64'})
        if obj['sha'] != entry['sha']:
            raise ValueError('remote blob mismatch')
    tree = request(repo, 'trees', {'base_tree': plan['base_tree'], 'tree': entries})
    if tree['sha'] != plan['tree']:
        raise ValueError('full reviewed tree mismatch; no commit created')
    commit = request(repo, 'commits', {'message': plan['message'], 'tree': tree['sha'], 'parents': [parent]})
    result = {'repository': repo, 'parent': parent, 'tree': tree['sha'], 'commit': commit['sha'],
              'plan_sha256': args.expected_plan_sha256, 'entries': entries, 'refs_modified': False,
              'validation': 'exact input/output/tree only; normal tests and PR review remain required'}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write('sha=' + commit['sha'] + '\n')

if __name__ == '__main__':
    main()
