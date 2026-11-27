#!/usr/bin/env python3
"""Recheck frozen PR admission before a queued stage starts expensive work."""
import argparse
import json
import os
from pathlib import Path
import subprocess

import artifacts


def recheck(plan, framework_sha):
    artifacts.check_plan(plan)
    artifacts.require(plan['framework_sha'] == framework_sha, 'source recheck framework differs from plan')
    if plan['mode'] != 'source':
        return 0
    records = plan.get('candidate_records')
    artifacts.require(isinstance(records, list) and records, 'source recheck requires frozen candidate records')
    primary = records[0]
    environment = dict(os.environ)
    artifacts.require(environment.get('GH_TOKEN'), 'source recheck requires a read-only control token')
    environment.update({
        'CALLER_TOKEN': environment['GH_TOKEN'],
        'CANDIDATE_REPOSITORY': primary['repository'],
        'CANDIDATE_PR': str(primary['pull_request_number']),
        'CANDIDATE_SHA': primary['candidate_sha'],
        'CANDIDATE_BASE_SHA': primary['base_sha'],
        'CANDIDATE_BASE_REF': primary['base_ref'],
        'CANDIDATE_HEAD_SHA': primary['head_sha'],
        'COMPANION_CANDIDATES': json.dumps(records[1:]),
    })
    # The same trusted admission implementation remains authoritative at
    # resolve, these early barriers, and final acceptance. No token is passed
    # to source materialization, product builds, or candidate execution.
    return subprocess.run(['bash', str(Path(__file__).with_name('validate-source-set.sh'))],
                          env=environment).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--framework-sha', required=True)
    args = parser.parse_args()
    return recheck(json.loads(args.plan.read_text()), args.framework_sha)


if __name__ == '__main__':
    raise SystemExit(main())
