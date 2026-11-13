#!/usr/bin/env python3
"""Compile the Daily candidate with its exact upstreams before merging its PR."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import tag_sources

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ci/integration'))
import go_workspace
import go_module_policy as policy


PACKAGES = {'sandboxer': './cmd/sandbox-ctl', 'orchestrator': './cmd/node-ctl',
            'runtime': './cmd/flatten-ctl'}


def checkout(root, unit, ref):
    subprocess.run(['git', 'init', '-q', '--template=', str(root)], check=True)
    tag_sources.git(root, 'remote', 'add', 'origin', 'https://github.com/' + tag_sources.repository(unit) + '.git')
    tag_sources.git(root, 'fetch', '-q', '--depth=1', '--no-tags', 'origin', ref)
    tag_sources.git(root, 'checkout', '-q', '--detach', 'FETCH_HEAD')


def check(inputs, sources):
    consumer = inputs['consumer']
    if consumer not in PACKAGES:
        raise ValueError('invalid Go consumer')
    owner = policy.UNITS[consumer]
    pr = int(inputs['pr_number'])
    head, base = inputs['head_sha'], inputs['base_sha']
    if pr <= 0 or not all(re.fullmatch(r'[0-9a-f]{40}', sha) for sha in (base, head)):
        raise ValueError('invalid PR or candidate identity')
    deps = json.loads(inputs['dependencies'])
    expected = {policy.OWNER_UNITS[name] for name in go_workspace.closure([owner]) if name != owner}
    # Old selected Host sources predate the runtime edge. Check the fixed source
    # declaration after admitting the merge; still reject all unknown keys here.
    allowed = [expected]
    if consumer in ('sandboxer', 'orchestrator'):
        allowed.append(expected - {'runtime'})
    if set(deps) not in allowed:
        raise ValueError('selection differs from the consumer module closure')
    version = inputs['version']
    if '-preview.' not in version:
        raise ValueError('Daily candidate check requires a Preview version')
    data = json.dumps([consumer, pr, head, base, version, deps], sort_keys=True, separators=(',', ':')).encode()
    if hashlib.sha256(data).hexdigest() != inputs['binding']:
        raise ValueError('selection digest changed')
    checkout(sources / owner, consumer, f'refs/pull/{pr}/merge')
    # cat-file reports both actual parents even for a depth-one fetch.
    commit = tag_sources.git(sources / owner, 'cat-file', '-p', 'HEAD').split('\n\n', 1)[0]
    parents = [line[7:] for line in commit.splitlines() if line.startswith('parent ')]
    if parents != [base, head]:
        raise ValueError('candidate moved after dependency selection')
    for unit, row in deps.items():
        directory = sources / policy.UNITS[unit]
        tag_sources.validate_tag(unit, row['tag'])
        if not re.fullmatch(r'[0-9a-f]{40}', row['sha']):
            raise ValueError(f'invalid selected {unit} SHA')
        if '-preview.' in row['tag']:
            # An upstream Preview name may be selected before its tag exists.
            checkout(directory, unit, row['sha'])
        else:
            tag_sources.fetch_unit(directory, unit, row['tag'], row['sha'])
    env = go_workspace.configure_bound_release(sources, owner, version, policy.owner_selection(deps))
    subprocess.run(['go', 'build', '-o', str(sources / 'compiled'), PACKAGES[consumer]],
                   cwd=sources / owner, env=env, check=True)
    if consumer == 'runtime' and (sources / owner / 'cmd/sandbox-init/main.go').is_file():
        subprocess.run(['go', 'build', '-o', str(sources / 'compiled-init'), './cmd/sandbox-init'],
                       cwd=sources / owner, env=env, check=True)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='go-selection-') as directory:
        check(json.loads(Path(sys.argv[1]).read_text())['inputs'], Path(directory))
