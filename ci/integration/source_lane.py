"""Classify source tests and documentation delivery without executing candidates.

The source lane still executes the admitted tests in the private Workbench.
These predicates only decide whether product E2E is part of that qualification.
"""
from functools import cache
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile

import tooling_lane

PLATFORM = 'kuasar-sandbox/kuasar-sandbox'
OWNERS = ('platform', 'accelerator', 'connector', 'guest-runtime', 'orchestrator', 'sandboxer')
REPOSITORIES = {PLATFORM if owner == 'platform' else 'kuasar-sandbox/' + owner for owner in OWNERS}
GUIDE_DECLARATIONS = {'release/guide-inputs.txt', 'release/inputs/guide-inputs.yaml',
                      'releases/guide-inputs.yaml'}
MIGRATIONS = 'release/guide-migrations.json'
DOCUMENTATION_TOOLS = {'test/e2e/assemble_docs.py', 'test/e2e/package_inputs.py',
                       'ci/check_docs.py', 'ci/integration/check-published-download.py',
                       'ci/integration/check-source-documentation.py',
                       'release/package-platform.sh'}
# These are fixture helpers, not no-argument entry points. Execute their
# maintained parent suite so the helper receives its real fixture arguments.
TEST_DRIVERS = {
    (PLATFORM, 'ci/integration/test-runner-native-materials.sh'): 'ci/integration/test-ci-tools.sh',
    (PLATFORM, 'test/release/test-publisher.sh'): 'test/release/test-release.sh',
    ('kuasar-sandbox/guest-runtime', 'scripts/test-release-runtime-binding.py'): 'scripts/test-release.sh',
    **{('kuasar-sandbox/' + owner, 'scripts/test-publisher.sh'): 'scripts/test-release.sh'
       for owner in OWNERS if owner != 'platform'},
}
GO_OSES = {'aix', 'android', 'darwin', 'dragonfly', 'freebsd', 'hurd', 'illumos',
           'ios', 'js', 'linux', 'netbsd', 'openbsd', 'plan9', 'solaris', 'wasip1', 'windows'}
