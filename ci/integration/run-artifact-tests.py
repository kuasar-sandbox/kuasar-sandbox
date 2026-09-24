#!/usr/bin/env python3
"""Execute only the predeclared cases from a prepared target workspace."""
import argparse
from contextlib import contextmanager
import csv
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import time

import artifacts


@contextmanager
def scratch_directory():
    # Owner cases use sudo and can leave root-owned metrics or VM state here.
    # Only this freshly allocated private directory may need privileged removal.
    state = Path(tempfile.mkdtemp(prefix="ki-", dir="/var/tmp"))
    try:
        yield state
    finally:
        try:
            shutil.rmtree(state)
        except PermissionError:
            subprocess.run(["sudo", "-n", "rm", "-rf", "--", str(state)], check=True)


def privileged_command(command, prepared):
    # The retired owner wrappers used sudo. Keep that execution condition at
    # the CI boundary, forwarding only explicitly prepared inputs, never -E.
    if os.geteuid() == 0:
        return command
    return ["sudo", "-n", "--preserve-env=" + ",".join(sorted(prepared)), "--", *command]


def execute(plan, arch, shard, workspace, result_path):
    provenance = artifacts.verify_workspace(workspace, plan, arch)
    artifacts.require(platform.machine() == arch, "E2E requires the selected native target runner")
    groups = artifacts.shards(provenance["profile"])
    artifacts.require(shard in groups, "unselected E2E shard")
    environment = os.environ.copy()
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "CALLER_TOKEN", "KUASAR_CI_APP_PRIVATE_KEY"):
        artifacts.require(not environment.get(key), f"artifact E2E must not receive {key}")
    result = {"arch": arch, "shard": shard, "plan_id": artifacts.identity(plan), "conclusion": "failure",
              "test_revisions": provenance["test_revisions"],
              "provenance_sha256": artifacts.digest(workspace / "provenance.json"), "cases": groups[shard], "timings": [],
              "extra_checks": plan["lanes"][arch].get("extra_checks", {}).get(shard, [])}
    started = time.monotonic()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Short private disk-backed paths preserve the existing Unix socket and
        # direct-I/O test contracts; long Actions workspace paths exceed sun_path.
        with scratch_directory() as state:
            for name in ("tmp", "docker", "metrics", "perf"):
                (state / name).mkdir(mode=0o700)
            prepared_env = dict(BIN=str(workspace / "bin"), KUASAR_ARTIFACT_E2E="1", TARGET_ARCH=arch,
                               KUASAR_EXPECTED_RUNTIME_INIT_SHA256=provenance["embedded"]["init"],
                               TMPDIR=str(state / "tmp"), DOCKER_CONFIG=str(state / "docker"),
                               KUASAR_CI_DIR=str(state / "metrics"), PERF_OUT_DIR=str(state / "perf"),
                               PYTHONDONTWRITEBYTECODE="1", CLUSTER_STUB_BUILD="0")
            for flag in ("KVM", "EXEC", "CLUSTER_STUB", "CLUSTER_REAL", "ORCH", "PROXY", "BUILDER", "RUNTASK", "CONNECTOR_E2E", "GUEST_RUNTIME"):
                prepared_env["REQUIRE_" + flag] = "1"
            for name, variable in {"zot": "ZOT_BIN", "versitygw": "VGW_BIN", "custom-proxy": "CUSTOM_PROXY_BIN",
                                   "telemetry-grpc-probe": "TELEMETRY_GRPC_PROBE_BIN", "usage-probe": "USAGE_PROBE_BIN",
                                   "cgroup-fork-probe": "CGROUP_FORK_PROBE_BIN", "orch-cli.test": "ORCH_CLI_TEST_BIN"}.items():
                if name in provenance["helpers"]:
                    prepared_env[variable] = str(workspace / "fixtures/bin" / name)
            environment.update(prepared_env)
            for record in provenance.get("images", {}).values():
                path = workspace / record["archive"]
                artifacts.require(artifacts.digest(path) == record["sha256"], "prepared image bytes changed")
                subprocess.run(["docker", "image", "load", "--input", str(path)], env=environment, check=True)
                image = json.loads(subprocess.check_output(["docker", "image", "inspect", record["image_id"]], env=environment))[0]
                artifacts.require(image["Id"] == record["image_id"] and image["Os"] + "/" + image["Architecture"] == record["platform"],
                                  "loaded image differs from prepared platform/digest")
            images = provenance.get("images", {})
            for label, variable in (("prometheus", "TELEMETRY_PROMETHEUS_IMAGE"), ("clickhouse", "TELEMETRY_CLICKHOUSE_IMAGE"),
                                    ("orchestrator-base", "ORCHESTRATOR_BASE_IMAGE"), ("orchestrator-execute", "ORCHESTRATOR_EXECUTE_IMAGE"),
                                    ("busybox", "KUASAR_BUSYBOX_IMAGE")):
                if label in images:
                    prepared_env[variable] = environment[variable] = images[label]["image_id"]
            for case in groups[shard]:
                selected = dict(prepared_env)
                parts = Path(case).parts
                owner = parts[2]
                rewritten = len(parts) == 5 and parts[:2] == ("test", "e2e") and parts[3] == "cases"
                if rewritten:
                    case_id = artifacts.case_name(parts[4])
                    selected.update(E2E_CASES_DIR=str(workspace / "test/e2e/cases"),
                                    E2E_LIB=str(workspace / "test/e2e/lib"),
                                    E2E_WORKSPACE=str(workspace), E2E_ARCH=arch, PATH=environment["PATH"])
                    command = ["python3", str(workspace / "test/e2e/e2e"), "run",
                               "--workdir", str(workspace), "--arch", arch,
                               "--run-root", str(state / "cases"), "--out-root", str(state / "out"),
                               "--include", case_id]
                else:
                    command = ["bash", str(workspace / case)]
                if owner == "accelerator":
                    selected["MANIFEST_FIXTURE_DIR"] = str(workspace / "fixtures/manifest")
                if owner == "guest-runtime":
                    selected["E2E_IMAGE"] = images["guest-runtime"]["image_id"]
                elif "python" in images:
                    selected.update(E2E_IMAGE=images["python"]["image_id"], IMAGE=images["python"]["image_id"])
                if rewritten:
                    command = privileged_command(command, selected)
                case_started = time.monotonic()
                completed = subprocess.run(command, cwd=state, env={**environment, **selected})
                result["timings"].append({"case": case, "wall_seconds": time.monotonic() - case_started, "exit_code": completed.returncode})
                artifacts.require(completed.returncode == 0, f"selected case failed: {case}")
            if result["extra_checks"] == ["working-set-smoke"]:
                # Keep the existing source CI smoke with the same product bytes.
                # Business flatten/manifest/snapshot operations remain in it.
                # Baseline-pinned platform tests still use the existing source
                # revision manifest. Newer tests read the prepared provenance.
                revisions = state / "working-set-revisions.tsv"
                with revisions.open("w", newline="") as output:
                    writer = csv.writer(output, delimiter="\t")
                    writer.writerow(("repository", "requested_ref", "resolved_sha", "role"))
                    for owner in artifacts.OWNERS:
                        record = plan["sources"][owner]
                        writer.writerow((record["repository"], record["sha"], record["sha"], record["role"]))
                selected = {**environment, "PERF_ITERS": "1", "IMAGE": images["python"]["image_id"],
                            "KUASAR_REVISION_MANIFEST": str(revisions)}
                case_started = time.monotonic()
                command = ["bash", str(workspace / "test/perf/working-set-netns.sh"), str(workspace / "test/perf/sandbox-perf-working-set.sh")]
                completed = subprocess.run(command, cwd=state, env=selected)
                result["timings"].append({"case": "working-set-smoke", "wall_seconds": time.monotonic() - case_started, "exit_code": completed.returncode})
                artifacts.require(completed.returncode == 0, "selected working-set smoke failed")
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
    args = parser.parse_args()
    execute(json.loads(args.plan.read_text()), args.arch, args.shard, args.workspace.resolve(), args.result.resolve())


if __name__ == "__main__":
    main()
