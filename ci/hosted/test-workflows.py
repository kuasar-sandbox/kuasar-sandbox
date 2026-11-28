#!/usr/bin/env python3
"""Offline public-caller, independent lifecycle and credential boundary contracts."""
import ast
import json
import os
import re
import tempfile
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]


def expression(value, context):
    """Evaluate the small Actions expression subset used by runner selection."""
    value = value.removeprefix("${{").removesuffix("}}").strip()
    tree = ast.parse(value.replace("&&", " and ").replace("||", " or "), mode="eval")

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return context[node.id]
        if isinstance(node, ast.Attribute):
            return visit(node.value)[node.attr]
        if isinstance(node, ast.BoolOp):
            result = visit(node.values[0])
            for term in node.values[1:]:
                if (isinstance(node.op, ast.And) and result) or (isinstance(node.op, ast.Or) and not result):
                    result = visit(term)
            return result
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = visit(node.left), visit(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "always":
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "fromJSON":
            return json.loads(visit(node.args[0]))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "format":
            return visit(node.args[0]).format(*(visit(arg) for arg in node.args[1:]))
        raise AssertionError(f"unsupported expression: {ast.dump(node)}")

    return visit(tree)


def load(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def check_request_rejection():
    """Run the real, credential-free request validator; routing is not authorization."""
    script = (ROOT / "ci/integration/validate-request.sh").read_text()
    sha = "1" * 40
    repository = "kuasar-sandbox/kuasar-sandbox"
    with tempfile.TemporaryDirectory() as directory:
        event = Path(directory) / "event.json"
        event.write_text(json.dumps({"repository": {"full_name": repository}, "pull_request": {
            "number": 1, "state": "open", "draft": False,
            "base": {"ref": "main", "sha": sha, "repo": {"full_name": repository}},
            "head": {"sha": sha, "repo": {"full_name": repository}}}}))
        env = dict(os.environ, GITHUB_REPOSITORY=repository, TRUSTED_WORKFLOW_REPOSITORY=repository, TRUSTED_WORKFLOW_SHA=sha,
                   GITHUB_EVENT_NAME="pull_request_target", GITHUB_EVENT_PATH=str(event),
                   CANDIDATE_REPOSITORY=repository, CANDIDATE_SHA=sha, CANDIDATE_BASE_SHA=sha,
                   CANDIDATE_HEAD_SHA=sha, CANDIDATE_BASE_REF="main", CANDIDATE_PR="1",
                   COMPANION_CANDIDATES="[]", INTEGRATION_MODE="source")
        cases = (({}, None), ({"INTEGRATION_MODE": "invalid"}, "mode must be source or exact-assets"),
                 ({"CANDIDATE_REPOSITORY": "kuasar-sandbox/connector"}, "inputs do not match"),
                 ({"CANDIDATE_HEAD_SHA": "2" * 40}, "inputs do not match"),
                 ({"INTEGRATION_MODE": "exact-assets"}, "requires workflow_dispatch"),
                 ({"TRUSTED_WORKFLOW_REPOSITORY": "outside/project"}, "implementation must come from"))
        for overrides, message in cases:
            result = subprocess.run(["bash", "-c", script], env={**env, **overrides},
                                    capture_output=True, text=True, timeout=5)
            assert (result.returncode == 0) == (message is None), (overrides, result.stderr)
            if message:
                assert message in result.stderr, (overrides, result.stderr)
        env.update(GITHUB_EVENT_NAME="pull_request", GITHUB_SHA=sha)
        for overrides, allowed in (({}, True), ({"GITHUB_SHA": "2" * 40}, False),
                                   ({"TRUSTED_WORKFLOW_SHA": "2" * 40}, False),
                                   ({"CANDIDATE_REPOSITORY": "kuasar-sandbox/connector"}, False),
                                   ({"CANDIDATE_BASE_SHA": "2" * 40}, False),
                                   ({"CANDIDATE_HEAD_SHA": "2" * 40}, False),
                                   ({"COMPANION_CANDIDATES": '[{}]'}, False)):
            result = subprocess.run(["bash", "-c", script], env={**env, **overrides},
                                    capture_output=True, text=True, timeout=5)
            assert (result.returncode == 0) == allowed, (overrides, result.stderr)
        original = event.read_text()
        for fault in ("fork", "draft", "closed"):
            data = json.loads(original)
            if fault == "fork": data["pull_request"]["head"]["repo"]["full_name"] = "outside/fork"
            elif fault == "draft": data["pull_request"]["draft"] = True
            else: data["pull_request"]["state"] = "closed"
            event.write_text(json.dumps(data))
            result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=5)
            assert result.returncode != 0, fault
        env['GITHUB_EVENT_NAME'] = 'push'
        result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=5)
        assert result.returncode != 0


def check_persistent_workspace_isolation():
    # Execute the real setup and transport twice in a reused Actions workspace.
    # A prior prepared tree must neither be overwritten nor block a new group.
    steps = {row.get('name'): row for row in load('integration-architecture.yml')['jobs']['e2e']['steps']}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); temporary = root / 'temp'; temporary.mkdir()
        (root / 'prepared').mkdir(); (root / 'prepared/stale').write_text('prior job')
        payload = root / 'payload'; payload.write_text('current artifact')
        import tarfile
        with tarfile.open(root / 'prepared.tar', 'w') as archive:
            archive.add(payload, arcname='aarch64/payload')
        environment = dict(os.environ, RUNNER_TEMP=str(temporary), GITHUB_RUN_ID='42', GITHUB_RUN_ATTEMPT='1',
                           TARGET_ARCH='aarch64', TEST_SHARD='supplemental-arm--snapshot',
                           GITHUB_ENV=str(root / 'environment'))
        workspaces = []
        for _ in range(2):
            subprocess.run(['bash', '-ceu', steps['Create a fresh execution workspace']['run']],
                           cwd=root, env=environment, check=True)
            workspace = Path((root / 'environment').read_text().splitlines()[-1].split('=', 1)[1])
            assert workspace.parent == temporary and workspace not in workspaces
            subprocess.run([sys.executable, '-B', str(ROOT / 'ci/integration/transport.py'), 'extract',
                            str(root / 'prepared.tar'), str(workspace / 'prepared')], check=True)
            assert (workspace / 'prepared/aarch64/payload').read_text() == 'current artifact'
            workspaces.append(workspace)
        assert (root / 'prepared/stale').read_text() == 'prior job'


