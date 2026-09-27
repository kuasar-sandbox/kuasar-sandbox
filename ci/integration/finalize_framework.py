"""Publish a real platform PR pipeline result on its exact current merge commit."""
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
    require(repository == PLATFORM, 'framework result requires the platform repository')
    match = re.fullmatch(r'refs/pull/([1-9][0-9]*)/merge', ref)
    require(match and re.fullmatch(r'[0-9a-f]{40}', sha), 'invalid framework merge ref or SHA')
    number, = match.groups()
    pr = fetch(f'repos/{repository}/pulls/{number}')
    require(pr['number'] == int(number) and pr['state'] == 'open' and pr['draft'] is False,
            'framework result requires an open Ready PR')
    require(pr['head']['repo']['full_name'] == repository and pr['base']['repo']['full_name'] == repository,
            'framework result must be a same-repository PR')
    head = pr['head']['sha']
    require(re.fullmatch(r'[0-9a-f]{40}', head) and pr['merge_commit_sha'] == sha, 'framework result is stale')
    require(re.search(r'<!--\s*kuasar-(?:ci|bms)-companions\b', pr.get('body') or '') is None,
            'framework result cannot omit companions')
    base, base_ref = pr['base']['sha'], pr['base']['ref']
    require(re.fullmatch(r'[0-9a-f]{40}', base) and
            (base_ref == 'main' or re.fullmatch(r'release/v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.x', base_ref)),
            'invalid framework result base')
    commit = fetch(f'repos/{repository}/git/commits/{sha}')
    require(commit['sha'] == sha and [p['sha'] for p in commit['parents']] == [base, head],
            'framework result requires the ordered base/head merge parents')
    for name, expected in ((f'pull/{number}/merge', sha), (f'pull/{number}/head', head), (f'heads/{base_ref}', base)):
        current = fetch(f'repos/{repository}/git/ref/{name}')
        require(current['ref'] == 'refs/' + name and current['object']['sha'] == expected,
                'framework result refs changed')
    return {'repository': repository, 'pull_request_number': number, 'candidate_sha': sha,
            'base_sha': base, 'base_ref': base_ref, 'head_sha': head}


def main():
    require(not sys.argv[1:], 'unknown framework finalization option')
    require(os.environ['GITHUB_EVENT_NAME'] == 'pull_request', 'framework result requires a PR event')
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    repository, ref, sha = (os.environ[name] for name in ('GITHUB_REPOSITORY', 'GITHUB_REF', 'GITHUB_SHA'))
    require(event['repository']['full_name'] == repository and event['repository']['visibility'] == 'public',
            'framework result requires its public repository event')
    record = resolve(repository, ref, sha, lambda endpoint: json.loads(
        subprocess.check_output(['gh', 'api', endpoint], text=True)))
    pr = event['pull_request']
    require(pr['number'] == int(record['pull_request_number']) and pr['state'] == 'open' and
            pr['draft'] is False and pr['head']['repo']['full_name'] == repository and
            pr['base']['repo']['full_name'] == repository and pr['head']['sha'] == record['head_sha'] and
            pr['base']['sha'] == record['base_sha'] and pr['base']['ref'] == record['base_ref'],
            'framework result differs from the tested PR event')
    conclusion = os.environ['INTEGRATION_RESULT']
    require(conclusion in ('success', 'failure', 'cancelled', 'skipped'), 'missing full pipeline result')
    run = os.environ['GITHUB_RUN_ID']
    require(re.fullmatch(r'[1-9][0-9]*', run), 'invalid framework run identity')
    state = 'success' if conclusion == 'success' else 'failure'
    status = {'state': state, 'context': 'ci / finalize',
              'description': 'Full framework pipeline: ' + conclusion,
              'target_url': f'https://github.com/{repository}/actions/runs/{run}'}
    subprocess.run(['gh', 'api', '--method', 'POST', f'repos/{repository}/statuses/{sha}', '--input', '-'],
                   input=json.dumps(status), text=True, check=True, stdout=subprocess.PIPE)
    print(json.dumps(record | {'run_id': run, 'state': state}, sort_keys=True))
    return 0 if state == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())