GO_ARCHES = {'386', 'amd64', 'amd64p32', 'arm', 'arm64', 'loong64', 'mips', 'mipsle',
             'mips64', 'mips64le', 'ppc64', 'ppc64le', 'riscv64', 's390x', 'wasm'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def safe_path(path):
    return (isinstance(path, str) and bool(path) and not path.startswith('/') and
            '\\' not in path and not any(c.isspace() or ord(c) < 32 for c in path) and
            all(part not in ('', '.', '..', '.git') for part in path.split('/')))


def owner(repository):
    require(repository in REPOSITORIES, 'unknown source-test repository')
    return 'platform' if repository == PLATFORM else repository.split('/')[1]


def metadata(repository, path):
    if repository not in REPOSITORIES or not safe_path(path):
        return False
    return path == MIGRATIONS or path == 'release/guide-inputs.txt' or (
        repository == PLATFORM and path in GUIDE_DECLARATIONS)


def test_script(repository, path):
    """Standalone source-test entry points, never product cases or helper inputs."""
    if repository not in REPOSITORIES or not safe_path(path):
        return False
    parts = PurePosixPath(path).parts
    name = parts[-1]
    if path == 'ci/integration/test_fixtures.py':
        return False
    if any(part in {'cases', 'fixtures', 'testdata'} for part in parts):
        return False
    if not name.endswith(('.py', '.sh')):
        return False
    named_test = name.startswith(('test_', 'test-')) or name.endswith(('_test.py', '_test.sh'))
    if path.startswith('scripts/') and len(parts) == 2:
        return named_test
    if path.startswith('test/e2e/'):
        if repository == PLATFORM and name.startswith('test_') and name.endswith('.py'):
            return len(parts) == 3 or 'lib' in parts[2:-1]
        if repository.endswith('/guest-runtime') and path == 'test/e2e/test_scripts.py':
            return True
        # package_inputs.library_inputs excludes test_* library entries from
        # delivered runtime inputs. Ordinary libraries remain outside this rule.
        return 'lib' in parts[2:-1] and name.startswith('test_') and name.endswith('.py')
    if path.startswith(('test/source/', 'test/scripts/')):
        return named_test or (path.startswith('test/source/') and name.endswith('.sh'))
    if repository == PLATFORM:
        if path == 'workbench/test_workbench.py':
            return True
        if path.startswith(('ci/', 'test/release/', 'test/demo/')):
            return named_test
        if path.startswith('test/perf/'):
            return name.endswith(('_test.py', '_test.sh'))
        if path.startswith('release/'):
            return named_test
    return path in {'deps/test-common.sh', 'native-deps/deps/test-common.sh'}


def go_test(repository, path):
    if repository not in REPOSITORIES - {PLATFORM} or not safe_path(path) or not path.endswith('_test.go'):
        return False
    names = PurePosixPath(path).parts
    if any(name.startswith(('.', '_')) for name in names) or set(names[:-1]) & {'vendor', 'testdata'}:
        return False
    # This source gate runs Linux/amd64. Architecture-specific tests retain
    # their existing qualification instead of silently being excluded by Go.
    parts = PurePosixPath(path).stem.removesuffix('_test').split('_')
    if len(parts) > 1 and parts[-1] in GO_OSES | GO_ARCHES and parts[-1] not in {'linux', 'amd64'}:
        return False
    return not (len(parts) > 2 and parts[-1] in GO_ARCHES and
                parts[-2] in GO_OSES - {'linux'})


def test_driver(repository, path):
    return TEST_DRIVERS.get((repository, path), path)


def fixture(repository, path):
    if repository not in REPOSITORIES or not safe_path(path):
        return False
    if repository == PLATFORM and path.startswith('ci/integration/fixtures/'):
        return PurePosixPath(path).name not in {'README.md', 'README_zh.md'} and (
            PurePosixPath(path).suffix in {'.json', '.yaml', '.yml', '.txt', '.md', '.py', '.sh'})
    if repository == PLATFORM and path == 'ci/integration/test_fixtures.py':
        return True
    parts = PurePosixPath(path).parts
    if path.startswith('test/e2e/') and 'lib' in parts[2:-1]:
        library = parts.index('lib', 2)
        if any(part.startswith('test_') for part in parts[library + 1:]):
            return not test_script(repository, path)
    return (repository, path) in {
        ('kuasar-sandbox/accelerator', 'test/fixtures/flatten-old-layout.patch'),
        ('kuasar-sandbox/accelerator', 'pkg/tarstream/testdata/legacy-byte-holes.tar'),
        ('kuasar-sandbox/accelerator', 'pkg/tarstream/testdata/legacy-presence-zero.tar'),
        ('kuasar-sandbox/guest-runtime', 'scripts/testdata/linux-COPYING'),
    }


def allowed(repository, path):
    return (metadata(repository, path) or test_script(repository, path) or go_test(repository, path)
            or fixture(repository, path) or repository == PLATFORM and path in DOCUMENTATION_TOOLS)


def e2e_input(repository, path):
    """Runtime inputs take precedence over broad CI/tooling suffix rules."""
    if repository not in REPOSITORIES or not safe_path(path):
        return True
    if path.endswith('_test.go') and not go_test(repository, path):
        return True
    if '/cases/' in '/' + path:
        return path.startswith('test/e2e/') and not path.endswith('.md')
    if go_test(repository, path) or test_script(repository, path) or fixture(repository, path):
        return False
    parts = PurePosixPath(path).parts
    if path.startswith('test/e2e/') and 'lib' in parts[2:-1] and not path.endswith('.md'):
        return True
    if repository == PLATFORM:
        inputs, _ = delivery_modules()
        return path in inputs.PLATFORM_RUNTIME or path == 'test/e2e/assemble.sh'
    return False


def supported(repository, tree, changed, read_blob):
    """Do not qualify a Go test that the source gate cannot actually select."""
    for path in changed:
        if not go_test(repository, path):
            continue
        row = tree['entries'].get(path)
        if row is None:
            continue
        module = tree['entries'].get('go.mod', {})
        if module.get('type') != 'blob' or module.get('mode') != '100644':
            return False
        if any((parent / 'go.mod').as_posix() in tree['entries']
               for parent in PurePosixPath(path).parents if parent != PurePosixPath('.')):
            return False
        text = read_blob(repository, row['sha']).decode('utf-8')
        # Only leading line comments can carry Go build constraints. Stop at
        # the first token, so package names or directives inside test strings
        # and block comments cannot alter the CI classification.
        comments, offset = [], 0
        text = text.removeprefix('\ufeff')
        while offset < len(text):
            if text[offset].isspace():
                offset += 1
            elif text.startswith('//', offset):
                end = text.find('\n', offset)
                if end < 0:
                    end = len(text)
                comments.append(text[offset:end])
                offset = end
            elif text.startswith('/*', offset):
                end = text.find('*/', offset + 2)
                if end < 0:
                    break  # The syntax check will reject the unfinished comment.
                offset = end + 2
            else:
                break
        header = '\n'.join(comments)
        for expression in re.findall(r'^//go:build\s+(.+)$', header, re.MULTILINE):
            accepted = {'linux', 'amd64', 'linux && amd64', 'amd64 && linux'}
            if repository.endswith('/accelerator'):
                accepted |= {'no_rocksdb', '!no_rocksdb', 'linux && !no_rocksdb'}
            if expression.strip() not in accepted:
                return False
        for expression in re.findall(r'^//\s*\+build\s+(.+)$', header, re.MULTILINE):
            if expression.strip() not in {'linux', 'amd64', 'linux,amd64', 'amd64,linux'}:
                return False
    return True


def parser_for(repository, path):
    if metadata(repository, path):
        return 'guide-migrations' if path == MIGRATIONS else 'guide-inputs'
    if fixture(repository, path):
        return 'source-fixture'
    return tooling_lane.parser_for(repository, path)


@cache
def delivery_modules():
    directory = Path(__file__).resolve().parents[2] / 'test/e2e'
    sys.path.insert(0, str(directory))
    import package_inputs
    import assemble_docs
    return package_inputs, assemble_docs


def check_metadata(repository, tree, changed, read_blob):
    """Use the actual delivery parsers on data-only exact tree materialization."""
    selected = [path for path in changed if metadata(repository, path)]
    if not selected:
        return
    package_inputs, assemble_docs = delivery_modules()
    with tempfile.TemporaryDirectory(prefix='source-doc-inputs-') as temporary:
        roots = {name: Path(temporary) / name for name in OWNERS}
        for root in roots.values():
            root.mkdir()
        root = roots[owner(repository)]
        entries = tree['entries']
        # Write regular entries first; never traverse a candidate symlink.
        for name, row in entries.items():
            path = root / name
            if row['type'] == 'tree':
                path.mkdir(parents=True, exist_ok=True)
            elif row['type'] == 'blob' and row['mode'] != '120000':
                path.parent.mkdir(parents=True, exist_ok=True)
                needed = name.endswith('.md') or name in GUIDE_DECLARATIONS or name == MIGRATIONS
                path.write_bytes(read_blob(repository, row['sha']) if needed else b'')
        for name, row in entries.items():
            if row['mode'] == '120000':
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(read_blob(repository, row['sha']).decode('utf-8'))
        if any(path in GUIDE_DECLARATIONS for path in selected):
            package_inputs.guide_inputs(root)
        if MIGRATIONS in selected:
            assemble_docs.guide_migrations(roots)


def check_tree(repository, tree, changed, read_blob):
    checks, findings = [], []
    selected = [path for path in changed if allowed(repository, path)]
    try:
        check_metadata(repository, tree, selected, read_blob)
    except (OSError, ValueError, UnicodeError) as error:
        findings.append(str(error))
    for path in selected:
        row = tree['entries'].get(path)
        if row is None:
            checks.append(dict(path=path, state='deleted'))
            continue
        try:
            parser = parser_for(repository, path)
            if parser not in {'guide-inputs', 'guide-migrations', 'source-fixture'}:
                tooling_lane.check_blob(repository, path, read_blob(repository, row['sha']))
            checks.append(dict(path=path, state='success', parser=parser, sha=row['sha']))
        except (OSError, ValueError, SyntaxError, UnicodeError) as error:
            findings.append(path + ': ' + str(error))
    return dict(checks=checks, findings=findings)


def verify(evidence, changed, repository):
    expected = [path for path in changed if allowed(repository, path)]
    require(isinstance(evidence, dict) and evidence.get('findings') == [], 'source classification failed')
    checks = evidence.get('checks')
    require(isinstance(checks, list) and [row.get('path') for row in checks] == expected,
            'incomplete source classification evidence')
    for row in checks:
        if row.get('state') == 'deleted' and set(row) == {'path', 'state'}:
            continue
        require(set(row) == {'path', 'state', 'parser', 'sha'} and row['state'] == 'success' and
                row['parser'] == parser_for(repository, row['path']) and
                isinstance(row['sha'], str) and re.fullmatch('[0-9a-f]{40}', row['sha']),
                'invalid source classification evidence')


def scope_from_evidence(evidence):
    require(evidence.get('schema') == 1 and evidence.get('lane') == 'source' and
            evidence.get('conclusion') == 'success', 'missing successful source classification')
    sources = sorted(evidence['sources'], key=lambda row: row['repository'])
    rows = evidence['repositories']
    require([row['repository'] for row in rows] == [row['repository'] for row in sources],
            'source scope repositories differ from admission')
    changes = {owner(row['repository']): sorted(set(row['changed']) | set(row['head_changed'])) for row in rows}
    return dict(schema=1, sources=sources, changes=changes)


def validate_plan_scope(plan):
    scope = plan.get('source_scope')
    if scope is None:
        return
    require(plan.get('mode') == 'source' and isinstance(scope, dict) and
            set(scope) == {'schema', 'sources', 'changes'} and type(scope['schema']) is int and scope['schema'] == 1,
            'invalid source-only plan scope')
    require('dependency_cutover' not in plan, 'dependency cutovers require product qualification')
    records = sorted(plan.get('candidate_records', []), key=lambda row: row['repository'])
    require(records and scope['sources'] == records, 'source scope differs from admitted candidates')
    require(len(records) == len({row['repository'] for row in records}), 'duplicate source scope repository')
    tests = plan.get('test_revisions')
    require(isinstance(tests, dict), 'source-only plan lacks exact test revisions')
    primary = plan['candidate_records'][0]['repository']
    for row in records:
        require(all(isinstance(row.get(key), str) and re.fullmatch('[0-9a-f]{40}', row[key])
                    for key in ('base_sha', 'head_sha', 'candidate_sha')),
                'source scope lacks exact Git identities')
        require(tests.get(owner(row['repository'])) == {
            'repository': row['repository'], 'sha': row['candidate_sha'],
            'role': 'candidate' if row['repository'] == primary else 'companion'},
            'source tests differ from the exact admitted candidate or companion')
    changes = scope['changes']
    require(isinstance(changes, dict) and set(changes) == {owner(row['repository']) for row in records},
            'source scope has missing or extra owners')
    # Import lazily: docs_lane uses this module for source-path classification.
    import docs_lane
    has_source = False
    for row in records:
        paths = changes[owner(row['repository'])]
        require(isinstance(paths, list) and paths == sorted(set(paths)) and
                all(safe_path(path) and docs_lane.light_allowed(row['repository'], path) for path in paths),
                'source scope contains an unknown or product path')
        require(tooling_lane.CUTOVER not in paths, 'source-only scope includes a dependency cutover')
        has_source = has_source or any(allowed(row['repository'], path) for path in paths)
    require(has_source, 'source-only plan has no source-test changes')


def verify_plan_scope(plan, evidence):
    validate_plan_scope(plan)
    require(plan.get('source_scope') == scope_from_evidence(evidence),
            'source plan differs from qualified HEAD and integration changes')
