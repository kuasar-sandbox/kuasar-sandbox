"""Trusted runner profiles and deterministic capability allocation.

No owner metadata can supply a runner label, image, command or priority.
Historical plans retain their original interpretation in artifacts.py.
"""
import json
import os
import re
from pathlib import Path

import artifacts

CONFIG = Path(__file__).with_name('runner-capabilities.json')
STATES = {'supported', 'unsupported', 'unknown'}


def validate_config(config):
    require = artifacts.require
    capabilities = artifacts.capabilities_module().CAPABILITIES
    require(isinstance(config, dict) and set(config) == {'schema', 'profiles'} and type(config['schema']) is int and config['schema'] == 1,
            'unsupported runner capability configuration')
    profiles = config['profiles']
    require(isinstance(profiles, dict) and profiles, 'empty runner profile configuration')
    for name, profile in profiles.items():
        require(re.fullmatch(r'[a-z][a-z0-9-]{0,47}', name), 'invalid runner profile ID')
        require(set(profile) == {'arch', 'public', 'enabled', 'priority', 'runs_on', 'executor',
                                 'capabilities', 'resources'}, 'invalid runner profile fields: ' + name)
        require(profile['arch'] in artifacts.ARCHES and type(profile['public']) is bool and
                type(profile['enabled']) is bool and type(profile['priority']) is int and
                profile['priority'] >= 0, 'invalid runner selection policy: ' + name)
        require(profile['executor'] in {'native', 'workbench-system'}, 'unknown E2E executor')
        labels = profile['runs_on']
        require(isinstance(labels, list) and labels and len(set(labels)) == len(labels) and
                all(isinstance(label, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', label)
                    for label in labels), 'invalid runner labels')
        if profile['public']:
            require(labels == [{'x86_64': 'ubuntu-24.04', 'aarch64': 'ubuntu-24.04-arm'}[profile['arch']]]
                    and profile['executor'] == 'native', 'public E2E profile differs from hosted execution')
        else:
            require(profile['executor'] == 'workbench-system' and 'self-hosted' in labels and
                    {'Linux', {'x86_64': 'X64', 'aarch64': 'ARM64'}[profile['arch']]} <= set(labels),
                    'supplemental E2E requires a native Linux Workbench runner')
        resource = profile['resources']
        require(set(resource) == {'cpus', 'memory_gib', 'timeout_minutes'} and
                all(type(value) is int and value > 0 for value in resource.values()) and
                resource['timeout_minutes'] <= 360, 'invalid E2E resource budget')
        require(set(profile['capabilities']) == capabilities, 'runner profile must classify every capability')
        for capability, fact in profile['capabilities'].items():
            require(isinstance(fact, dict) and set(fact) == {'state', 'evidence'} and
                    fact['state'] in STATES and isinstance(fact['evidence'], str) and fact['evidence'].strip(),
                    'invalid capability evidence: ' + name + '/' + capability)
    return config


def read_config():
    return validate_config(artifacts.capabilities_module().loads(CONFIG.read_text()))


def effective(profile, capability, runtime):
    # The existing runtime-only container has no service manager. A host's
    # capabilities cannot erase the selected executor's limitations.
    if runtime == 'runtime-only' and capability in {'systemd', 'journald'}:
        return 'unsupported'
    return profile['capabilities'][capability]['state']


def allocate(arch, selection, requirements, config):
    validate_config(config)
    profiles = {name: profile for name, profile in config['profiles'].items()
                if profile['arch'] == arch and profile['enabled']}
    public = {name: profile for name, profile in profiles.items() if profile['public']}
    artifacts.require(public, 'no enabled public capability profile for ' + arch)
    groups = {}
    for case in selection['cases']:
        suite = case.split('.')[0]
        runtime = 'runtime-only' if artifacts.requires_clean_runtime(arch, suite) else 'system'
        required = requirements['cases'][case]['requires']
        matches = [name for name, profile in profiles.items()
                   if all(effective(profile, cap, runtime) == 'supported' for cap in required)]
        matches.sort(key=lambda name: (not profiles[name]['public'], profiles[name]['priority'], name))
        diagnostics = {name: {cap: effective(profile, cap, runtime) for cap in required
                             if effective(profile, cap, runtime) != 'supported'}
                       for name, profile in profiles.items()}
        artifacts.require(matches, f'no capable {arch} runner for {case}: {diagnostics}')
        name = matches[0]
        missing = {key: sorted(cap for cap in required if effective(profile, cap, runtime) == 'unsupported')
                   for key, profile in public.items()}
        if not profiles[name]['public']:
            artifacts.require(all(missing.values()),
                              f'public capability is unresolved for {arch}/{case}: {diagnostics}')
        key = name + '--' + suite
        group = groups.setdefault(key, {'profile': name, 'suite': suite, 'runtime': runtime,
                                       'cases': [], 'requires': [], 'public_gaps': {}})
        group['cases'].append(case)
        group['requires'] = sorted(set(group['requires']) | set(required))
        if not profiles[name]['public']:
            group['public_gaps'][case] = missing
    return dict(sorted(groups.items()))


def apply(plan, contract):
    if contract is None:
        # The caller may only produce a historical-format plan for wholly
        # historical inputs. New inputs cannot opt out of the contract.
        return plan
    config = read_config()
    plan.update(schema=4, case_requirements=contract,
                runner_configuration={'sha256': artifacts.identity(config), 'value': config})
    for arch, lane in plan['lanes'].items():
        lane['selection'] = artifacts.suite_selection(plan['owners'], arch, plan['case_files'],
                                                    native_full=True, changes=plan.get('changes'))
        lane['groups'] = allocate(arch, lane['selection'], contract, config)
    return plan


def validate(plan):
    contract = artifacts.capabilities_module().validate(plan['case_requirements'], plan['case_files'])
    frozen = plan['runner_configuration']
    artifacts.require(set(frozen) == {'sha256', 'value'}, 'invalid runner configuration identity')
    config = validate_config(frozen['value'])
    artifacts.require(frozen['sha256'] == artifacts.identity(config), 'runner configuration digest changed')
    if 'execution_run' in plan or 'execution_images' in plan:
        check_execution_inputs(plan)
    for arch, lane in plan['lanes'].items():
        expected = artifacts.suite_selection(plan['owners'], arch, plan['case_files'],
                                            native_full=True, changes=plan.get('changes'))
        artifacts.require(lane['selection'] == expected, 'required coverage differs from selected suites')
        artifacts.require(lane['groups'] == allocate(arch, expected, contract, config),
                          'execution groups differ from deterministic capability allocation')


def matrix(plan, arch):
    artifacts.check_plan(plan)
    if plan['schema'] < 4:
        return [{'id': name, 'runs_on': ['ubuntu-24.04-arm' if arch == 'aarch64' else 'ubuntu-24.04'],
                 'executor': 'native', 'runtime': 'runtime-only' if artifacts.requires_clean_runtime(arch, name) else 'system',
                 'timeout_minutes': 120}
                for name in artifacts.shards(plan['lanes'][arch]['selection'])]
    config = plan['runner_configuration']['value']
    artifacts.require(config == read_config(), 'new execution matrix differs from the trusted framework configuration')
    return [{'id': name, 'runs_on': config['profiles'][group['profile']]['runs_on'],
             'executor': config['profiles'][group['profile']]['executor'], 'runtime': group['runtime'],
             'timeout_minutes': config['profiles'][group['profile']]['resources']['timeout_minutes']}
            for name, group in plan['lanes'][arch]['groups'].items()]


def check_execution_inputs(plan):
    run = plan.get('execution_run', {})
    artifacts.require(set(run) == {'repository', 'run_id', 'run_attempt'} and
                      re.fullmatch(r'kuasar-sandbox/[a-z0-9-]+', run.get('repository', '')) and
                      all(re.fullmatch(r'[1-9][0-9]*', run.get(key, '')) for key in ('run_id', 'run_attempt')),
                      'missing workflow run/attempt identity')
    images = plan.get('execution_images', {})
    artifacts.require(set(images) == set(artifacts.ARCHES), 'missing immutable executor image selection')
    for image in images.values():
        artifacts.require(set(image) == {'reference', 'image_id'} and
                          re.fullmatch(r'ghcr.io/kuasar-sandbox/workbench@sha256:[0-9a-f]{64}', image['reference']) and
                          re.fullmatch(r'sha256:[0-9a-f]{64}', image['image_id']), 'invalid immutable executor image')


def bind_execution(plan, selection, run):
    if plan['schema'] >= 4:
        artifacts.require(selection['framework_sha'] == plan['framework_sha'], 'executor image framework mismatch')
        plan['execution_images'] = {arch: {key: row[key] for key in ('reference', 'image_id')}
                                    for arch, row in selection['architectures'].items()}
        plan['execution_run'] = run
        check_execution_inputs(plan)
        artifacts.check_plan(plan)
    return plan


def runner_identity(plan, arch, name):
    group = plan['lanes'][arch]['groups'][name]
    profile = plan['runner_configuration']['value']['profiles'][group['profile']]
    result = {key: os.environ.get(variable, '') for key, variable in (
        ('repository', 'GITHUB_REPOSITORY'), ('run_id', 'GITHUB_RUN_ID'), ('run_attempt', 'GITHUB_RUN_ATTEMPT'),
        ('job', 'GITHUB_JOB'), ('name', 'RUNNER_NAME'), ('os', 'RUNNER_OS'), ('arch', 'RUNNER_ARCH'),
        ('environment', 'EXECUTION_RUNNER_ENVIRONMENT'))}
    check_runner(plan, arch, profile, result)
    return result


def check_runner(plan, arch, profile, runner):
    check_execution_inputs(plan)
    artifacts.require(set(runner) == {'repository', 'run_id', 'run_attempt', 'job', 'name', 'os', 'arch', 'environment'}
                      and all(runner.get(key) == plan['execution_run'][key] for key in ('repository', 'run_id')) and
                      isinstance(runner.get('run_attempt'), str) and
                      re.fullmatch(r'[1-9][0-9]*', runner['run_attempt']) and
                      int(runner['run_attempt']) >= int(plan['execution_run']['run_attempt']) and
                      runner['os'] == 'Linux' and runner['arch'] == {'x86_64': 'X64', 'aarch64': 'ARM64'}[arch] and
                      runner['environment'] == ('github-hosted' if profile['public'] else 'self-hosted') and
                      bool(runner['job']) and bool(runner['name']), 'actual runner differs from the selected execution identity')
    # A partial rerun carries successful producers of this exact plan. Failed
    # or cancelled current jobs are rejected by the workflow gate before collection.
    if (os.environ.get('GITHUB_REPOSITORY') == runner['repository'] and
            os.environ.get('GITHUB_RUN_ID') == runner['run_id']):
        artifacts.require(int(runner['run_attempt']) <= int(os.environ['GITHUB_RUN_ATTEMPT']),
                          'execution receipt belongs to a future workflow attempt')


def check_result(plan, arch, name, result):
    group = plan['lanes'][arch]['groups'][name]
    profile = plan['runner_configuration']['value']['profiles'][group['profile']]
    receipt = result.get('execution', {})
    artifacts.require(receipt.get('profile') == group['profile'] and
                      receipt.get('configuration_sha256') == plan['runner_configuration']['sha256'] and
                      receipt.get('executor') == profile['executor'], 'execution profile/configuration changed')
    check_runner(plan, arch, profile, receipt.get('runner', {}))
    # Import only trusted runtime code, not a prepared candidate implementation.
    import sys
    sys.path.insert(0, str(artifacts.ROOT / 'test/e2e/lib'))
    import capability_probe
    artifacts.require(result.get('capabilities') == {cap: capability_probe.OPERATIONS[cap] for cap in group['requires']},
                      'missing required capability startup evidence')
    if profile['executor'] == 'workbench-system':
        artifacts.require(receipt.get('image_id') == plan['execution_images'][arch]['image_id'],
                          'Workbench executor image differs from immutable selection')
    else:
        artifacts.require('image_id' not in receipt, 'native execution cannot claim a Workbench image')
    if group['runtime'] == 'runtime-only':
        artifacts.require(result.get('environment', {}).get('image_id') ==
                          result.get('preparation_environment', {}).get('image_id'),
                          'execution must use the transported preparation runtime image')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--selection', required=True, type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    bind_execution(plan, json.loads(args.selection.read_text()),
                   {key: os.environ.get(variable, '') for key, variable in (
                       ('repository', 'GITHUB_REPOSITORY'), ('run_id', 'GITHUB_RUN_ID'), ('run_attempt', 'GITHUB_RUN_ATTEMPT'))})
    args.plan.write_bytes(artifacts.canonical(plan) + b'\n')