def check_e2e_artifact_destinations():
    """Use the real exclusive downloader and workflow consumer with offline bytes."""
    import importlib.util
    import io
    import tarfile
    spec = importlib.util.spec_from_file_location('workflow_download', ROOT / 'ci/integration/download_artifact.py')
    download = importlib.util.module_from_spec(spec); spec.loader.exec_module(download)
    steps = load('integration-architecture.yml')['jobs']['e2e']['steps']
    transfers = [step for step in steps if step.get('with', {}).get('name', '').startswith(
        ('integration-prepared-', 'integration-runtime-'))]
    assert len(transfers) == 2
    consumer = next(step['run'] for step in steps
                    if step.get('name') == 'Run the selected capability group from immutable inputs')
    with tempfile.TemporaryDirectory(prefix='e2e-artifact-destinations-') as directory:
        root = Path(directory)
        (root / 'framework').symlink_to(ROOT, target_is_directory=True)
        validator = root / 'validate.py'
        validator.write_text('''from pathlib import Path
import os, sys
args = sys.argv[1:]
workspace = Path(args[args.index('--workspace') + 1])
runtime = Path(args[args.index('--runtime') + 1])
assert (workspace / 'payload').read_bytes() == b'prepared product fixture'
if os.environ['TEST_RUNTIME'] == 'runtime-only':
    assert runtime.read_bytes() == b'exact runtime fixture'
else:
    assert not runtime.exists()
Path(os.environ['CONSUMER_MARKER']).write_text('checked')
''')
        # Intercept only the product launcher. The actual workflow shell and
        # transport extractor still consume the downloaded fixture paths.
        prefix = '''python3() {
  if [ "$2" = framework/ci/integration/run-capability-group.py ]; then
    command python3 "$CONSUMER_VALIDATOR" "${@:3}"
  else
    command python3 "$@"
  fi
}
'''
        for arch in ('x86_64', 'aarch64'):
            for runtime in ('runtime-only', 'system'):
                workspace = root / (arch + '-' + runtime)
                workspace.mkdir()
                prepared = io.BytesIO()
                with tarfile.open(fileobj=prepared, mode='w:gz') as archive:
                    body = b'prepared product fixture'
                    member = tarfile.TarInfo(arch + '/payload')
                    member.size, member.mode = len(body), 0o644
                    archive.addfile(member, io.BytesIO(body))
                def offline_artifact(selected, frozen, staging, send):
                    payload = staging / 'payload'; payload.mkdir()
                    name, body = ('prepared.tar.gz', prepared.getvalue()) if selected['name'].startswith(
                        'integration-prepared-') else ('runtime.tar.gz', b'exact runtime fixture')
                    (payload / name).write_bytes(body)
                for step in transfers:
                    if 'if' in step and not expression(step['if'], {'matrix': {'group': {'runtime': runtime}}}):
                        continue
                    name = step['with']['name'].replace('${{ inputs.arch }}', arch).replace('${{ github.run_id }}', '42')
                    destination = step['with']['path'].replace('${{ env.E2E_WORKSPACE }}', str(workspace))
                    download.download({'name': name, 'pattern': ''}, destination,
                                      seconds=5, delays=(), operation=offline_artifact)
                marker = workspace / 'consumer-checked'
                env = dict(os.environ, E2E_WORKSPACE=str(workspace), TARGET_ARCH=arch,
                           TEST_SHARD='fixture', TEST_RUNTIME=runtime,
                           CONSUMER_VALIDATOR=str(validator), CONSUMER_MARKER=str(marker))
                subprocess.run(['bash', '-ceu', prefix + consumer], cwd=root, env=env,
                               check=True, capture_output=True, text=True, timeout=10)
                assert marker.read_text() == 'checked'
        print('e2e artifact destinations: both architectures, runtime-only and system consumers passed')


def check_helper_materialization():
    """Execute the real artifact transport/layout and validate exact tag inputs."""
    import importlib.util
    import shutil
    import tarfile
    from unittest.mock import patch
    sys.path.insert(0, str(ROOT / 'release'))
    import tag_sources
    sys.path.insert(0, str(ROOT / 'test/release'))
    from test_tag_sources import fixture
    steps = load('aggregate-release.yml')['jobs']['helper-build']['steps']
    script = next(step['run'] for step in steps
                  if step.get('name') == 'Materialize exact helper inputs without running a compiler')
    assert 'fetch' not in script.replace('fetched', '') and 'test-revisions' not in script
    spec = importlib.util.spec_from_file_location('workflow_cache_scope', ROOT / 'ci/hosted/cache-scope.py')
    scope = importlib.util.module_from_spec(spec); spec.loader.exec_module(scope)
    selected = {unit: ('runtime-v1.2.3' if unit == 'runtime' else 'vmlinux-v1.2.3' if unit == 'vmlinux' else 'v1.2.3')
                for unit in tag_sources.selection.UNITS}
    with tempfile.TemporaryDirectory(prefix='helper-materialize-') as temporary:
        directory = Path(temporary)
        for name in ('valid', 'missing-owner', 'moved-tag', 'dirty-source', 'wrong-owner'):
            case = directory / name
            fetched = case / 'original/fetched'
            records = {unit: fixture(fetched / 'sources' / unit, unit, tag) for unit, tag in selected.items()}
            platform_sha = fixture(fetched / 'platform', 'connector', 'v1.2.3')
            tag_sources.git(fetched / 'platform', 'remote', 'set-url', 'origin',
                            'https://github.com/kuasar-sandbox/kuasar-sandbox.git')
            owner = fetched / 'sources/sandboxer'
            if name == 'missing-owner': shutil.rmtree(owner)
            if name == 'dirty-source': (owner / 'product.go').write_text('changed input')
            if name == 'moved-tag': tag_sources.git(owner, 'tag', '-d', selected['sandboxer'])
            if name == 'wrong-owner': tag_sources.git(owner, 'remote', 'set-url', 'origin', 'https://example.invalid/wrong.git')
            (case / 'transport').mkdir()
            with tarfile.open(case / 'transport/fetched.tar', 'w') as archive:
                archive.add(fetched, arcname='fetched')
            shutil.rmtree(case / 'original')
            (case / 'control').symlink_to(ROOT, target_is_directory=True)
            subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=case, check=True)
            sources = case / 'helper-work'
            if name != 'valid':
                try: tag_sources.inspect_sources(sources, selected, owner_layout=True)
                except (ValueError, subprocess.CalledProcessError): pass
                else: raise AssertionError('invalid helper source accepted: ' + name)
                continue
            facts = tag_sources.inspect_sources(sources, selected, owner_layout=True)
            assert {unit: row['sha'] for unit, row in facts.items()} == records
            expected = {str(tag_sources.source_path(Path('.'), unit, owner_layout=True)): sha for unit, sha in records.items()}
            expected['platform'] = platform_sha
            event = case / 'event.json'; event.write_text('{"inputs": {}}')
            environment = {'GITHUB_REPOSITORY': 'kuasar-sandbox/kuasar-sandbox', 'GITHUB_REF': 'refs/heads/main',
                           'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_RUN_ID': 'fixture', 'GITHUB_EVENT_PATH': str(event)}
            with patch.dict(os.environ, environment), patch.object(scope, 'public_main', return_value=True):
                receipt = scope.decide(sources, case / 'scope.json')
            assert receipt['scope'] == receipt['namespace'] == 'trusted'
            assert {row['path']: row['sha'] for row in receipt['sources']} == expected
            assert all(row['clean'] and row['on_main'] for row in receipt['sources'])
    print('helper materialization: exact transported tag trees and four invalid inputs passed')




