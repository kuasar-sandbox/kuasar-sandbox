#!/usr/bin/env python3
"""Invoke the shared public runner for one predeclared suite shard."""
import argparse
import json
from pathlib import Path
import platform
import re
import subprocess
import time

import artifacts
import execution
import capability_routing


def execute(plan, arch, shard, workspace, result_path, clean_image=None, runner=None, executor_image=None):
    provenance = artifacts.verify_workspace(workspace, plan, arch)
    artifacts.require(platform.machine() == arch, "E2E requires the selected native target runner")
    groups = artifacts.plan_shards(plan, arch)
    artifacts.require(shard in groups, "unselected E2E shard")
    result = execution.result_record(plan, arch, provenance, workspace)
    result.update(shard=shard, cases=groups[shard], environment={"kind": "native-host"})
    if plan['schema'] >= 4:
        group = plan['lanes'][arch]['groups'][shard]
        profile = plan['runner_configuration']['value']['profiles'][group['profile']]
        runner = runner if runner is not None else capability_routing.runner_identity(plan, arch, shard)
        capability_routing.check_runner(plan, arch, profile, runner)
        artifacts.require((profile['executor'] == 'workbench-system') == bool(executor_image), 'wrong E2E executor')
        artifacts.require((group['runtime'] == 'runtime-only') == bool(clean_image), 'wrong E2E runtime environment')
        result['execution'] = {'profile': group['profile'], 'executor': profile['executor'], 'runner': runner,
                               'configuration_sha256': plan['runner_configuration']['sha256']}
        if executor_image:
            artifacts.require(re.fullmatch(r'sha256:[0-9a-f]{64}', executor_image), 'invalid local Workbench image')
            result['execution']['image_id'] = executor_image
            result['execution']['reference'] = plan['execution_images'][arch]['reference']
            result['environment'] = {'kind': 'workbench-system', 'image_id': executor_image}
    started = time.monotonic()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    failure = None
    batches = [result['cases']] if result['cases'] else []
    if all(case.split('.', 1)[0] == 'snapshot' for case in result['cases']):
        batches = [[case] for case in result['cases']]
    try:
        # Snapshot cases retain disks and full-RAM observations in WORK and OUT.
        # Reclaim their private state before the next case. Other suites keep
        # their existing single invocation and cross-case template preparation.
        # The shipped runner, prepared inputs and selected coverage are unchanged.
        for batch in batches:
            with execution.scratch_directory() as state:
                environment, prepared = execution.environment(state)
                report = state / "result.json"
                selection = [value for case in batch for value in ('--include', case)]
                command = ["python3", "-B", str(workspace / "test/e2e/e2e"), "run",
                           "--workdir", str(workspace), "--arch", arch,
                           "--run-root", str(state / "cases"), "--out-root", str(state / "out"),
                           "--result", str(report), *selection]
                if clean_image:
                    arguments = ['--arch', arch, *selection]
                    command = execution.clean_command(clean_image, workspace, state, arguments)
                    result['environment'] = execution.runtime_identity(clean_image)
                else:
                    command = execution.privileged_command(command, prepared)
                completed = subprocess.run(command, cwd=state, env=environment)
                artifacts.require(report.is_file(), "public runner did not write execution evidence")
                public = json.loads(report.read_text())
                artifacts.require(public["arch"] == arch and public["cases"] == batch and
                                  public["provenance_sha256"] == result["provenance_sha256"],
                                  "public runner result differs from selected inputs")
                result["timings"].extend(public["timings"])
                if public["conclusion"] != "success":
                    failure = "public runner reported failure"
                elif completed.returncode:
                    failure = "shared public runner failed"
                else:
                    artifacts.check_timings(public["timings"], batch)
                    if plan['schema'] >= 4:
                        observed = public.get('capabilities')
                        required = {cap for case in batch for cap in plan['case_requirements']['cases'][case]['requires']}
                        previous = result.setdefault('capabilities', {})
                        artifacts.require(isinstance(observed, dict) and set(observed) == set(required) and
                                          all(cap not in previous or previous[cap] == value
                                              for cap, value in observed.items()),
                                          'missing or changed case capability startup evidence')
                        previous.update(observed)
        artifacts.require(failure is None, failure)
        artifacts.check_timings(result["timings"], result["cases"])
        artifacts.verify_workspace(workspace, plan, arch)
        if plan['schema'] >= 4:
            capability_routing.check_result(plan, arch, shard, result)
        if clean_image and result['cases']:
            result['environment']['verified'] = True
        result["conclusion"] = "success"
    finally:
        result["wall_seconds"] = time.monotonic() - started
        result_path.write_bytes(artifacts.canonical(result) + b"\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--arch", required=True, choices=artifacts.ARCHES)
    parser.add_argument("--shard", required=True)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--clean-image", help="immutable runtime-only image for source/compiler-free acceptance")
    parser.add_argument('--runner-identity', type=Path, help='trusted host runner receipt for Workbench execution')
    parser.add_argument('--executor-image', help='verified immutable Workbench image ID')
    args = parser.parse_args()
    execute(json.loads(args.plan.read_text()), args.arch, args.shard, args.workspace.resolve(), args.result.resolve(),
            args.clean_image, json.loads(args.runner_identity.read_text()) if args.runner_identity else None, args.executor_image)


if __name__ == "__main__":
    main()
