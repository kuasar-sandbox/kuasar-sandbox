#!/usr/bin/env python3
"""Bind Actions rerun views to actual producer executions, never stale success.

Partial reruns create new IDs for carried successful jobs but preserve their
execution, runner and steps. Such a view is created after that execution ended.
It may refer only to the latest preceding successful producer of the same job.
"""
from datetime import datetime, timezone

VIEW_FIELDS = {'id', 'node_id', 'url', 'html_url', 'check_run_url', 'run_attempt', 'created_at'}
EXECUTION_FIELDS = {'run_id', 'name', 'head_sha', 'started_at', 'completed_at',
                    'status', 'conclusion', 'runner_id', 'runner_name', 'steps'}


def require(condition, message):
    if not condition:
        raise ValueError('Actions producer: ' + message)


def timestamp(value):
    require(isinstance(value, str), 'missing execution timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError('Actions producer: invalid execution timestamp') from error
    require(parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed),
            'execution timestamp must be UTC')
    return parsed


def execution(job):
    require(EXECUTION_FIELDS <= job.keys() and type(job.get('id')) is int and job['id'] > 0,
            'incomplete carried execution identity')
    require(type(job['run_id']) is int and job['run_id'] > 0 and
            type(job['runner_id']) is int and job['runner_id'] > 0 and
            isinstance(job['runner_name'], str) and job['runner_name'] and
            isinstance(job['steps'], list) and job['steps'], 'missing carried runner or steps')
    require(job['status'] == 'completed' and job['conclusion'] == 'success' and
            timestamp(job['started_at']) <= timestamp(job['completed_at']),
            'only completed successful execution can be carried')
    result = {key: value for key, value in job.items() if key not in VIEW_FIELDS}
    # The observed Hosted API uses null in carried views for the original
    # sentinel group ID 0. The real runner ID/name, labels and group name stay.
    if result.get('runner_group_id', -1) is None:
        result['runner_group_id'] = 0
    return result


def latest_producers(jobs, *, run_id=None, run_attempt=None):
    views, seen = [], set()
    for job in jobs:
        name, attempt = job.get('name'), job.get('run_attempt')
        require(isinstance(name, str) and name and type(attempt) is int and attempt > 0,
                'missing Actions job identity')
        require((name, attempt) not in seen, 'duplicate Actions job attempt')
        if run_id is not None:
            require(job.get('run_id') == run_id, 'job has another run')
        if run_attempt is not None:
            require(attempt <= run_attempt, 'job has a future attempt')
        seen.add((name, attempt))
        views.append(job)
    selected = {}
    for job in sorted(views, key=lambda item: (item['run_attempt'], item['name'])):
        name = job['name']
        created, ended = job.get('created_at'), job.get('completed_at')
        # GitHub also creates views of skipped/failed jobs. They are not
        # successful producers: keep their current status so consumers reject
        # them if required, without treating unrelated skipped jobs as errors.
        carried = (job.get('status') == 'completed' and job.get('conclusion') == 'success' and
                   created is not None and ended is not None and timestamp(created) > timestamp(ended))
        if carried:
            previous = selected.get(name)
            require(previous is not None and previous['run_attempt'] < job['run_attempt'] and
                    previous.get('id') != job.get('id'), 'carried job has no unique earlier producer')
            require(execution(previous) == execution(job), 'carried job differs from latest preceding execution')
            require(previous.get('created_at') is not None and
                    timestamp(previous['created_at']) <= timestamp(previous['started_at']),
                    'carried origin is not an actual execution')
            continue
        # A real new execution, including running/failed/cancelled, supersedes
        # earlier success. Never scan backwards past it for usable artifacts.
        selected[name] = job
    return selected
