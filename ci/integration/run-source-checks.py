#!/usr/bin/env python3
"""Keep existing compiler-dependent checks outside the prepared E2E workspace."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time

import artifacts

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("artifact_build", Path(__file__).with_name("build-artifacts.py"))
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def execute(plan, sources, output):
    artifacts.check_plan(plan)
    environment = os.environ.copy()
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "CALLER_TOKEN", "KUASAR_CI_APP_PRIVATE_KEY"):
        artifacts.require(not environment.get(key), f"source checks must not receive {key}")
    selected = set(artifacts.OWNERS if "platform" in plan["owners"] else plan["owners"])
    owners = selected | {"platform"}
    for owner in list(owners):
        owners.update(build.LIBRARIES.get(owner, ()))
    sources.mkdir(parents=True, exist_ok=False)
    for owner in sorted(owners):
        record = plan["sources"][owner]
        build.checkout(record["repository"], record["sha"], sources / owner)
    modules = ["./" + owner for owner in sorted(owners) if (sources / owner / "go.mod").is_file()]
    if modules:
        build.run(["go", "work", "init", *modules], cwd=sources)
    # Existing Unix-socket regressions need a short disk-backed temporary path.
    environment.update(ORG=str(sources), TARGET_ARCH="x86_64", PYTHONDONTWRITEBYTECODE="1", TMPDIR="/var/tmp")
    result = {"plan_id": artifacts.identity(plan), "conclusion": "failure", "checks": []}
    output.parent.mkdir(parents=True, exist_ok=True)
    checks = [("platform-contracts", ["make", "test-ci-tools", "test-release-tools", "test-perf-tools"], sources / "platform")]
    for owner in ("connector", "sandboxer", "orchestrator"):
        if owner in selected:
            checks.append((owner + "-unit-race-vet", ["bash", "scripts/ci-source-checks.sh"], sources / owner))
    if "accelerator" in selected:
        checks.append(("accelerator-fixtures", ["python3", "-B", "test/scripts/test_e2e_manifest.py", "-v"], sources / "accelerator"))
    if selected & {"sandboxer", "platform"}:
        checks.append(("uffd-source-benchmark", ["bash", "test/perf/uffd-performance-gate.sh"], sources / "platform"))
    try:
        for name, command, directory in checks:
            start = time.monotonic()
            completed = subprocess.run(command, cwd=directory, env=environment)
            result["checks"].append({"name": name, "exit_code": completed.returncode, "wall_seconds": time.monotonic() - start})
            artifacts.require(completed.returncode == 0, f"required source check failed: {name}")
        result["conclusion"] = "success"
    finally:
        output.write_bytes(artifacts.canonical(result) + b"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    execute(json.loads(args.plan.read_text()), args.sources.resolve(), args.output.resolve())
