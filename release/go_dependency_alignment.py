"""Align the exact selected Go module sources before a new release is frozen."""
import base64
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ci/integration'))
import go_module_policy as policy

TITLE = 'release: align internal Go module versions'
CHECK = 'go-selection-check.yml'


def selected_files(root, units):
    expected = policy.expected_versions(units)
    for unit in units:
        if unit != 'vmlinux':
            path = root / unit / 'go.mod'
            if path.is_symlink() or not path.is_file():
                raise ValueError(f'{unit}: missing regular go.mod')
            validate(unit, path.read_text(), expected)


def validate(unit, text, expected):
    delta = policy.declaration_changes(text, expected)
    if delta:
        detail = ', '.join(f'{name}: {old} -> {new}' for name, (old, new) in sorted(delta.items()))
        raise ValueError(f'{unit} go.mod differs from the release selection: {detail}')


@functools.lru_cache(maxsize=128)
def source_files(c, repo, sha):
    tree = c.api(f'repos/{repo}/git/trees/{sha}')
    if tree['truncated']:
        raise ValueError(f'incomplete tree for {repo}@{sha}')
    return {row['path']: (row['mode'], row['sha']) for row in tree['tree']}


@functools.lru_cache(maxsize=512)
def read_file(c, repo, sha, filename, *, optional=False):
    entry = source_files(c, repo, sha).get(filename)
    if entry is None and optional:
        return ''
    if not entry or entry[0] not in {'100644', '100755'}:
        raise ValueError(f'{repo}@{sha}: missing regular {filename}')
    data = c.api(f'repos/{repo}/git/blobs/{entry[1]}')
    if data['sha'] != entry[1] or data.get('encoding') != 'base64':
        raise ValueError(f'{repo}@{sha}: unexpected {filename} content')
    return base64.b64decode(data['content'].replace('\n', ''), validate=True).decode()


@functools.lru_cache(maxsize=64)
def module_checksums(module, version):
    # Never execute the candidate's code or honor its replace/toolchain settings.
    # Ask the Go client for the actual public module, in an empty directory.
    with tempfile.TemporaryDirectory(prefix='release-go-sums-') as directory:
        env = {key: os.environ[key] for key in ('HOME', 'PATH', 'GOCACHE', 'GOMODCACHE', 'TMPDIR',
                                                'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY') if key in os.environ}
        env.update(GOENV='off', GOWORK='off', GO111MODULE='on', GOSUMDB='sum.golang.org')
        proc = subprocess.run(['go', 'mod', 'download', '-json', module + '@' + version], cwd=directory,
                              env=env, text=True, capture_output=True, timeout=180)
        if proc.returncode:
            raise ValueError(f'cannot download checksums for {module}@{version}: {proc.stderr or proc.stdout}')
        result = json.loads(proc.stdout)
    if result.get('Path') != module or result.get('Version') != version:
        raise ValueError(f'Go resolved another module for {module}@{version}')
    fields = {version: result.get('Sum'), version + '/go.mod': result.get('GoModSum')}
    if any(not isinstance(v, str) or not re.fullmatch(r'h1:[A-Za-z0-9+/]{43}=', v) for v in fields.values()):
        raise ValueError(f'incomplete checksums for {module}@{version}')
    return {f'{module} {ver} {digest}' for ver, digest in fields.items()}


def update_sums(current, stable):
    existing = {line for line in current.splitlines() if line.strip()}
    by_version = {row.rsplit(' ', 1)[0]: row for row in existing}
    additions = set()
    for module, tag in stable.items():
        keys = (f'{module} {tag}', f'{module} {tag}/go.mod')
        if all(key in by_version for key in keys):
            continue
        for row in module_checksums(module, tag):
            key = row.rsplit(' ', 1)[0]
            if key in by_version and by_version[key] != row:
                raise ValueError(f'go.sum conflicts with the published checksum for {key}')
            additions.add(row)
    if not additions - existing:
        return current
    # Insert Stable sums in Go's module/core order without changing unrelated lines.
    def key(line):
        module, version, _ = line.split()
        version, _, manifest = version.partition('/')
        match = re.match(r'^v([0-9]+)\.([0-9]+)\.([0-9]+)(.*)$', version)
        if not match:
            raise ValueError(f'invalid go.sum version for {module}: {version}')
        return (module, *(int(n) for n in match.groups()[:3]),
                not match[4].startswith('-'), match[4], manifest)
    lines = current.splitlines()
    for row in sorted(additions - existing, key=key):
        insert = next((i for i, old in enumerate(lines) if old.strip() and key(old) > key(row)), len(lines))
        lines.insert(insert, row)
    return '\n'.join(lines) + '\n'


