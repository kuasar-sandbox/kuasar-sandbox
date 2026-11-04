"""Trusted syntax checks for CI/release-only changes; never execute candidate code."""
import importlib.util
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile

PLATFORM = 'kuasar-sandbox/kuasar-sandbox'
CUTOVER = 'ci/integration/dependency-cutovers.json'
SCRIPT_SUFFIXES = {'.py', '.sh'}
COMPONENT_SCRIPTS = {
    'scripts/build-e2e-helpers.sh', 'scripts/publish-release.sh', 'scripts/delete-preview.sh',
    'scripts/validate-preview-line.sh', 'scripts/validate-release-source.sh',
    'scripts/test-preview-line.sh', 'scripts/test-delete-preview.sh', 'scripts/test-publisher.sh',
    'scripts/test-environment-tools.py', 'scripts/test-selected-go-materials.py',
}
PLATFORM_PYTHON = {'test/e2e/e2e'}


def platform_harness(path):
    return (path in PLATFORM_PYTHON or path.startswith('test/e2e/lib/') and path.endswith('.py')
            or path.startswith('test/e2e/test_') and path.count('/') == 2 and path.endswith('.py'))


PLATFORM_SHELLS = {'ci/runner/kuasar-ci-bpf', 'ci/runner/kuasar-ci-network'}


def allowed(repository, path):
    parts = path.split('/')
    if not path or path.startswith('/') or any(p in ('', '.', '..', '.git') for p in parts):
        return False
    suffix = PurePosixPath(path).suffix
    if repository == PLATFORM and (path == CUTOVER or platform_harness(path)):
        return True
    if repository == PLATFORM and path in PLATFORM_SHELLS:
        return True
    if path.startswith(('.github/workflows/', '.github/actions/')):
        return suffix in {'.yml', '.yaml', '.js', '.mjs', '.cjs'}
    if path.startswith(('ci/', 'release/', 'test/release/')):
        return suffix in SCRIPT_SUFFIXES
    if path in COMPONENT_SCRIPTS:
        return True
    return (path.count('/') == 1 and path.startswith(('scripts/ci-', 'scripts/release', 'scripts/test-release'))
            and suffix in SCRIPT_SUFFIXES | {'.go'})


def run_parser(command, data):
    # Candidate-controlled names, cwd, rc files and preload variables never
    # become parser inputs. These commands parse only; none executes payloads.
    with tempfile.TemporaryDirectory(prefix='tooling-syntax-') as temporary:
        path = Path(temporary) / ('candidate' + command[-1])
        path.write_bytes(data)
        env = {name: os.environ[name] for name in ('PATH', 'SYSTEMROOT') if name in os.environ}
        env.update(HOME=temporary, LC_ALL='C.UTF-8')
        result = subprocess.run([*command[:-1], str(path)], cwd=temporary, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        if result.returncode:
            raise ValueError((result.stderr or result.stdout).decode('utf-8', errors='replace')[:2000])


def parser_for(repository, path):
    if repository == PLATFORM and path in PLATFORM_PYTHON:
        return 'python-compile'
    if repository == PLATFORM and path == CUTOVER:
        return 'dependency-cutover-schema'
    if repository == PLATFORM and path in PLATFORM_SHELLS:
        return 'bash-syntax'
    return {'.py': 'python-compile', '.sh': 'bash-syntax', '.js': 'node-syntax',
            '.mjs': 'node-syntax', '.cjs': 'node-syntax', '.go': 'go-syntax',
            '.yml': 'yaml-syntax', '.yaml': 'yaml-syntax'}.get(PurePosixPath(path).suffix)


def check_blob(repository, path, data):
    suffix = PurePosixPath(path).suffix
    if suffix == '.py' or repository == PLATFORM and path in PLATFORM_PYTHON:
        # compile(), unlike ast.parse(), also rejects top-level return/break.
        # The code object is discarded. There is no import, exec or .pyc write.
        compile(data, path, 'exec', dont_inherit=True)
        return 'python-compile'
    if suffix == '.sh' or repository == PLATFORM and path in PLATFORM_SHELLS:
        run_parser(['bash', '--noprofile', '--norc', '-n', '.sh'], data)
        return 'bash-syntax'
    if suffix in {'.js', '.mjs', '.cjs'}:
        run_parser(['node', '--check', suffix], data)
        return 'node-syntax'
    if suffix == '.go':
        run_parser(['gofmt', '-e', '.go'], data)
        return 'go-syntax'
    if suffix in {'.yml', '.yaml'}:
        import yaml
        try:
            documents = list(yaml.safe_load_all(data))
        except yaml.YAMLError as error:
            raise ValueError(str(error)) from error
        if len(documents) != 1 or not isinstance(documents[0], dict):
            raise ValueError('workflow/action must be one YAML mapping')
        document = documents[0]
        if path.startswith('.github/workflows/'):
            jobs = document.get('jobs')
            if not isinstance(jobs, dict) or not jobs or not all(isinstance(job, dict) for job in jobs.values()):
                raise ValueError('workflow must declare jobs')
        else:
            runs = document.get('runs')
            if not isinstance(runs, dict) or not isinstance(runs.get('using'), str):
                raise ValueError('action must declare runs.using')
        return 'yaml-syntax'
    if repository == PLATFORM and path == CUTOVER:
        # Reuse the trusted schema/identity parser without querying publications
        # or admitting a dependency. PR number zero cannot match a real request.
        spec = importlib.util.spec_from_file_location('tooling_cutover', Path(__file__).with_name('resolve-artifacts.py'))
        resolver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(resolver)
        resolver.cutover_request(data.decode('utf-8'), [dict(repository=PLATFORM, pull_request_number=0)])
        return 'dependency-cutover-schema'
    raise ValueError('missing trusted tooling parser: ' + path)


def check_tree(repository, tree, changed, read_blob):
    checks, findings = [], []
    for path in changed:
        if not allowed(repository, path):
            continue
        row = tree['entries'].get(path)
        if row is None:
            checks.append(dict(path=path, state='deleted'))
            continue
        try:
            parser = check_blob(repository, path, read_blob(repository, row['sha']))
            checks.append(dict(path=path, state='success', parser=parser, sha=row['sha']))
        except (OSError, ValueError, SyntaxError, UnicodeError,
                subprocess.TimeoutExpired) as error:
            findings.append(path + ': ' + str(error))
    return dict(checks=checks, findings=findings)


def verify(evidence, changed, repository):
    expected = [path for path in changed if allowed(repository, path)]
    if not isinstance(evidence, dict) or evidence.get('findings') != []:
        raise ValueError('missing successful tooling syntax evidence')
    checks = evidence.get('checks')
    if not isinstance(checks, list) or [row.get('path') for row in checks] != expected:
        raise ValueError('incomplete tooling syntax evidence')
    for row in checks:
        if row.get('state') == 'deleted' and set(row) == {'path', 'state'}:
            continue
        if (set(row) != {'path', 'state', 'parser', 'sha'} or row.get('state') != 'success'
                or row.get('parser') != parser_for(repository, row['path'])
                or not isinstance(row.get('sha'), str) or len(row['sha']) != 40
                or any(c not in '0123456789abcdef' for c in row['sha'])):
            raise ValueError('invalid tooling syntax evidence')