def check_aggregate_collection_before_helpers():
    """Execute source-only collection and the moved native-helper join offline."""
    import hashlib
    import shutil
    import struct
    import tarfile
    sys.path.insert(0, str(ROOT / 'test/release'))
    from test_documentation_package import DocumentationPackageTest, OWNERS

    jobs = load('aggregate-release.yml')['jobs']
    collect, stage = jobs['collect'], jobs['stage']
    assert collect['needs'] == 'prepare'
    assert stage['needs'] == ['prepare', 'helper-build', 'workbench-build', 'node-infra-build']
    assert "needs.helper-build.result == 'success'" in stage['if']
    assert 'aggregate-helpers-' not in json.dumps(collect)
    assert 'aggregate-inputs-' not in json.dumps((collect, stage))
    assert any(row.get('with', {}).get('name') == 'aggregate-sources-' + '$' + '{{ github.run_id }}'
               for row in stage['steps'])
    assert any(row.get('with', {}).get('pattern') == 'aggregate-helpers-*-' + '$' + '{{ github.run_id }}'
               for row in stage['steps'])
    materialize = next(row['run'] for row in collect['steps']
                       if row.get('name') == 'Materialize the exact collection sources')
    gather = next(row['run'] for row in collect['steps']
                  if row.get('name') == 'Collect the shared external inputs once for both native images')
    join = next(row['run'] for row in stage['steps']
                if row.get('name') == 'Assemble the complete declared set without repackaging products')
    assert 'package-platform.sh' not in gather and 'tar -xzf' not in gather
    assert 'control/release/aggregate-release.sh assemble "$VERSION" inputs/fetched aggregate-stage' in join
    # Only the changed materialization prefix runs here. Final package/validation
    # remains the existing aggregate-release entry, not a replacement fixture.
    join = join.split('export SOURCE_DATE_EPOCH=', 1)[0]

    fixture = DocumentationPackageTest()
    fixture.setUp()
    try:
        base = fixture.root
        executable = base / 'bin'; executable.mkdir()
        sudo = executable / 'sudo'
        sudo.write_text('#!/bin/sh\n[ "$1" = apt-get ]\n')
        sudo.chmod(0o755)
        environment = dict(os.environ, PATH=str(executable) + os.pathsep + os.environ['PATH'],
                           VERSION='release-v1.2.3', E2E_SOURCE_ROOT='/missing-source',
                           E2E_HELPER_ROOT='/missing-helpers', E2E_WHEEL_ROOT='/missing-wheels')
        collector = ('import argparse, json\nfrom pathlib import Path\n'
                     'p=argparse.ArgumentParser(); p.add_argument("--cases", type=Path); '
                     'p.add_argument("--arch", action="append"); p.add_argument("--output", type=Path)\n'
                     'a=p.parse_args(); a.output.mkdir(); '
                     '(a.output/"observed.json").write_text(json.dumps('
                     '{"cases": sorted(x.name for x in a.cases.iterdir()), "arches": a.arch}))\n')

        def sources(case, fault=None):
            original = case / 'original/fetched'
            shutil.copytree(base / 'platform', original / 'platform')
            # Only OCI acquisition is replaced: the production assembler, transport,
            # owner declarations, case bytes and output tar command all execute.
            (original / 'platform/workbench/collect-inputs.py').write_text(collector)
            for owner in (*OWNERS[1:], 'vmlinux'):
                name = 'runtime' if owner == 'guest-runtime' else owner
                shutil.copytree(base / owner, original / 'sources' / name)
            selected = ''.join(name + '\tv1.2.3\n'
                               for name in ('accelerator', 'connector', 'runtime', 'sandboxer', 'orchestrator', 'vmlinux'))
            (original / 'selection.tsv').write_text(selected)
            if fault == 'missing-owner':
                shutil.rmtree(original / 'sources/connector')
            if fault == 'duplicate-case':
                shutil.copy2(original / 'sources/accelerator/test/e2e/cases/basic.accelerator-fixture.sh',
                             original / 'sources/connector/test/e2e/cases/basic.accelerator-fixture.sh')
            (case / 'transport').mkdir(parents=True)
            with tarfile.open(case / 'transport/fetched.tar', 'w') as archive:
                archive.add(original, arcname='fetched')
            (case / 'control').symlink_to(ROOT, target_is_directory=True)
            return selected

        expected = {f'basic.{owner}-fixture.sh':
                    (base / owner / ('test/e2e/platform/cases' if owner == 'platform'
                                     else 'test/e2e/cases') / f'basic.{owner}-fixture.sh').read_bytes()
                    for owner in OWNERS}
        for fault in (None, 'missing-owner', 'duplicate-case'):
            case = base / ('collect-' + (fault or 'valid'))
            selected = sources(case, fault)
            subprocess.run(['bash', '-euo', 'pipefail', '-c', materialize],
                           cwd=case, env=environment, check=True, capture_output=True)
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', gather],
                                    cwd=case, env=environment, capture_output=True, text=True)
            assert (result.returncode == 0) == (fault is None), (fault, result.stderr)
            if fault:
                assert not (case / 'workbench-inputs.tar').exists(), fault
                continue
            assert not (case / 'fetched/e2e-helpers').exists()
            assert (case / 'collection-docs-refs.tsv').read_text() == selected + 'platform\trelease-v1.2.3\n'
            observed = json.loads((case / 'collected-inputs/observed.json').read_text())
            assert observed == {'cases': sorted(expected), 'arches': ['x86_64', 'aarch64']}
            with tarfile.open(case / 'workbench-inputs.tar') as archive:
                actual = {Path(item.name).name: archive.extractfile(item).read()
                          for item in archive.getmembers()
                          if item.isfile() and item.name.startswith('collected-platform/test/e2e/cases/')}
            assert actual == expected

        for fault in (None, 'missing-arm', 'corrupt-arm'):
            case = base / ('stage-' + (fault or 'valid'))
            sources(case)
            (case / 'helpers').mkdir()
            (case / 'workbench-images').mkdir()
            (case / 'workbench-images/preserved').write_text('original image receipt')
            for arch, machine in [('x86_64', 62), ('aarch64', 183)]:
                if fault == 'missing-arm' and arch == 'aarch64':
                    continue
                payload = case / ('payload-' + arch)
                helper = payload / 'e2e-helpers' / arch; helper.mkdir(parents=True)
                wheel = payload / 'e2e-wheels' / arch; wheel.mkdir(parents=True)
                (wheel / 'fixture.whl').write_bytes(b'wheel bytes')
                binary = bytearray(64); binary[:7] = b'\x7fELF\x02\x01\x01'
                struct.pack_into('<H', binary, 18, machine)
                (helper / 'probe').write_bytes(binary); (helper / 'probe').chmod(0o755)
                row = {'owner': 'platform', 'env': None, 'cases': ['*'],
                       'sha256': hashlib.sha256(binary).hexdigest(), 'size': len(binary), 'mode': 0o755}
                (helper / 'helpers.json').write_text(json.dumps({'schema': 2, 'arch': arch, 'helpers': {'probe': row}}))
                if fault == 'corrupt-arm' and arch == 'aarch64':
                    (helper / 'probe').write_bytes(binary + b'changed')
                with tarfile.open(case / f'helpers/helpers-{arch}.tar', 'w') as archive:
                    for name in ('e2e-helpers', 'e2e-wheels'):
                        archive.add(payload / name, arcname=name)
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', join],
                                    cwd=case, env=environment, capture_output=True, text=True)
            assert (result.returncode == 0) == (fault is None), (fault, result.stderr)
            if not fault:
                for arch in ('x86_64', 'aarch64'):
                    assert (case / f'inputs/fetched/e2e-helpers/{arch}/probe').is_file()
                    assert (case / f'inputs/fetched/e2e-wheels/{arch}/fixture.whl').read_bytes() == b'wheel bytes'
                assert (case / 'inputs/fetched/workbench/preserved').read_text() == 'original image receipt'
    finally:
        fixture.doCleanups()
    print('aggregate collection: exact cases before helpers; missing/colliding sources and missing/corrupt helpers rejected')