def writer(endpoint, method, payload):
    token = os.environ.get('GO_ALIGNMENT_TOKEN')
    if not token:
        raise RuntimeError('missing component Go alignment writer token')
    proc = subprocess.run(['gh', 'api', '--method', method, endpoint, '--input', '-'],
                          env={**os.environ, 'GH_TOKEN': token}, input=json.dumps(payload),
                          text=True, capture_output=True)
    if proc.returncode:
        raise RuntimeError(f'{method} {endpoint}: {proc.stderr.strip()}')
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def exact_combination(c, plan, pr, version, dependencies):
    head, base, number = pr['head']['sha'], pr['base']['sha'], pr['number']
    identity = [plan.unit.name, number, head, base, version, dependencies]
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    repo = c.PLATFORM_REPOSITORY
    trusted = c.branch_sha(repo, 'main')
    endpoint = f'repos/{repo}/actions/workflows/{CHECK}/runs?' + urlencode({
        'event': 'workflow_dispatch', 'branch': 'main', 'head_sha': trusted, 'per_page': 100})
    runs = [r for r in c.paginated(endpoint, 'workflow_runs') if r.get('display_title') == 'Go selection ' + digest
            and r.get('head_sha') == trusted and r.get('head_branch') == 'main'
            and r.get('head_repository', {}).get('full_name') == repo]
    latest = max(runs, key=lambda r: r['id'], default=None)
    if latest is None:
        c.gh('workflow', 'run', CHECK, '--repo', repo, '--ref', 'main',
             '-f', 'consumer=' + plan.unit.name, '-f', 'pr_number=' + str(number),
             '-f', 'head_sha=' + head, '-f', 'base_sha=' + base, '-f', 'version=' + version,
             '-f', 'dependencies=' + json.dumps(dependencies, sort_keys=True), '-f', 'binding=' + digest)
        raise c.Pending(f'dispatched exact dependency check for {pr["html_url"]}')
    if latest['status'] != 'completed':
        raise c.Pending(f'waiting for {latest["html_url"]}')
    if latest.get('conclusion') != 'success':
        raise c.Deferred(f'exact dependency check failed: {latest["html_url"]}')
    return trusted


def normal_ci(c, repo, pr):
    # The CI App has Actions read/write but no Checks API grant. Query the
    # actual component PR workflow and its exact finalize job with Actions.
    head, base = pr['head']['sha'], pr['base']['sha']
    path = f'repos/{repo}/actions/workflows/integration-tests.yml/runs?' + urlencode({
        'event': 'pull_request_target', 'head_sha': head, 'per_page': 100})
    runs = [r for r in c.paginated(path, 'workflow_runs')
            if r['head_sha'] == head and r['event'] == 'pull_request_target'
            and r.get('head_repository', {}).get('full_name') == repo
            and any(p['number'] == pr['number'] and p['head']['sha'] == head and p['base']['sha'] == base
                    for p in r['pull_requests'])]
    run = max(runs, key=lambda r: r['id'], default=None)
    if run is None or run.get('status') != 'completed':
        raise c.Pending(f'waiting for the normal PR workflow on {pr["html_url"]}')
    if run.get('conclusion') != 'success':
        raise c.Deferred(f'PR CI failed: {run["html_url"]}')
    jobs = c.paginated(f'repos/{repo}/actions/runs/{run["id"]}/jobs?filter=all&per_page=100', 'jobs')
    finalize = c.latest_actual_jobs(jobs).get('ci / finalize', {})
    if (finalize.get('head_sha') != head or finalize.get('status') != 'completed'
            or finalize.get('conclusion') != 'success'):
        raise c.Deferred(f'missing successful ci / finalize for {head}: {run["html_url"]}')


def reviews_resolved(c, repo, number):
    owner, name = repo.split('/')
    query = """query($owner:String!,$name:String!,$number:Int!,$after:String) {
      repository(owner:$owner,name:$name) { pullRequest(number:$number) {
        reviewDecision reviewThreads(first:100, after:$after) {
          nodes { isResolved } pageInfo { hasNextPage endCursor }
        }
      }}
    }"""
    args = ['api', 'graphql', '-f', 'query=' + query, '-f', 'owner=' + owner,
            '-f', 'name=' + name, '-F', 'number=' + str(number)]
    cursor = []
    while True:
        result = json.loads(c.gh(*args, *cursor).stdout)['data']['repository']['pullRequest']
        threads = result['reviewThreads']
        if (result['reviewDecision'] in {'CHANGES_REQUESTED', 'REVIEW_REQUIRED'}
                or any(not node['isResolved'] for node in threads['nodes'])):
            return False
        page = threads['pageInfo']
        if not page['hasNextPage']:
            return True
        cursor = ['-f', 'after=' + page['endCursor']]


