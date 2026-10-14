#!/usr/bin/env python3
"""Complete Workbench group coverage and archived/current Actions authorization."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

import artifacts
import actions_jobs

SUITES = {'A': {'basic', 'image', 'orchestrator', 'sandbox', 'snapshot'},
          'B': {'builder', 'network', 'storage', 'telemetry'}}
COMMON = ('arch', 'aggregate_version', 'source_revision', 'archive', 'image_id', 'sha256', 'size',
          'compression_seconds', 'plan_id', 'framework_sha', 'test_revisions', 'input_assets')
FORMAT = 'workbench-groups-v1'
require = artifacts.require


def group_cases(case_files):
    cases = artifacts.workbench_cases(case_files, 'x86_64', native_full=True)
    require(cases and len(cases) == len(set(cases)), 'missing or duplicate standard runtime inventory')
    groups = {name: [case for case in cases if case.split('.')[0] in suites] for name, suites in SUITES.items()}
    require(set(groups['A']).isdisjoint(groups['B']) and set(groups['A'] + groups['B']) == set(cases),
            'unassigned runtime suite; use full execution or explicitly assign the suite')
    require(all(groups.values()), 'runtime grouping requires both nonempty groups')
    return groups


def artifact_name(run_id, attempt, group):
    return f'integration-workbench-result-x86_64-{group}-{run_id}-{attempt}'


def latest_jobs(jobs, run=None):
    return actions_jobs.latest_producers(jobs,
        run_id=run['id'] if run is not None else None,
        run_attempt=run['run_attempt'] if run is not None else None)


def group_jobs(jobs, run=None):
    result = {}
    for name, job in latest_jobs(jobs, run).items():
        match = re.search(r'(?:^| / )workbench-release \(x86_64, (A|B)\)$', name)
        if match:
            require(match[1] not in result, 'duplicate runtime group producer')
            result[match[1]] = job
    require(set(result) == set(SUITES), 'missing runtime group producer')
    return result


def producer_proofs(run, jobs, available):
    require(type(run.get('id')) is int and type(run.get('run_attempt')) is int and run['run_attempt'] > 0,
            'missing runtime Actions run identity')
    require(re.fullmatch('[0-9a-f]{40}', run.get('head_sha', '')), 'missing runtime Actions head')
    proofs = {}
    for group, job in group_jobs(jobs, run).items():
        require(job.get('run_id') == run['id'] and job['run_attempt'] <= run['run_attempt'] and
                job.get('status') == 'completed' and job.get('conclusion') == 'success',
                'runtime group producer did not succeed for this run')
        name = artifact_name(run['id'], job['run_attempt'], group)
        rows = [row for row in available if row.get('name') == name]
        require(len(rows) == 1 and rows[0].get('expired') is False, 'missing or duplicate runtime group artifact')
        row = rows[0]
        require(row.get('workflow_run', {}).get('id') == run['id'] and
                row['workflow_run'].get('head_sha') == run['head_sha'], 'runtime artifact run/head mismatch')
        require(job['started_at'] <= row.get('created_at', '') <= job['completed_at'], 'runtime artifact attempt mismatch')
        proofs[group] = {key: job[key] for key in ('id', 'name', 'run_attempt', 'runner_name')}
        proofs[group].update(artifact_id=row['id'], artifact_name=name)
    return proofs


def arm_proof(run, jobs, available):
    rows = [job for name, job in latest_jobs(jobs, run).items()
            if name.endswith(' / workbench-release (aarch64, all)')]
    require(len(rows) == 1, 'missing ARM runtime artifact producer')
    job = rows[0]
    require(job.get('status') == 'completed' and job.get('conclusion') == 'success' and job.get('run_id') == run['id'],
            'ARM runtime artifact producer did not succeed')
    name = f"integration-workbench-result-aarch64-all-{run['id']}-{job['run_attempt']}"
    archives = [row for row in available if row.get('name') == name]
    require(len(archives) == 1 and archives[0].get('expired') is False and
            archives[0].get('workflow_run', {}).get('id') == run['id'] and
            archives[0]['workflow_run'].get('head_sha') == run['head_sha'] and
            job['started_at'] <= archives[0].get('created_at', '') <= job['completed_at'],
            'missing or stale ARM runtime artifact')
    return {key: job[key] for key in ('id', 'name', 'run_attempt')} | dict(artifact_id=archives[0]['id'], artifact_name=name)


def input_proofs(plan, run, jobs, available):
    latest = latest_jobs(jobs, run)
    proofs = []
    for name, artifact in [('stage', f"aggregate-stage-{plan['baseline']['version']}-{run['id']}"),
                           ('release-asset-validation / resolve', f"integration-plan-{run['id']}")]:
        job = latest.get(name, {})
        require(job.get('run_id') == run['id'] and job.get('status') == 'completed' and
                job.get('conclusion') == 'success', 'missing successful exact input producer')
        rows = [row for row in available if row.get('name') == artifact]
        require(len(rows) == 1 and rows[0].get('expired') is False and
                rows[0].get('workflow_run', {}).get('id') == run['id'] and
                rows[0]['workflow_run'].get('head_sha') == run['head_sha'] and
                job['started_at'] <= rows[0].get('created_at', '') <= job['completed_at'],
                'exact input artifact has another producer attempt')
        proofs.append({key: job[key] for key in ('id', 'name', 'run_attempt')} |
                      dict(artifact_id=rows[0]['id'], artifact_name=artifact))
    return proofs


def check_record(version, source_sha, record, *, arch, expected_assets, case_files):
    require(arch == 'x86_64' and record.get('result_format') == FORMAT and
            record.get('qualification_scope') == 'native-grouped' and record.get('conclusion') == 'success',
            'unsupported grouped runtime result')
    require(all(key in record for key in COMMON), 'incomplete grouped runtime identity')
    for key in ('plan_id',):
        require(re.fullmatch('[0-9a-f]{64}', record.get(key, '')), 'missing runtime plan identity')
    require(re.fullmatch('[0-9a-f]{40}', record.get('framework_sha', '')), 'missing runtime framework identity')
    expected = group_cases(case_files)
    rows = record.get('runtime_groups')
    require(isinstance(rows, list) and [row.get('group') for row in rows] == ['A', 'B'], 'missing or duplicate runtime group')
    auth = record.get('authorization', {})
    require(auth.get('repository') == 'kuasar-sandbox/kuasar-sandbox' and
            type(auth.get('run_id')) is int and auth['run_id'] > 0 and
            type(auth.get('run_attempt')) is int and auth['run_attempt'] > 0 and
            type(auth.get('validator_job_id')) is int and auth['validator_job_id'] > 0 and
            re.fullmatch('[0-9a-f]{40}', auth.get('head_sha', '')), 'incomplete runtime authorization')
    require(auth['head_sha'] == record['framework_sha'], 'runtime run head differs from executor')
    arm = auth.get('arm', {})
    require(type(arm.get('id')) is int and arm['id'] > 0 and type(arm.get('artifact_id')) is int and
            arm['artifact_id'] > 0 and type(arm.get('run_attempt')) is int and 0 < arm['run_attempt'] <= auth['run_attempt'] and
            arm.get('artifact_name') == f"integration-workbench-result-aarch64-all-{auth['run_id']}-{arm['run_attempt']}" and
            arm.get('name', '').endswith(' / workbench-release (aarch64, all)') and
            re.fullmatch('[0-9a-f]{64}', auth.get('arm_result_sha256', '')), 'missing bound ARM producer/result')
    inputs = auth.get('inputs', [])
    require([row.get('name') for row in inputs] == ['stage', 'release-asset-validation / resolve'] and
            all(type(row.get('id')) is int and row['id'] > 0 and type(row.get('artifact_id')) is int and
                row['artifact_id'] > 0 and type(row.get('run_attempt')) is int and
                0 < row['run_attempt'] <= auth['run_attempt'] for row in inputs), 'missing exact input producer binding')
    instances, containers, machines, job_ids, artifact_ids = set(), set(), set(), set(), set()
    for row in rows:
        group = row['group']
        require(not set(COMMON) & row.keys(), 'group cannot override shared source/assets identity')
        require(row.get('qualification_scope') == 'native-full', 'group must execute its complete native selection')
        require(row.get('cpus') == 2 and row.get('memory_gib') == 8 and row.get('cleanup_exit_code') == 0,
                'runtime group budget or cleanup mismatch')
        require(row.get('prepared_cases_sha256') == artifacts.identity(artifacts.workbench_cases(case_files, arch, native_full=True)),
                'each runtime group must prepare the complete ordinary offline inventory')
        producer = row.get('producer', {})
        require(all(item['run_attempt'] <= producer.get('run_attempt', 0) for item in inputs),
                'runtime group predates current exact inputs')
        require(type(producer.get('run_attempt')) is int and 0 < producer['run_attempt'] <= auth['run_attempt'] and
                producer.get('artifact_name') == artifact_name(auth['run_id'], producer['run_attempt'], group) and
                re.search(r'(?:^| / )workbench-release \(x86_64, ' + group + r'\)$', producer.get('name', '')),
                'runtime producer group/run/attempt mismatch')
        for value, seen, kind in ((row.get('instance_id'), instances, str), (row.get('container_id'), containers, str),
                                  (producer.get('runner_name'), machines, str), (producer.get('id'), job_ids, int),
                                  (producer.get('artifact_id'), artifact_ids, int)):
            require(type(value) is kind and value and value not in seen, 'missing or shared runtime instance/job/artifact')
            seen.add(value)
        full = {key: record[key] for key in COMMON} | row
        artifacts.check_workbench_record(version, source_sha, full, arch=arch, expected_assets=expected_assets,
                                         case_files=case_files, selected_cases=expected[group])
    return record


def aggregate(plan, results, run, jobs, available, validator_job_id, arm_result):
    require(isinstance(results, list) and len(results) == 2 and {row.get('group') for row in results} == set(SUITES),
            'missing or duplicate runtime results')
    results = sorted(results, key=lambda row: row['group'])
    common = {key: results[0][key] for key in COMMON}
    require(common['plan_id'] == artifacts.identity(plan) and common['framework_sha'] == plan['framework_sha'] and
            common['test_revisions'] == plan['test_revisions'], 'runtime result belongs to another plan/source set')
    proofs = producer_proofs(run, jobs, available)
    grouped = []
    for result in results:
        require({key: result[key] for key in COMMON} == common, 'runtime groups differ in source/assets/image identity')
        grouped.append({key: value for key, value in result.items() if key not in COMMON} |
                       {'producer': proofs[result['group']]})
    record = common | dict(result_format=FORMAT, qualification_scope='native-grouped', conclusion='success',
                           runtime_groups=grouped, authorization=dict(repository='kuasar-sandbox/kuasar-sandbox',
                           run_id=run['id'], run_attempt=run['run_attempt'], head_sha=run['head_sha'],
                           validator_job_id=validator_job_id, inputs=input_proofs(plan, run, jobs, available),
                           arm=arm_proof(run, jobs, available), arm_result_sha256=artifacts.identity(arm_result)))
    return check_record(plan['baseline']['version'], plan['baseline']['sha'], record, arch='x86_64',
                        expected_assets={item['name']:item['digest'] for item in plan['baseline']['assets'] if item['name'] != 'SHA256SUMS'},
                        case_files=plan['case_files'])


def verify_current(record, run, jobs):
    """Archived artifact IDs survive cleanup; latest successful producer jobs must still agree."""
    auth = record['authorization']
    require(run.get('id') == auth['run_id'] and run.get('head_sha') == auth['head_sha'] and
            run.get('run_attempt', 0) >= auth['run_attempt'] and
            run.get('event') == 'workflow_dispatch' and run.get('path') == '.github/workflows/aggregate-release.yml',
            'grouped evidence has another current run/source/authorization')
    current = group_jobs(jobs, run)
    latest = latest_jobs(jobs, run)
    for proof in [*auth['inputs'], auth['arm']]:
        job = latest.get(proof['name'], {})
        require(all(job.get(key) == proof[key] for key in ('id', 'name', 'run_attempt')) and
                job.get('status') == 'completed' and job.get('conclusion') == 'success',
                'input producer rerun invalidated runtime validation')
    for row in record['runtime_groups']:
        job, proof = current[row['group']], row['producer']
        require(all(job.get(key) == proof.get(key) for key in ('id', 'name', 'run_attempt', 'runner_name')) and
                job.get('status') == 'completed' and job.get('conclusion') == 'success',
                'runtime producer rerun invalidated prior validation')
    validators = [job for job in latest_jobs(jobs, run).values() if job.get('id') == auth['validator_job_id']]
    require(len(validators) == 1 and validators[0].get('status') == 'completed' and
            validators[0].get('conclusion') == 'success' and
            validators[0].get('name', '').endswith(' / results'), 'runtime validation job is stale or unsuccessful')
    require(all(job['completed_at'] <= validators[0]['started_at'] for job in [*current.values(), latest[auth['arm']['name']]]),
            'runtime validation predates the latest producer')


def api(endpoint):
    return json.loads(subprocess.check_output(['gh', 'api', endpoint]))


def pages(endpoint, key):
    # Compatible with older operator gh as well as current Hosted gh.
    raw = subprocess.check_output(['gh', 'api', '--paginate', endpoint]).decode()
    decoder, result = json.JSONDecoder(), []
    while raw.strip():
        document, end = decoder.raw_decode(raw.lstrip())
        result.extend(document[key])
        raw = raw.lstrip()[end:]
    return result


def verify_live(record):
    if record.get('qualification_scope') != 'native-grouped':
        return
    auth = record['authorization']
    prefix = f'repos/{auth["repository"]}/actions/runs/{auth["run_id"]}'
    verify_current(record, api(prefix), pages(prefix + '/jobs?filter=all&per_page=100', 'jobs'))


def recovery_order(jobs):
    if not any(re.search(r'workbench-release \(x86_64, [AB]\)$', job.get('name', '')) for job in jobs):
        return
    producers = group_jobs(jobs)
    latest = latest_jobs(jobs)
    prerequisites = [latest.get(name, {}) for name in ('stage', 'release-asset-validation / resolve',
        'release-asset-validation / workbench-release (aarch64, all)')]
    validators = [job for name, job in latest_jobs(jobs).items() if name == 'release-asset-validation / results']
    require(len(validators) == 1 and validators[0].get('conclusion') == 'success' and
            all(job.get('conclusion') == 'success' and job.get('status') == 'completed' and
                job['completed_at'] <= validators[0]['started_at'] for job in [*producers.values(), *prerequisites]),
            'cannot reuse validation after a runtime producer rerun')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    artifacts.check_plan(plan)
    repo, run_id = os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_RUN_ID']
    require(repo == 'kuasar-sandbox/kuasar-sandbox', 'grouped qualification requires the platform caller')
    prefix = f'repos/{repo}/actions/runs/{run_id}'
    run = api(prefix)
    require(str(run['run_attempt']) == os.environ['GITHUB_RUN_ATTEMPT'] and run.get('event') == 'workflow_dispatch'
            and run.get('path') == '.github/workflows/aggregate-release.yml', 'unexpected grouped qualification caller')
    jobs = pages(prefix + '/jobs?filter=all&per_page=100', 'jobs')
    available = pages(prefix + '/artifacts?per_page=100', 'artifacts')
    proofs = producer_proofs(run, jobs, available)
    results = []
    for group in ('A', 'B'):
        directory = args.results / proofs[group]['artifact_name']
        paths = list(directory.rglob('x86_64-' + group + '.json'))
        require(len(paths) == 1 and not paths[0].is_symlink(), 'missing or duplicate runtime group result file')
        results.append(json.loads(paths[0].read_text()))
    validator = [job for name, job in latest_jobs(jobs, run).items() if name == 'release-asset-validation / results'
                 and job.get('run_attempt') == run['run_attempt'] and job.get('status') == 'in_progress']
    require(len(validator) == 1, 'missing current runtime validator identity')
    arm = arm_proof(run, jobs, available)
    arm_path = args.results / arm['artifact_name'] / 'aarch64-all.json'
    require(arm_path.is_file() and not arm_path.is_symlink(), 'missing unchanged ARM artifact qualification')
    arm_result = json.loads(arm_path.read_text())
    record = aggregate(plan, results, run, jobs, available, validator[0]['id'], arm_result)
    artifacts.check_workbench_results(plan['baseline']['version'], plan['baseline']['sha'],
        {'x86_64': record, 'aarch64': arm_result}, expected_assets=record['input_assets'], case_files=plan['case_files'])
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'x86_64.json').write_bytes(artifacts.canonical(record) + b'\n')
    (args.output / 'aarch64.json').write_bytes(artifacts.canonical(arm_result) + b'\n')


if __name__ == '__main__':
    main()
