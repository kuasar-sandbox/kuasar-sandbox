#!/usr/bin/env python3
"""Run the independent working-set smoke with the prepared product bytes."""
import argparse
import csv
import importlib.util
import json
from pathlib import Path
import platform
import subprocess
import time

import artifacts
import execution


def execute(plan, arch, workspace, result_path):
    provenance = artifacts.verify_workspace(workspace, plan, arch)
    artifacts.require(arch == platform.machine() == "x86_64", "working-set smoke requires native x86")
    artifacts.require(plan["lanes"][arch]["performance"] == ["working-set-smoke"], "unselected performance gate")
    result = execution.result_record(plan, arch, provenance, workspace)
    result["checks"] = ["working-set-smoke"]
    started = time.monotonic()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with execution.scratch_directory() as state:
            environment, _ = execution.environment(state)
            spec = importlib.util.spec_from_file_location("prepared_inputs", workspace / "test/e2e/lib/workspace.py")
            inputs = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(inputs)
            inputs.load_images(workspace, provenance, environment)
            environment.update(inputs.case_environment(workspace, provenance, "sandbox.working-set.sh"))
            revisions = state / "working-set-revisions.tsv"
            with revisions.open("w", newline="") as output:
                writer = csv.writer(output, delimiter="\t")
                writer.writerow(("repository", "requested_ref", "resolved_sha", "role"))
                for owner in artifacts.OWNERS:
                    record = plan["sources"][owner]
                    writer.writerow((record["repository"], record["sha"], record["sha"], record["role"]))
            environment.update(PERF_ITERS="1", KUASAR_REVISION_MANIFEST=str(revisions), WORK=str(state / "work"))
            command = ["bash", str(workspace / "test/perf/working-set-netns.sh"),
                       str(workspace / "test/perf/sandbox-perf-working-set.sh")]
            case_started = time.monotonic()
            completed = subprocess.run(command, cwd=state, env=environment)
            result["timings"].append({"case": "working-set-smoke", "wall_seconds": time.monotonic() - case_started,
                                      "exit_code": completed.returncode})
            artifacts.require(completed.returncode == 0, "working-set smoke failed")
            artifacts.verify_workspace(workspace, plan, arch)
        result["conclusion"] = "success"
    finally:
        result["wall_seconds"] = time.monotonic() - started
        result_path.write_bytes(artifacts.canonical(result) + b"\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--arch", choices=artifacts.ARCHES, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    execute(json.loads(args.plan.read_text()), args.arch, args.workspace.resolve(), args.result.resolve())


if __name__ == "__main__":
    main()