def check_validation_only_identity():
    definition = load('aggregate-release.yml')
    deletion = load('delete-preview.yml')
    assert definition['concurrency']['cancel-in-progress'] is False
    assert deletion['concurrency']['cancel-in-progress'] is False

    def render(value, context):
        return re.sub(r'\$\{\{(.*?)\}\}', lambda match: str(expression(match[0], context)), value)

    locks = set()
    for repository in ('kuasar-sandbox/kuasar-sandbox', 'other/repo'):
        for version in ('release-v1.2.3', 'release-v1.2.3-preview.20261015'):
            context = {'github': {'repository': repository, 'run_id': 100, 'run_attempt': 1},
                       'inputs': {'version': version, 'source_sha': 'a' * 40, 'validation_only': False}}
            ordinary = render(definition['concurrency']['group'], context)
            assert ordinary == render(deletion['concurrency']['group'], context)
            assert ordinary == f'aggregate-mutation-{repository}-{version}'
            assert ordinary not in locks
            locks.add(ordinary)
            assert render(definition['run-name'], context) == f'Aggregate {version} @' + 'a' * 40
            # Separate actual dispatches for all and A+B, including repeated all
            # and group runs, must not queue/replace each other or publication.
            for grouped in (False, True, False, True):
                context['inputs'].update(validation_only=True, workbench_runtime_groups=grouped)
                context['github']['run_id'] = len(locks) + 100
                validation = render(definition['concurrency']['group'], context)
                assert validation not in locks and validation != ordinary
                locks.add(validation)
                assert render(definition['run-name'], context) == f'Validate aggregate {version} @' + 'a' * 40
                context['github']['run_attempt'] = 2
                assert render(definition['concurrency']['group'], context) == validation
                # Nested exact-assets locks continue to isolate these runs.
                context['inputs'].update(mode='exact-assets', candidate_repository='', candidate_sha='',
                                         release_version=version, arch='x86_64')
                context['github']['event_name'] = 'workflow_dispatch'
                for workflow in ('integration-tests.yml', 'integration-architecture.yml'):
                    nested = render(load(workflow)['concurrency']['group'], context)
                    assert str(context['github']['run_id']) in nested


