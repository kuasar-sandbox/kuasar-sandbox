#!/usr/bin/env python3
"""Collect source gates using current Actions jobs and immutable artifact IDs."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

import artifacts

spec = importlib.util.spec_from_file_location('source_checks', Path(__file__).with_name('run-source-checks.py'))
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)
require = artifacts.require


def groups_for(plan, grouped=False):
    return ['A', 'B'] if grouped and checks.checks_for(plan, Path('/src'), 'B') else ['A'] if grouped else ['all']


def artifact_name(run_id, attempt, group):
    return f'integration-source-result-{run_id}-{attempt}-{group}'


def validate(plan, groups, run, jobs, available, read):
    """Earlier successful jobs are reusable only while they remain the latest producer.

    Query filter=all: a newer failed/running/cancelled producer invalidates old
    evidence. Never choose the newest *successful* job or merge ZIP directories.
    """
    require(groups in (['all'], groups_for(plan, True)), 'unexpected source group plan')
    require(type(run.get('id')) is int and run['id'] > 0 and
            type(run.get('run_attempt')) is int and run['run_attempt'] > 0 and
            re.fullmatch('[0-9a-f]{40}', run.get('head_sha', '')), 'missing Actions run identity')
    selected, seen = {}, set()
    for job in jobs:
        match = re.search(r'(?:^| / )source-checks \((all|A|B)\)$', job.get('name', ''))
        if not match:
            continue
        group = match[1]
        require(group in groups, 'unexpected source producer')
        require(job.get('run_id') == run['id'] and type(job.get('run_attempt')) is int
                and 0 < job['run_attempt'] <= run['run_attempt'], 'source job run/attempt mismatch')
        identity = (group, job['run_attempt'])
        require(identity not in seen, 'duplicate source producer')
        seen.add(identity)
        old = selected.get(group)
        if old is None or old['run_attempt'] < job['run_attempt']:
            selected[group] = job
    require(set(selected) == set(groups), 'missing source producer')
    results, instances, containers, runners = [], set(), set(), set()
    for group in groups:
        job = selected[group]
        require(job.get('status') == 'completed' and job.get('conclusion') == 'success',
                'source producer did not succeed: ' + group)
        require(type(job.get('id')) is int and job['id'] > 0 and job.get('runner_name'), 'missing source job identity')
        name = artifact_name(run['id'], job['run_attempt'], group)
        matches = [row for row in available if row.get('name') == name]
        require(len(matches) == 1, 'missing or duplicate source artifact: ' + name)
        archive = matches[0]
        require(type(archive.get('id')) is int and archive['id'] > 0 and not archive.get('expired', True),
                'invalid source artifact')
        require(archive.get('workflow_run', {}).get('id') == run['id'] and
                archive['workflow_run'].get('head_sha') == run['head_sha'], 'source artifact run/head mismatch')
        require(job['started_at'] <= archive.get('created_at', '') <= job['completed_at'],
                'source artifact was not created by this producer attempt')
        data = read(archive['id'])
        result, receipt, instance = data['result'], data['receipt'], data['instance']
        require(result.get('plan_id') == artifacts.identity(plan) and
                result.get('framework_sha') == plan['framework_sha'] and
                result.get('test_revisions') == plan['test_revisions'] and
                result.get('sources') == checks.source_records(plan)[1], 'source result identity mismatch')
        require(result.get('phase') == 'all' and result.get('group') == group and
                result.get('arch') == 'x86_64' and result.get('conclusion') == 'success' and
                result.get('exit_code') == 0, 'source result failed or empty')
        expected = checks.checks_for(plan, Path('/src'), group)
        observed = result.get('checks', [])
        require([(row.get('name'), row.get('command'), row.get('directory')) for row in observed] ==
                [(name, command, str(directory)) for name, command, directory in expected] and
                all(row.get('exit_code') == 0 for row in observed), 'source command coverage mismatch')
        require(receipt.get('conclusion') == 'success' and receipt.get('cleanup_exit_code') == 0 and
                receipt.get('delete_output_exit_code') == 0 and receipt.get('mode') == 'system' and
                receipt.get('framework_sha') == plan['framework_sha'] and
                receipt.get('arch') == 'x86_64' and receipt.get('cpus') == 2 and receipt.get('memory_gib') == 8,
                'source instance/budget/cleanup mismatch')
        require(receipt.get('commands') and all(row.get('exit_code') == 0 for row in receipt['commands']),
                'incomplete source instance execution')
        require(result.get('workbench') == {'image_id': receipt.get('image', {}).get('image_id'),
                'framework_sha': plan['framework_sha']} and
                re.fullmatch('sha256:[0-9a-f]{64}', result['workbench'].get('image_id', '')),
                'source Workbench identity mismatch')
        iid, cid, runner = instance.get('id'), instance.get('container_id'), job['runner_name']
        require(isinstance(iid, str) and iid and isinstance(cid, str) and cid and
                iid not in instances and cid not in containers and runner not in runners,
                'missing or shared source instance/machine')
        instances.add(iid)
        containers.add(cid)
        runners.add(runner)
        results.append({'group': group, 'job_id': job['id'], 'run_attempt': job['run_attempt'],
                        'artifact_id': archive['id'], 'artifact_name': name,
                        'instance_id': iid, 'container_id': cid, 'result_sha256': artifacts.identity(result)})
    return {'plan_id': artifacts.identity(plan), 'run_id': run['id'], 'run_attempt': run['run_attempt'],
            'head_sha': run['head_sha'], 'conclusion': 'success', 'groups': results}


def api(endpoint):
    return json.loads(subprocess.check_output(['gh', 'api', endpoint]))


def pages(endpoint, key):
    documents = json.loads(subprocess.check_output(['gh', 'api', '--paginate', '--slurp', endpoint]))
    return [row for document in documents for row in document[key]]


def read_artifact(root, available, artifact_id):
    # The pinned download action flattens a single pattern match, even with
    # merge-multiple:false. Count every match, including earlier attempts,
    # rather than the number of selected groups in the current plan.
    rows = [row for row in available if row['id'] == artifact_id]
    require(len(rows) == 1, 'ambiguous artifact ID')
    name = rows[0]['name']
    require(re.fullmatch(r'integration-source-result-\d+-\d+-(all|A|B)', name), 'invalid source artifact name')
    prefix = 'integration-source-result-' + name.split('-')[3] + '-'
    matched = [row for row in available if row.get('name', '').startswith(prefix)]
    directory = root if len(matched) == 1 else root / name
    require(directory.is_dir() and not directory.is_symlink(), 'missing downloaded source artifact')
    result = {}
    for key, suffix in [('result', 'source-system-evidence/output/source-result.json'),
                        ('receipt', 'source-system-evidence/receipt.json'),
                        ('instance', 'source-system-evidence/instance.json')]:
        matches = list(directory.rglob(suffix))
        require(len(matches) == 1, 'missing or duplicate source evidence ' + key)
        path = matches[0]
        require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(directory.resolve())
                and path.stat().st_size <= 16 * 1024 * 1024, 'invalid source evidence file')
        result[key] = json.loads(path.read_text())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--schedule', choices=('all', 'grouped'))
    parser.add_argument('--groups')
    parser.add_argument('--results', type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    artifacts.check_plan(plan)
    if args.schedule:
        print(json.dumps(groups_for(plan, args.schedule == 'grouped')))
        return
    repository, run_id = os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_RUN_ID']
    run = api(f'repos/{repository}/actions/runs/{run_id}')
    require(str(run['run_attempt']) == os.environ['GITHUB_RUN_ATTEMPT'], 'run attempt changed during finalization')
    jobs = pages(f'repos/{repository}/actions/runs/{run_id}/jobs?filter=all&per_page=100', 'jobs')
    available = pages(f'repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100', 'artifacts')
    print(json.dumps(validate(plan, json.loads(args.groups), run, jobs, available,
                              lambda aid: read_artifact(args.results, available, aid)), sort_keys=True))


if __name__ == '__main__':
    main()
