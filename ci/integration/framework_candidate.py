"""Bind a task-owned push ref to the live, same-repository PR merge commit."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

PLATFORM = 'kuasar-sandbox/kuasar-sandbox'


def require(value, message):
    if not value:
        raise ValueError(message)


def resolve(repository, ref, sha, fetch):
    require(repository == PLATFORM, 'framework candidate requires the platform repository')
    match = re.fullmatch(r'refs/heads/ci/framework/pr-([1-9][0-9]*)/([0-9a-f]{40})', ref)
    require(match and re.fullmatch(r'[0-9a-f]{40}', sha), 'invalid framework candidate ref or SHA')
    number, head = match.groups()
    pr = fetch(f'repos/{repository}/pulls/{number}')
    require(pr['number'] == int(number) and pr['state'] == 'open' and pr['draft'] is False,
            'framework candidate requires an open Ready PR')
    require(pr['head']['repo']['full_name'] == repository and pr['base']['repo']['full_name'] == repository,
            'framework candidate must be a same-repository PR')
    require(pr['head']['sha'] == head and pr['merge_commit_sha'] == sha, 'framework candidate is stale')
    require(re.search(r'<!--\s*kuasar-(?:ci|bms)-companions\b', pr.get('body') or '') is None,
            'framework candidate cannot omit companions')
    base, base_ref = pr['base']['sha'], pr['base']['ref']
    require(re.fullmatch(r'[0-9a-f]{40}', base) and
            (base_ref == 'main' or re.fullmatch(r'release/v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.x', base_ref)),
            'invalid framework candidate base')
    commit = fetch(f'repos/{repository}/git/commits/{sha}')
    require(commit['sha'] == sha and [p['sha'] for p in commit['parents']] == [base, head],
            'framework candidate requires the ordered base/head merge parents')
    for name, expected in ((f'pull/{number}/merge', sha), (f'pull/{number}/head', head), (f'heads/{base_ref}', base)):
        current = fetch(f'repos/{repository}/git/ref/{name}')
        require(current['ref'] == 'refs/' + name and current['object']['sha'] == expected,
                'framework candidate refs changed')
    return {'repository': repository, 'pull_request_number': number, 'candidate_sha': sha,
            'base_sha': base, 'base_ref': base_ref, 'head_sha': head}


def main():
    require(os.environ['GITHUB_EVENT_NAME'] == 'push', 'framework candidate requires a push event')
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    repository, ref, sha = (os.environ[name] for name in ('GITHUB_REPOSITORY', 'GITHUB_REF', 'GITHUB_SHA'))
    require(event['repository']['full_name'] == repository and event['repository']['visibility'] == 'public'
            and event['ref'] == ref and event['after'] == sha and event['deleted'] is False,
            'framework candidate does not match its public push event')
    record = resolve(repository, ref, sha, lambda endpoint: json.loads(
        subprocess.check_output(['gh', 'api', endpoint], text=True)))
    if sys.argv[1:] == ['--verify-inputs']:
        names = {'repository': 'CANDIDATE_REPOSITORY', 'pull_request_number': 'CANDIDATE_PR',
                 'candidate_sha': 'CANDIDATE_SHA', 'base_sha': 'CANDIDATE_BASE_SHA',
                 'base_ref': 'CANDIDATE_BASE_REF', 'head_sha': 'CANDIDATE_HEAD_SHA'}
        require(all(record[key] == os.environ[name] for key, name in names.items()) and
                os.environ['COMPANION_CANDIDATES'] == '[]', 'framework candidate inputs changed')
    else:
        require(not sys.argv[1:], 'unknown framework candidate option')
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            for name, value in record.items():
                output.write(f'{name}={value}\n')
    print(json.dumps(record, sort_keys=True))


if __name__ == '__main__':
    main()