def check_validation_only_publication():
    definition = load('aggregate-release.yml')
    events = definition.get('on', definition.get(True))
    assert events['workflow_dispatch']['inputs']['validation_only']['default'] is False
    jobs = definition['jobs']
    script = next(step['run'] for step in jobs['publish']['steps']
                  if step.get('name') == 'Publish aggregate release')
    # Execute the real routing shell with inert transports: all verification
    # consumers run in validation mode, while the publish command cannot run.
    with tempfile.TemporaryDirectory(prefix='validation-only-routing-') as temporary:
        root = Path(temporary)
        (root / 'bin').mkdir(); (root / 'release').mkdir(); (root / 'platform').mkdir()
        for name in ('publish-release.sh', 'aggregate-release.sh', 'validate-preview-line.sh'):
            path = root / 'release' / name
            path.write_text('#!/bin/sh\nprintf \'%s:%s\\n\' \'%s\' "$*" >> "$CALL_LOG"\n' % ('%s', '%s', name))
            if name == 'publish-release.sh':
                with path.open('a') as stream:
                    stream.write('if [ "$1" = check ] && [ "$(cat "$PUBLICATION_STATE")" = true ]; then exit 1; fi\n')
            path.chmod(0o755)
        git = root / 'bin/git'
        git.write_text('#!/bin/sh\nprintf \'%s\\n\' "$OBSERVED_SHA"\n'); git.chmod(0o755)
        python = root / 'bin/python3'
        python.write_text('#!/bin/sh\nprintf \'python:%s\\n\' "$*" >> "$CALL_LOG"\n'
                          'case "$*" in *release/workbench_registry.py*)\n'
                          '  if [ "$PUBLISH_DURING_REGISTRY" = true ]; then printf "true\\n" > "$PUBLICATION_STATE"; fi ;;\n'
                          'esac\n')
        python.chmod(0o755)
        for mode, identity, published, success in [('true', 'a' * 40, 'during-registry', False),
                ('true', 'a' * 40, 'false', True),
                ('false', 'a' * 40, 'false', True), ('invalid', 'a' * 40, 'false', False),
                ('true', 'b' * 40, 'false', False), ('true', 'a' * 40, 'true', False)]:
            log = root / 'calls'; log.write_text('')
            state = root / 'publication-state'
            state.write_text('true' if published == 'true' else 'false')
            env = dict(os.environ, PATH=str(root / 'bin') + os.pathsep + os.environ['PATH'],
                       CALL_LOG=str(log), OBSERVED_SHA=identity, VALIDATION_ONLY=mode, PUBLICATION_STATE=str(state),
                       PUBLISH_DURING_REGISTRY='true' if published == 'during-registry' else 'false',
                       SOURCE_SHA='a' * 40, VERSION='release-v1.2.3', SOURCE_REF='main', DELIVERY='workbench-v1',
                       PLATFORM_SOURCE_ROOT=str(root / 'platform'),
                       RELEASE_PLAN_FILE='validated/plan/plan.json',
                       RELEASE_VALIDATION_FILE='validated/validation.json',
                       RELEASE_WORKBENCH_RESULTS='workbench-results')
            result = subprocess.run(['bash', '-c', script], cwd=root, env=env, capture_output=True)
            assert (result.returncode == 0) == success, (mode, identity, published, result.stderr)
            assert (b'Validation-only acceptance completed;' in result.stdout) == (mode == 'true' and success)
            calls = log.read_text().splitlines()
            writes = [line for line in calls if line.startswith('publish-release.sh:publish ')]
            assert bool(writes) == (mode == 'false' and success), (mode, calls)
            if published == 'true':
                assert len(calls) == 2 and calls[-1].startswith('publish-release.sh:check '), calls
            if mode == 'true' and (success or published == 'during-registry'):
                # Both checks and all consumers run in order, with no mutation
                # entry point. The race starts unpublished and changes during
                # the last consumer, so only the final observation can reject it.
                assert calls == [
                    'validate-preview-line.sh:release-v1.2.3 ' + 'a' * 40,
                    'publish-release.sh:check release-v1.2.3',
                    'aggregate-release.sh:validate release-v1.2.3 aggregate-stage',
                    'python:-B release/publication_body.py aggregate-stage/release-notes.md',
                    'python:-B release/workbench_registry.py aggregate-stage release-v1.2.3 ' + 'a' * 40 + ' --validate-only'
                    ' --plan validated/plan/plan.json --validation validated/validation.json --workbench workbench-results',
                    'publish-release.sh:check release-v1.2.3'], calls
                assert state.read_text().strip() == ('true' if published == 'during-registry' else 'false')
        context = {'github': {'repository': 'kuasar-sandbox/kuasar-sandbox', 'event': {'repository': {
            'visibility': 'public', 'full_name': 'kuasar-sandbox/kuasar-sandbox'}}}, 'true': True}
        for mode in (False, True):
            context['inputs'] = {'validation_only': mode}
            assert expression(jobs['cleanup']['if'], context) == (not mode)