def ensure_pr(c, plan, base, files, version, dependencies, confirm):
    repo, ref = plan.unit.repository, plan.source_ref
    endpoint = 'repos/' + repo
    bot = os.environ.get('GO_ALIGNMENT_BOT', '')
    if not os.environ.get('GO_ALIGNMENT_TOKEN') or not re.fullmatch(r'[A-Za-z0-9_-]+\[bot\]', bot):
        raise c.Deferred(f'{repo}: cannot align go.mod without the component writer token; grant the CI App '
                         'Pull requests: Read and write, approve the installation change and rerun Daily')
    branch = 'automation/align-go/' + ref
    if c.PLATFORM_REF != ref:
        branch += '/for-' + c.PLATFORM_REF.replace('/', '-')
    pulls = c.paginated(endpoint + '/pulls?' + urlencode({
        'head': repo.split('/')[0] + ':' + branch, 'base': ref, 'state': 'open', 'per_page': 100}))
    if len(pulls) > 1:
        raise c.Deferred(f'multiple alignment PRs for {repo}:{ref}')
    pr = pulls[0] if pulls else None
    if pr and (pr['user']['login'] != bot or pr['head']['repo']['full_name'] != repo):
        raise c.Deferred(f'{repo}:{branch} is owned by another contributor')
    old = c.branch_sha(repo, branch)
    if old:
        commit = c.api(endpoint + '/commits/' + old)
        if ((commit.get('author') or {}).get('login') != bot or commit['commit']['message'].splitlines()[0] != TITLE):
            raise c.Deferred(f'{repo}:{branch} contains a commit outside the alignment workflow')
    desired = source_files(c, repo, base).copy()
    edits = []
    for name, value in files.items():
        mode = desired.get(name, ('100644', None))[0]
        encoded = value.encode()
        blob = hashlib.sha1(b'blob ' + str(len(encoded)).encode() + b'\0' + encoded).hexdigest()
        desired[name] = (mode, blob)
        edits.append({'path': name, 'mode': mode, 'type': 'blob', 'content': value})
    if not old or commit['parents'][0]['sha'] != base or source_files(c, repo, old) != desired:
        base_tree = c.api(endpoint + '/git/commits/' + base)['tree']['sha']
        tree = writer(endpoint + '/git/trees', 'POST', {'base_tree': base_tree, 'tree': edits})['sha']
        parents = [base] if not old or old == base else [base, old]
        new = writer(endpoint + '/git/commits', 'POST', {'message': TITLE, 'tree': tree, 'parents': parents})
        if c.branch_sha(repo, ref) != base or c.branch_sha(repo, branch) != old:
            raise c.Pending(f'{repo} changed while preparing the alignment')
        if old:
            writer(endpoint + '/git/refs/heads/' + branch, 'PATCH', {'sha': new['sha'], 'force': False})
        else:
            writer(endpoint + '/git/refs', 'POST', {'ref': 'refs/heads/' + branch, 'sha': new['sha']})
        old = new['sha']
    if not pr:
        created = writer(endpoint + '/pulls', 'POST', {
            'head': branch, 'base': ref, 'title': TITLE,
            'body': 'Align the internal Go dependencies with the maintained platform selection. '
                    'The daily release controller rechecks the selected commits and normal PR checks before merging.'})
        raise c.Pending(f'opened {created["html_url"]}')
    pr = c.api(endpoint + '/pulls/' + str(pr['number']))
    if (pr['user']['login'] != bot or pr['head']['sha'] != old or pr['base']['sha'] != base
            or pr['head']['ref'] != branch or pr['base']['ref'] != ref or pr['state'] != 'open'):
        raise c.Pending(f'{repo}: rescan changed PR or branch')
    trusted = exact_combination(c, plan, pr, version, dependencies)
    normal_ci(c, repo, pr)
    if (pr.get('mergeable_state') != 'clean' or pr.get('mergeable') is not True
            or not reviews_resolved(c, repo, pr['number'])):
        raise c.Pending(f'waiting for required checks and reviews on {pr["html_url"]}')
    if (c.api(endpoint + '/branches/' + c.quote(ref, safe='')).get('protected') is not True
            or c.branch_sha(repo, ref) != base or c.branch_sha(c.PLATFORM_REPOSITORY, 'main') != trusted):
        raise c.Deferred(f'cannot confirm protected, unchanged base for {pr["html_url"]}')
    confirm()
    # The workflow status is computed on the exact integration commit. Use ordinary
    # merge with an expected head and let GitHub enforce every branch/review rule.
    result = writer(endpoint + '/pulls/' + str(pr['number']) + '/merge', 'PUT',
                    {'sha': old, 'merge_method': 'squash'})
    if result.get('merged') is not True:
        raise c.Pending(f'merge pending: {pr["html_url"]}')
    raise c.Pending(f'merged {pr["html_url"]}; rescan actual component and downstream SHAs')


