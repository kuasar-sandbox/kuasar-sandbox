#!/usr/bin/env python3
"""Run owner release gates and native declared recipes on fixed source inputs."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import time

import artifacts
import build_helpers
import source_inputs

spec = importlib.util.spec_from_file_location('source_checks', Path(__file__).with_name('run-source-checks.py'))
source_checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source_checks)
helpers = build_helpers.helper_inputs


def read(path):
    artifacts.require(path.is_file() and not path.is_symlink(), 'missing regular frozen input: ' + str(path))
    return helpers.contract.loads(path.read_text())


def requested(sources, env):
    return (any((sources / name).exists() or (sources / name).is_symlink()
                for name in ('.source-plan.json', '.source-checks.json')) or
            any(env.get(key) for key in ('REQUIRE_WORKING_SET_PRIVILEGED', 'KUASAR_WORKSPACE_ROOT',
                                         'KUASAR_WORKBENCH_IMAGE_ID', 'KUASAR_WORKBENCH_FRAMEWORK_SHA', 'KUASAR_CI_DIR')))


def identities(plan, records):
    """Keep the admitted named ref/tag, including two tags at the same commit."""
    result = {}
    for owner, pin in records.items():
        candidates = [row for row in plan.get('run_inputs', {}).values()
                      if row.get('repository') == pin['repository'] and row.get('sha') == pin['sha']]
        if pin['role'] in ('candidate', 'companion'):
            admitted = [row for row in plan.get('candidate_records', [])
                        if row['repository'] == pin['repository'] and row['candidate_sha'] == pin['sha']]
            artifacts.require(len(admitted) == 1, 'missing unique candidate admission: ' + owner)
            ref = f"refs/pull/{admitted[0]['pull_request_number']}/merge"
            candidates = [row for row in candidates if row.get('ref') == ref and 'tag' not in row]
        elif owner != 'platform':
            unit = 'runtime' if owner == 'guest-runtime' else owner
            selected = plan['source_records'][unit]
            candidates = [row for row in candidates if all(row.get(key) == value for key, value in selected.items())]
        facts = [{key: row[key] for key in ('repository', 'sha', 'tree', 'ref', 'tag') if key in row}
                 for row in candidates]
        # A tag record may also carry its equivalent refs/tags spelling.
        for row in facts:
            if 'tag' in row:
                artifacts.require(row.get('ref', 'refs/tags/' + row['tag']) == 'refs/tags/' + row['tag'],
                                  'tag/ref mismatch')
                row.pop('ref', None)
        unique = {artifacts.canonical(row) for row in facts}
        artifacts.require(len(unique) == 1, 'ambiguous or absent fixed owner source: ' + owner)
        result[owner] = facts[0]
        artifacts.require('tag' in facts[0] or re.fullmatch(
            r'refs/(heads/(main|release/v[0-9]+\.[0-9]+\.x)|pull/[1-9][0-9]*/merge)', facts[0].get('ref', '')),
            'owner lacks admitted named source')
    return result


def validate(plan_path, sources, env):
    plan = read(plan_path)
    plan_id = artifacts.check_plan(plan)
    artifacts.require(plan['mode'] == 'source', 'release source gates require a source plan')
    selected, records = source_checks.source_records(plan)
    artifacts.require(read(sources / '.source-checks.json') == {'plan_id': plan_id, 'sources': records},
                      'source materialization receipt differs from original plan')
    if env.get('KUASAR_WORKBENCH_FRAMEWORK_SHA'):
        artifacts.require(env['KUASAR_WORKBENCH_FRAMEWORK_SHA'] == plan['framework_sha'], 'framework identity changed')
    if env.get('KUASAR_WORKSPACE_ROOT'):
        artifacts.require(Path(env['KUASAR_WORKSPACE_ROOT']).resolve() == sources, 'workspace root changed')
    facts = identities(plan, records)
    for owner, row in facts.items():
        observed = source_inputs.inspect_checkout(sources / owner, row['repository'], row['sha'])
        artifacts.require(observed['tree'] == row['tree'], 'fixed source tree changed: ' + owner)
        helpers.inspect(sources / owner, row)
    for owner in sorted(selected - {'platform'}):
        makefile = helpers.contract.regular(sources / owner, 'Makefile').read_text()
        artifacts.require(re.search(r'^test-release\s*:(?!=)', makefile, re.M), 'missing original release entry: ' + owner)
    rows = recipes(plan, selected, facts, records, sources)
    return plan, plan_id, selected, facts, rows


def recipes(plan, selected, facts, records, sources):
    contract = plan.get('helper_contract')
    if contract is not None:
        helpers.contract.validate(contract)
    rows = {}
    for owner in sorted(selected):
        if contract is not None:
            row = contract['owners'][owner]
            artifacts.require(row['source'] == facts[owner], 'helper owner differs from frozen source')
            if row.get('historical'):
                artifacts.require(owner not in plan['owners'] and records[owner]['role'] == 'baseline',
                                  'candidate cannot use historical helper evidence')
                rows[owner] = row
                continue
        row = helpers.owner(sources / owner, facts[owner])
        if contract is not None:
            artifacts.require(row == contract['owners'][owner], 'helper declaration differs from original plan')
        declaration = helpers.contract.declaration(row['declaration'])
        for dependency in declaration['dependencies']:
            artifacts.require(dependency in facts, 'missing admitted helper dependency: ' + dependency)
            if contract is not None:
                artifacts.require(contract['owners'][dependency]['source'] == facts[dependency], 'helper dependency changed')
        rows[owner] = row
    if contract is None and set(rows) == set(artifacts.OWNERS):
        # Separate regression evidence; the original trusted schema2 plan is unchanged.
        regression_contract = {'schema': 1, 'owners': rows}
        helpers.contract.validate(regression_contract)
        for lane in plan['lanes'].values():
            artifacts.planned_helpers(lane['selection'], regression_contract)
    return rows


def execute(plan_path, sources, output, env):
    result = {'conclusion': 'failure', 'exit_code': 1, 'commands': []}
    scratch = None
    artifacts.require(not any(output.resolve().is_relative_to(sources / owner) for owner in artifacts.OWNERS),
                      'release evidence must be outside owner sources')
    artifacts.require(not output.exists() and not output.is_symlink(), 'release gate evidence already exists')
    output.mkdir(parents=True)
    def save():
        (output / 'result.json').write_bytes(artifacts.canonical(result) + b'\n')
    def run(name, command, directory, environment):
        row = {'name': name, 'command': command, 'directory': str(directory), 'exit_code': None,
               'state': 'running', 'log': f'{len(result["commands"]):02d}.log'}
        result['commands'].append(row); save()
        print('release source gate: ' + name, flush=True)
        start = time.monotonic()
        try:
            with (output / row['log']).open('wb') as log:
                completed = subprocess.run(command, cwd=directory, env=environment, stdout=log, stderr=subprocess.STDOUT)
            row['exit_code'] = completed.returncode
            row['state'] = 'completed'
            row['log_sha256'] = artifacts.digest(output / row['log'])
            if completed.returncode:
                result['exit_code'] = completed.returncode
                raise subprocess.CalledProcessError(completed.returncode, command)
        finally:
            row['seconds'] = time.monotonic() - start
            save()
    try:
        source_checks.environment()  # No control credentials enter owner commands.
        plan, plan_id, selected, facts, rows = validate(plan_path, sources, env)
        result.update(plan_id=plan_id, framework_sha=plan['framework_sha'], sources=facts,
                      selected_owners=sorted(selected), native_arch=platform.machine())
        regression = {'plan_id': plan_id, 'owners': rows}
        if plan.get('helper_contract', {}).get('schema') == 2:
            regression['historical_contract'] = plan['helper_contract']
        (output / 'inputs.json').write_bytes(artifacts.canonical(regression) + b'\n')
        result['inputs_sha256'] = artifacts.identity(regression)
        arch = platform.machine()
        artifacts.require(arch in artifacts.ARCHES and env.get('TARGET_ARCH', arch) == arch,
                          'release source recipes require the native architecture')
        # Existing workspace only: do not create or repair it to hide changed inputs.
        modules = [owner for owner in facts if (sources / owner / 'go.mod').is_file()]
        work = sources / 'go.work'
        before_work = work.read_bytes() if work.is_file() and not work.is_symlink() else None
        artifacts.require(not work.is_symlink(), 'linked Go workspace input')
        if modules:
            artifacts.require(work.is_file() and not work.is_symlink() and env.get('GOWORK') == str(work),
                              'missing fixed Go workspace')
            before_work = work.read_bytes()
            result['workspace_verification'] = source_checks.new_result(plan, 'verify')
            source_checks.workspace(plan, sources, result['workspace_verification'], dict(env))
            result['workspace_sha256'] = helpers.contract.digest(before_work)
        def unchanged():
            artifacts.require(validate(plan_path, sources, env) == (plan, plan_id, selected, facts, rows),
                              'frozen source evidence changed during release gates')
            artifacts.require((work.read_bytes() if work.exists() else None) == before_work,
                              'fixed Go workspace changed during release gates')
            artifacts.require(not work.is_symlink(), 'linked Go workspace input')
        scratch = tempfile.TemporaryDirectory(prefix='release-python-')
        artifacts.require(not Path(scratch.name).resolve().is_relative_to(sources), 'Python cache is inside frozen sources')
        # Explicit py_compile writes even with -B. Its disposable cache must not
        # modify admitted sources; cleanliness assertions remain unconditional.
        selected_env = dict(env, PYTHONPYCACHEPREFIX=str(Path(scratch.name) / 'bytecode'), KUASAR_WORKSPACE_ROOT=str(sources), KUASAR_PLATFORM_ROOT=str(sources / 'platform'),
                            TARGET_ARCH=arch, PYTHONDONTWRITEBYTECODE='1')
        for owner in sorted(selected - {'platform'}):
            try:
                run(owner + '-release', ['make', 'test-release'], sources / owner, selected_env)
            finally:
                unchanged()
        result['helpers'] = {}
        for owner, row in rows.items():
            if row.get('historical'):
                result['helpers'][owner] = {'status': 'retained-historical-evidence', 'source': row['source']}
                save(); continue
            with tempfile.TemporaryDirectory(prefix='release-helpers-') as directory:
                built = Path(directory) / 'bin'
                artifacts.require(not built.resolve().is_relative_to(sources), 'recipe output is inside frozen sources')
                built.mkdir()
                try:
                    outputs = build_helpers.build_owner(sources / owner, arch, built, selected_env, row,
                        lambda cmd, cwd, e: run(owner + '-helpers', cmd, cwd, e))
                    result['helpers'][owner] = {'status': 'validated-native-recipe' if outputs else 'declared-empty',
                                               'outputs': outputs, 'dependencies': {
                        name: facts[name] for name in helpers.contract.declaration(row['declaration'])['dependencies']}}
                finally:
                    unchanged()
            save()
        result.update(conclusion='success', exit_code=0)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        result['error'] = str(error)
        if isinstance(error, subprocess.CalledProcessError): result['exit_code'] = error.returncode
        elif isinstance(error, FileNotFoundError): result['exit_code'] = 127
    finally:
        save()
        if scratch is not None:
            scratch.cleanup()
    status = result['exit_code']
    return status if status >= 0 else 128 - status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--auto', action='store_true', help='activate only in existing frozen-source context')
    args = parser.parse_args()
    artifacts.require(args.output is None or str(args.output) != '.', 'explicit output directory is required')
    sources = args.sources.resolve()
    if args.auto and args.plan is None and args.output is None and not requested(sources, os.environ):
        print('Platform tooling only: frozen source-set release validation was not requested.')
        return 0
    artifacts.require(not args.auto or args.plan is None and args.output is None, 'explicit paths cannot auto-skip')
    artifacts.require(args.auto or args.plan is not None and args.output is not None, 'explicit gate needs plan and output')
    artifacts.require(Path(__file__).resolve().parents[2] == sources / 'platform', 'gate is not the admitted platform source')
    plan = args.plan or sources / '.source-plan.json'
    parent = Path(os.environ.get('KUASAR_CI_DIR', str(sources / '.release-checks')))
    output = args.output or parent / ('release-gates-' + str(time.time_ns()))
    return execute(plan, sources, output, dict(os.environ))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print('release source gates: ' + str(error), file=sys.stderr)
        sys.exit(1)