def check():
    entry = load("ci-entry.yml")["jobs"]
    integration = load("integration-tests.yml")["jobs"]
    lanes = load("integration-architecture.yml")["jobs"]
    # Evaluate real allocation guards: candidate inputs, provider publicity and
    # failure/finalization cannot authorize a private or mismatched actual caller.
    count = 0
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        name = path.name
        for job_name, job in load(name)["jobs"].items():
            # A thin reusable caller allocates no runner itself. Every actual
            # allocation, and guarded nested invocation, is evaluated below.
            if "runs-on" not in job and "if" not in job:
                continue
            assert "if" in job, (name, "missing pre-allocation guard")
            for caller in ("kuasar-sandbox/accelerator", "kuasar-sandbox/kuasar-sandbox", "outside/repo"):
                for visibility in ("private", "internal", ""):
                    context = {"github": {"repository": caller, "event": {"repository": {
                        "full_name": caller, "visibility": visibility, "private": True}}},
                        "inputs": {"candidate_repository": "kuasar-sandbox/kuasar-sandbox", "arch": "aarch64", "mode": "source"}}
                    assert expression(job["if"], context) is False, (name, caller, visibility)
                    count += 1
                context["github"]["event"]["repository"].update(visibility="public", private=False, full_name="provider/other")
                assert expression(job["if"], context) is False, name
            if "runs-on" in job:
                assert "self-hosted" not in str(job["runs-on"]), name
                if not str(job["runs-on"]).startswith("${{"):
                    expected = "ubuntu-24.04" if (name, job_name) == ("aggregate-release.yml", "collect") else "ubuntu-latest"
                    if (name, job_name) == ("integration-tests.yml", "source-checks"):
                        expected = "ubuntu-24.04"
                    assert job["runs-on"] == expected, (name, job["runs-on"])
    check_request_rejection()
    check_validation_only_identity()
    check_validation_only_publication()
    caller = load("ci.yml")
    assert caller["permissions"] == {"contents": "read", "pull-requests": "read"}
    platform = caller["jobs"]["ci"]
    assert platform["uses"] == "./.github/workflows/integration-tests.yml"
    assert "secrets" not in platform and "permissions" not in platform
    assert platform["with"]["candidate_sha"] == "${{ github.sha }}"
    context = {"github": {"repository": "kuasar-sandbox/kuasar-sandbox", "event_name": "pull_request",
               "event": {"repository": {"visibility": "public", "full_name": "kuasar-sandbox/kuasar-sandbox"},
                         "pull_request": {"draft": False, "head": {"repo": {"full_name": "kuasar-sandbox/kuasar-sandbox"}}}}},
               "false": False}
    assert expression(platform["if"], context) is True
    assert expression(integration["results"]["name"], context) == "finalize"
    for event in ("pull_request_target", "workflow_dispatch"):
        context["github"]["event_name"] = event
        assert expression(platform["if"], context) is False
        assert expression(integration["results"]["name"], context) == "results"
    context["github"]["event_name"] = "pull_request_target"
    context["github"]["event"]["pull_request"]["head"]["repo"]["full_name"] = "outside/fork"
    fork = caller["jobs"]["trusted-fork"]
    assert expression(fork["if"], context) is True
    assert fork["name"] == "ci" and fork["uses"].endswith("/ci-entry.yml@main")
    context["github"]["event_name"] = "pull_request"
    assert expression(platform["if"], context) is False
    assert expression(fork["if"], context) is False
    assert "github.event_name" in load("integration-tests.yml")["concurrency"]["group"]
    assert "github.event_name" in load("ci-entry.yml")["concurrency"]["group"]
    assert any("validate-source-set.sh" in step.get("run", "") for step in integration["results"]["steps"])
    assert entry["e2e"]["uses"] == "./.github/workflows/integration-tests.yml"
    assert entry["e2e"]["with"]["candidate_repository"] == "${{ github.repository }}"
    for job in (entry["admission"], entry["finalize"]):
        checkout, bootstrap = job["steps"][:2]
        assert checkout["with"]["ref"] == "${{ job.workflow_sha }}"
        assert checkout["with"]["repository"] == "${{ job.workflow_repository }}"
        assert checkout["with"]["persist-credentials"] is False
        assert "--profile control" in bootstrap["run"]
    for arch in ("x86_64", "aarch64"):
        lane = integration[arch]
        assert lane["needs"] == "resolve"  # no other-architecture build barrier
        assert lane["uses"] == "./.github/workflows/integration-architecture.yml"
        assert lane["with"]["arch"] == arch
    assert lanes["prepare"]["needs"] == "build"
    assert lanes["e2e"]["needs"] == ["build", "prepare"]
    assert "matrix.group.id" in lanes["e2e"]["concurrency"]["group"]
    assert "inputs.arch" in lanes["e2e"]["concurrency"]["group"]
    assert "github.run_attempt" in lanes["e2e"]["concurrency"]["group"]
    for arch, runner in (("x86_64", "ubuntu-24.04"), ("aarch64", "ubuntu-24.04-arm")):
        for name in ("build", "prepare", "performance"):
            assert expression(lanes[name]["runs-on"], {"inputs": {"arch": arch}}) == runner
    # Only E2E allocation may select a supplemental profile, from the trusted
    # build job's validated plan. All other lane allocations remain hosted.
    assert lanes['e2e']['runs-on'] == '${{ matrix.group.runs_on }}'
    assert lanes['e2e']['strategy']['matrix'] == {'group': '${{ fromJSON(needs.build.outputs.shards) }}'}
    assert "needs.build.outputs.has_groups == 'true'" in lanes['e2e']['if']
    profile_config = json.loads((ROOT / 'ci/integration/runner-capabilities.json').read_text())
    for profile in profile_config['profiles'].values():
        actual = expression(lanes['e2e']['runs-on'], {'matrix': {'group': {'runs_on': profile['runs_on']}}})
        assert actual == profile['runs_on']
    e2e_steps = {step.get('name'): step for step in lanes['e2e']['steps']}
    assert e2e_steps["Run the selected capability group from immutable inputs"]["env"]["EXECUTION_RUNNER_ENVIRONMENT"] == "${{ runner.environment }}"
    executor_upload = {step.get('name'): step for step in integration['resolve']['steps']}['Upload exact trusted executor']
    assert executor_upload['with']['name'] == '${{ steps.executor.outputs.artifact }}'
    assert executor_upload['with']['path'] == 'executor.tar.gz'
    for arch in ('x86_64', 'aarch64'):
        for key in ('executor_artifact', 'executor_digest', 'executor_tree'):
            assert integration[arch]['with'][key] == '${{ needs.resolve.outputs.' + key + ' }}'
    assert e2e_steps['Check out only the trusted executor']['if'] == "runner.environment == 'github-hosted'"
    download_executor = e2e_steps['Download the exact executor capsule']
    assert download_executor['if'] == "runner.environment == 'self-hosted'"
    assert download_executor['with']['name'] == '${{ inputs.executor_artifact }}'
    assert download_executor['with']['path'] == '${{ env.E2E_WORKSPACE }}/executor'
    assert download_executor['timeout-minutes'] == 5
    restore_executor = e2e_steps['Verify and restore exact executor Git objects']
    assert restore_executor['if'] == download_executor['if']
    assert restore_executor['timeout-minutes'] == 3
    assert restore_executor['env']['EXECUTOR_DIGEST'] == '${{ inputs.executor_digest }}'
    assert restore_executor['env']['TRUSTED_WORKFLOW_SHA'] == '${{ job.workflow_sha }}'
    assert restore_executor['run'].index('hashlib.sha256') < restore_executor['run'].index("tarfile.open")
    assert restore_executor['run'].index("tarfile.open") < restore_executor['run'].index('restore.py" restore')
    assert e2e_steps['Bootstrap declared native execution profile']['if'] == "matrix.group.executor == 'native'"
    assert e2e_steps["Release only this group's Workbench resources"]['if'] == "always() && matrix.group.executor == 'workbench-system' && env.E2E_WORKSPACE != ''"
    assert 'mktemp -d' in e2e_steps['Create a fresh execution workspace']['run']
    for step in lanes['e2e']['steps']:
        if step.get('uses', '').endswith('/.github/actions/download-artifact'):
            assert step['with']['path'].startswith('${{ env.E2E_WORKSPACE }}/')
    assert e2e_steps['Upload exact shard result']['with']['path'].startswith('${{ env.E2E_WORKSPACE }}/results/')
    assert "--delete-output" in e2e_steps["Remove only this task's retained files"]['run']
    building = {step.get("name"): step for step in lanes["build"]["steps"]}
    for name in ("Build trusted native archive readers in Workbench", "Build the affected delta and test helpers once"):
        assert building[name]["uses"] == "./framework/.github/actions/workbench"
        assert building[name]["with"]["selection"] == "plan/workbench.json"
        assert "env" not in building[name]
    assert building["Build trusted native archive readers in Workbench"]["with"]["cache"] == "false"
    assert "--materialize-only" in building["Fetch the exact private source and helper set without a compiler"]["run"]
    assert "--materialized" in building["Build the affected delta and test helpers once"]["with"]["run"]
    assert "artifact-cross" not in json.dumps(lanes["build"])
    for name in ("prepare", "e2e", "performance"):
        steps = {step.get("name"): step for step in lanes[name]["steps"]}
        assert steps["Download trusted native archive readers"]["with"]["name"] == "integration-readers-${{ inputs.arch }}-${{ github.run_id }}"
        assert "sha256sum --check SHA256SUMS" in steps["Verify and select the prebuilt archive readers"]["run"]
    resolve_steps = integration['resolve']['steps']
    resolve_names = [row.get('name') for row in resolve_steps]
    assert resolve_names.index('Validate reusable workflow request') < resolve_names.index('Resolve declared companions for the read-only framework PR') < resolve_names.index('Validate exact source admission')
    companion_step = next(row for row in resolve_steps if row.get('id') == 'companions')
    assert companion_step['if'] == "inputs.mode == 'source' && github.event_name == 'pull_request'"
    final_source = next(row for row in integration['results']['steps'] if row.get('name') == 'Recheck the current exact source candidate before acceptance')
    assert final_source['env']['COMPANION_CANDIDATES'] == '${{ needs.resolve.outputs.companions }}'
    aggregate = load("aggregate-release.yml")["jobs"]
    assert aggregate["helper-build"]["needs"] == "prepare"
    assert aggregate["collect"]["needs"] == "prepare"
    assert aggregate["workbench-build"]["needs"] == ["prepare", "collect"]
    assert aggregate["helper-build"]["strategy"]["matrix"]["include"] == [
        {"arch": "x86_64", "runner": "ubuntu-24.04"}, {"arch": "aarch64", "runner": "ubuntu-24.04-arm"}]
    assert aggregate["release-asset-validation"]["with"]["workbench_selection_artifact"] == "aggregate-sources-${{ github.run_id }}"
    assert "helper-build" not in json.dumps(aggregate["prepare"])
    helper = next(step for step in aggregate["helper-build"]["steps"] if step.get("uses") == "./control/.github/actions/workbench")
    assert '--arch "$TARGET_ARCH"' in helper["with"]["run"]
    assert "GH_TOKEN" not in helper["with"]["run"]
    check_helper_materialization()
    check_aggregate_collection_before_helpers()
    check_persistent_workspace_isolation()
    check_e2e_artifact_destinations()
    assert integration["results"]["needs"] == ["resolve", "x86_64", "aarch64", "source-checks", "workbench-native", "workbench-release"]
    native = integration['workbench-native']
    assert native['needs'] == 'resolve'
    assert {(item['arch'], item['runner']) for item in native['strategy']['matrix']['include']} == {
        ('x86_64', 'ubuntu-24.04'), ('aarch64', 'ubuntu-24.04-arm')}
    assert 'continue-on-error' not in json.dumps(native)
    released = integration['workbench-release']
    assert released['needs'] == 'resolve'
    include = released['strategy']['matrix']['include']
    for grouped in (False, True):
        rows = expression(include, {'inputs': {'workbench_runtime_groups': grouped}})
        expected = [('x86_64', 'A'), ('x86_64', 'B'), ('aarch64', 'all')] if grouped else [('x86_64', 'all'), ('aarch64', 'all')]
        assert [(row['arch'], row['group']) for row in rows] == expected
        assert {(row['arch'], row['runner']) for row in rows} == {(row['arch'], row['runner']) for row in native['strategy']['matrix']['include']}
    assert released['strategy']['fail-fast'] is False
    release_script = next(step['run'] for step in released['steps'] if step.get('name', '').startswith('Qualify staged'))
    assert "matrix.arch" in release_script and '--artifact-only' in release_script
    assert 'continue-on-error' not in json.dumps(released)
    assert 'KUASAR_CI_APP_PRIVATE_KEY' not in json.dumps(released)
    for step in released['steps']:
        if 'actions/checkout@' in step.get('uses', ''):
            assert step['with']['ref'] == '${{ needs.resolve.outputs.framework_sha }}'
            assert step['with']['persist-credentials'] is False
    required = integration['results']['steps'][0]
    assert required['env']['WORKBENCH_RELEASE_RESULT'] == '${{ needs.workbench-release.result }}'
    script = required['run']
    for mode, value, success in [('exact-assets', 'success', True), ('exact-assets', 'skipped', False),
                                 ('exact-assets', 'failure', False), ('source', 'skipped', True),
                                 ('source', 'success', False), ('', 'skipped', False)]:
        environment = dict(os.environ, DOCS_LANE='full', RESOLVE_RESULT='success', X86_RESULT='success', ARM_RESULT='success',
                           SOURCE_RESULT='skipped' if mode == 'exact-assets' else 'success',
                           WORKBENCH_SELECTED='false', WORKBENCH_RESULT='skipped',
                           INTEGRATION_MODE=mode, WORKBENCH_RELEASE_RESULT=value)
        outcome = subprocess.run(['bash', '-c', script], env=environment, capture_output=True)
        assert (outcome.returncode == 0) == success, (mode, value, outcome.stderr)
    # A skipped source group must not be reported as passed, and source mode
    # still requires its actual checks. Exercise both incorrect summaries.
    for mode, source in [('exact-assets','success'),('source','skipped')]:
        environment.update(INTEGRATION_MODE=mode,SOURCE_RESULT=source,
                           WORKBENCH_RELEASE_RESULT='success' if mode=='exact-assets' else 'skipped')
        outcome=subprocess.run(['bash','-c',script],env=environment,capture_output=True)
        assert outcome.returncode != 0 and b'source checks' in outcome.stderr, (mode,outcome.stderr)
    for job in lanes.values():
        assert "KUASAR_CI_APP_PRIVATE_KEY" not in json.dumps(job)
        assert "continue-on-error" not in json.dumps(job)
        for step in job["steps"]:
            if "actions/checkout@" in step.get("uses", ""):
                assert step["with"]["repository"] == "kuasar-sandbox/kuasar-sandbox"
                assert step["with"]["ref"] == "${{ inputs.framework_sha }}"
                assert step["with"]["persist-credentials"] is False
    executor = json.dumps(lanes["e2e"])
    for forbidden in ("create-github-app-token", "go build", "cargo build", "source-owner.sh build", "src/platform"):
        assert forbidden not in executor, forbidden
    assert "run-capability-group.py" in executor
    assert 'run-artifact-tests.py' in (ROOT / 'ci/integration/run-capability-group.py').read_text()
    assert "sparse-checkout" in executor
    source_job = integration["source-checks"]
    assert source_job["needs"] == "resolve"
    assert source_job["strategy"]["fail-fast"] is False
    assert source_job["strategy"]["matrix"]["group"] == "${{ fromJSON(needs.resolve.outputs.source_groups) }}"
    # Docs success has no source result archive: neither transport nor binding
    # may run on that lane after integrating the source-group result collector.
    result_steps = {step.get('name'): step for step in integration['results']['steps']}
    for lane in ('docs','full'):
        for mode in ('source','exact-assets'):
            lane_context={'needs':{'resolve':{'outputs':{'lane':lane}}},'inputs':{'mode':mode}}
            for name in ('Download source evidence in separate artifact directories',
                         'Bind every source result to its current successful producer'):
                assert expression(result_steps[name]['if'],lane_context)==(lane=='full' and mode=='source')
    assert expression(integration['resolve']['outputs']['source_groups'],
                      {'steps': {'plan': {'outputs': {'source_groups': ''}}}}) == '["all"]'
    source_steps = {step.get("name"): step for step in source_job["steps"]}
    source_fetch = source_steps["Fetch exact test sources without a compiler"]["run"]
    assert "--materialize-only" in source_fetch
    assert "--profile source" not in json.dumps(source_job)
    system_name = "Run the complete required source gate in Workbench system mode"
    system = source_steps[system_name]["run"]
    source_prefix, source_user = system.split("<<'SOURCE_USER'\n", 1)
    source_user, source_suffix = source_user.split("\nSOURCE_USER\n", 1)
    assert "taskset" not in source_prefix + source_suffix
    for name, step in source_steps.items():
        if name != system_name:
            assert "taskset" not in json.dumps(step)
    affinity = source_user.index("source_cpus=$(python3 - <<'SOURCE_AFFINITY'")
    bind = source_user.index('taskset -pc "$source_cpus" "$$" >/dev/null')
    observe = source_user.index('taskset -pc "$$"')
    assert source_user.index("install_runtime_reader") < affinity < bind < observe < source_user.index("run-source-checks.py")
    selection = source_user[affinity:bind]
    assert 'os.environ.get("KUASAR_BUILD_JOBS", "")' in selection
    assert "jobs = int(budget)" in selection and "os.sched_getaffinity(0)" in selection
    assert "jobs > len(allowed)" in selection and "allowed[:jobs]" in selection
    # Execute the exact workflow block in child shells: successful taskset must
    # affect the actual source process tree, and invalid budgets must stop it.
    block = source_user[affinity:source_user.index("python3 -B /inputs/release/ci/integration/run-source-checks.py")]
    marker = "python3 - <<'AFFINITY_CHILD'\nimport json, os\nprint('AFFINITY_CHILD=' + json.dumps(sorted(os.sched_getaffinity(0))))\nAFFINITY_CHILD\n"
    allowed = sorted(os.sched_getaffinity(0))
    assert allowed
    cases = [(str(jobs), allowed[:jobs]) for jobs in sorted({1, min(2, len(allowed))})]
    cases += [(value, None) for value in (None, "0", "-1", "invalid", str(len(allowed) + 1))]
    for budget, expected in cases:
        env = dict(os.environ)
        env.pop("KUASAR_BUILD_JOBS", None)
        if budget is not None:
            env["KUASAR_BUILD_JOBS"] = budget
        observed = subprocess.run(["bash", "-euo", "pipefail", "-c", block + marker],
                                  env=env, text=True, capture_output=True, timeout=15)
        masks = [line.removeprefix("AFFINITY_CHILD=") for line in observed.stdout.splitlines()
                 if line.startswith("AFFINITY_CHILD=")]
        if expected is None:
            assert observed.returncode != 0 and not masks, (budget, observed.stdout, observed.stderr)
        else:
            assert observed.returncode == 0 and len(masks) == 1, (budget, observed.stdout, observed.stderr)
            assert json.loads(masks[0]) == expected
        assert sorted(os.sched_getaffinity(0)) == allowed, "source test changed parent CPU affinity"
    assert "start --mode system" in system
    assert system.index('chown -hR "$source_uid:$source_gid" /src /work/home /build\n') < system.index("run-source-checks.py")
    assert 'chgrp "$source_gid" /output\n' in system
    assert 'chmod 2775 /output\n' in system
    assert 'setpriv --reuid "$source_uid" --regid "$source_gid" --groups "$source_docker_gid"' in system
    assert '--inh-caps=-all --ambient-caps=-all' in system
    assert 'source_docker_gid=$(stat -c %g /run/docker.sock)' in system
    assert 'docker --host unix:///run/docker.sock info' in system
    assert '[ "$(id -u)" -ne 0 ]' in system
    assert 'visudo -cf /etc/sudoers.d/kuasar-source' in system
    assert "--selection plan/workbench.json" in system
    assert "bash -euo pipefail -c \"$source_script\"" in system
    assert "<<'WORKBENCH_SOURCE'" in system
    assert "sudo -n true" in system
    assert "ip netns add ks-source-probe" in system and "ip netns del ks-source-probe" in system
    assert "ip tuntap add dev ks-source-probe mode tap" in system
    assert "ip link del dev ks-source-probe" in system and "trap - EXIT" in system
    assert "install_readers" in system and "install_runtime_reader" in system
    assert "--materialized" in system and "--phase" not in system
    assert "/output/source-result.json" in system
    assert "cache" not in system
    cleanup = source_steps["Stop the owned system instance and retain safe source evidence"]
    assert cleanup["if"] == "always()" and "workbench.py finish" in cleanup["run"]
    for step in source_job["steps"]:
        assert "GH_TOKEN" not in json.dumps(step) and "create-github-app-token" not in json.dumps(step)
        if "actions/upload-artifact@" in step.get("uses", ""):
            assert "source-checks" not in step["with"]["path"], "candidate sources must not be uploaded directly"
            assert "source-system-evidence" in step["with"]["path"]
    source = (ROOT / "ci/integration/run-source-checks.py").read_text()
    assert "uffd-performance-gate.sh" in source and "ci-source-checks.sh" in source
    runtime = (ROOT / "ci/integration/run-artifact-tests.py").read_text()
    performance = (ROOT / "ci/integration/run-artifact-performance.py").read_text()
    assert 'test/e2e/e2e' in runtime and 'working-set-netns.sh' not in runtime
    assert "working-set-netns.sh" in performance and "PERF_ITERS" in performance
    assert lanes["performance"]["needs"] == ["build", "prepare"]
    assert lanes["result"]["needs"] == ["build", "prepare", "e2e", "performance"]
    for required in ("true", "false", ""):
        context = {"github": {"repository": "kuasar-sandbox/kuasar-sandbox", "event": {"repository": {
                    "visibility": "public", "full_name": "kuasar-sandbox/kuasar-sandbox"}}},
                   "needs": {"build": {"outputs": {"performance": required}}}}
        assert expression(lanes["performance"]["if"], context) == (required == "true")
    print(f"hosted-workflows: {count} non-public allocation checks; independent lanes, exact requests and required checks PASS")


if __name__ == "__main__":
    check()
    for name in ("test-bootstrap.py", "test-exact-assets-tools.py", "test-sparse-runtime.py"):
        subprocess.run([sys.executable, str(Path(__file__).with_name(name))], check=True)
