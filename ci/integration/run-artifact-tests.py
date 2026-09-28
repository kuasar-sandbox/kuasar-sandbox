#!/usr/bin/env python3
"""Invoke the shared public runner for one predeclared suite shard."""
import argparse
import json
from pathlib import Path
import platform
import subprocess
import time

import artifacts
import execution


def execute(plan, arch, shard, workspace, result_path, clean_image=None):
    provenance = artifacts.verify_workspace(workspace, plan, arch)
    artifacts.require(platform.machine() == arch, "E2E requires the selected native target runner")
    groups = artifacts.shards(provenance["selection"])
    artifacts.require(shard in groups, "unselected E2E shard")
    result = execution.result_record(plan, arch, provenance, workspace)
    result.update(shard=shard, cases=groups[shard], environment={"kind": "native-host"})
    started = time.monotonic()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with execution.scratch_directory() as state:
            environment, prepared = execution.environment(state)
            if result["cases"]:
                report = state / "result.json"
                command = ["python3", "-B", str(workspace / "test/e2e/e2e"), "run",
                           "--workdir", str(workspace), "--arch", arch,
                           "--run-root", str(state / "cases"), "--out-root", str(state / "out"),
                           "--result", str(report)]
                for case in result["cases"]:
                    command += ["--include", case]
                if clean_image:
                    arguments = ['--arch', arch]
                    for case in result['cases']:
                        arguments += ['--include', case]
                    command = execution.clean_command(clean_image, workspace, state, arguments)
                    result['environment'] = execution.runtime_identity(clean_image)
                else:
                    command = execution.privileged_command(command, prepared)
                completed = subprocess.run(command, cwd=state, env=environment)
                if report.is_file():
                    public = json.loads(report.read_text())
                    result["timings"] = public["timings"]
                    artifacts.require(public["arch"] == arch and public["cases"] == result["cases"] and
                                      public["provenance_sha256"] == result["provenance_sha256"],
                                      "public runner result differs from selected inputs")
                    artifacts.require(public["conclusion"] == "success", "public runner reported failure")
                artifacts.require(completed.returncode == 0, "shared public runner failed")
                if clean_image:
                    result['environment']['verified'] = True
            artifacts.check_timings(result["timings"], result["cases"])
            artifacts.verify_workspace(workspace, plan, arch)
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
    args = parser.parse_args()
    execute(json.loads(args.plan.read_text()), args.arch, args.shard, args.workspace.resolve(), args.result.resolve(), args.clean_image)


if __name__ == "__main__":
    main()