def confirm_inputs(c, plans):
    for plan in plans.values():
        if plan.action == 'publish' and c.branch_sha(plan.unit.repository, plan.source_ref) != plan.source_sha:
            raise c.Pending(f'{plan.unit.name}: component changed during selection; rescan')
        if plan.action != 'publish' and c.tag_sha(plan.unit.repository, plan.selected) != plan.source_sha:
            raise c.Deferred(f'{plan.unit.name}: selected tag moved during planning')


def check_plans(c, plans, next_versions=None, *, date=None, before_alignment=None):
    tags = {name: plan.selected for name, plan in plans.items()}
    expected = policy.expected_versions(tags, next_versions)
    for unit, plan in plans.items():
        if unit == 'vmlinux':
            continue
        source = read_file(c, plan.unit.repository, plan.source_sha, 'go.mod')
        if date is None:
            validate(unit, source, expected)
            continue
        updated = policy.align_declarations(source, expected)
        managed = policy.managed_requires(updated)
        if not managed and source == updated:
            continue
        relevant = {name: {'tag': other.selected, 'sha': other.source_sha} for name, other in plans.items()
                    if name in plan.unit.dependencies and (name != 'sandboxer' or unit != 'runtime')}
        # Compute checksums for real Stable tags only. An unpublished future core
        # has no canonical content; its eventual Stable producer supplies it.
        stable = {path: ver for path, ver in managed.items()
                  if any(item['tag'] == ver and path == 'github.com/kuasar-sandbox/' + name
                         for name, item in relevant.items())}
        old_sum = read_file(c, plan.unit.repository, plan.source_sha, 'go.sum', optional=True)
        new_sum = update_sums(old_sum, stable)
        if source == updated and old_sum == new_sum:
            continue
        if not plan.source_ref:
            raise c.Deferred(f'{unit} {plan.selected} needs a new aligned source version; its maintenance branch is missing')
        if before_alignment:
            before_alignment()
        base = plan.source_head or plan.source_sha
        if c.branch_sha(plan.unit.repository, plan.source_ref) != base:
            raise c.Pending(f'{unit}: source moved before Go alignment')
        base_mod = read_file(c, plan.unit.repository, base, 'go.mod')
        base_sum = read_file(c, plan.unit.repository, base, 'go.sum', optional=True)
        updated = policy.align_declarations(base_mod, expected)
        new_sum = update_sums(base_sum, stable)
        if updated == base_mod and new_sum == base_sum:
            raise c.Deferred(f'{unit} {plan.selected} is stale; select a fresh consumer release from {base}')
        changed = {}
        if updated != base_mod:
            changed['go.mod'] = updated
        if new_sum != base_sum:
            changed['go.sum'] = new_sum
        version = c.preview_selection.preview_candidate(unit, plan.winner, date, (next_versions or {}).get(unit),
                                                         plan.source_ref)
        if c.RepositoryState(plan.unit).status(version).partial:
            raise c.Deferred(f'{unit} {version} already has a tag or release; select a fresh Preview revision')
        def confirm():
            if c.branch_sha(c.PLATFORM_REPOSITORY, c.PLATFORM_REF) != c.PLATFORM_SHA:
                raise c.Pending('platform moved before Go alignment; rescan the new selection')
            confirm_inputs(c, plans)
        confirm()
        ensure_pr(c, plan, base, changed, version, relevant, confirm)



if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('usage: go_dependency_alignment.py SELECTION.tsv SOURCES')
    selected_files(Path(sys.argv[2]), dict(line.split('\t') for line in Path(sys.argv[1]).read_text().splitlines()))
