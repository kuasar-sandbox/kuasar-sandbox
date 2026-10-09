#!/usr/bin/env python3
"""Materialize exact test pins, then run source checks in ordinary/system Workbench."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import artifacts

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("artifact_build", Path(__file__).with_name("build-artifacts.py"))
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


SOURCE_RECORD = ".source-checks.json"


def source_records(plan):
    selected = set(artifacts.OWNERS if "platform" in plan["owners"] else plan["owners"])
    owners = build.dependencies(selected | {"platform"})
    return selected, {owner: plan["test_revisions"][owner] for owner in sorted(owners)}


def environment():
    environment = os.environ.copy()
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "CALLER_TOKEN", "KUASAR_CI_APP_PRIVATE_KEY"):
        artifacts.require(not environment.get(key), f"source checks must not receive {key}")
    return environment


def new_result(plan, phase):
    return {"plan_id": artifacts.identity(plan), "framework_sha": plan["framework_sha"],
            "test_revisions": plan["test_revisions"], "phase": phase,
            "conclusion": "failure", "exit_code": 1, "actions": [], "checks": []}


def save_result(output, result):
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(artifacts.canonical(result) + b"\n")


def checkout_sources(plan, sources, result):
    _, records = source_records(plan)
    sources.mkdir(parents=True, exist_ok=False)
    for owner, record in records.items():
        start = time.monotonic()
        action = {"name": "checkout:" + owner, "repository": record["repository"],
                  "sha": record["sha"], "directory": str(sources / owner), "exit_code": 1}
        try:
            build.checkout(record["repository"], record["sha"], sources / owner)
            action["exit_code"] = 0
        except subprocess.CalledProcessError as error:
            action["exit_code"] = error.returncode
            result["exit_code"] = error.returncode
            raise
        finally:
            action["wall_seconds"] = time.monotonic() - start
            result["actions"].append(action)
    result["sources"] = records
    # Materialization only uses Git. No host compiler is invoked to make this
    # receipt or to initialize a Go workspace.
    (sources / SOURCE_RECORD).write_bytes(artifacts.canonical({
        "plan_id": result["plan_id"], "sources": records}) + b"\n")


def materialize(plan, sources, output):
    result = new_result(plan, "materialize")
    try:
        artifacts.check_plan(plan)
        environment()
        checkout_sources(plan, sources, result)
        result.update(conclusion="success", exit_code=0)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        result["error"] = str(error)
        raise
    finally:
        save_result(output, result)


def action(result, collection, name, command, directory, env, *, capture=False):
    start = time.monotonic()
    record = {"name": name, "command": [str(item) for item in command],
              "directory": str(directory), "exit_code": 127}
    try:
        kwargs = {"cwd": directory, "env": env}
        if capture:
            kwargs.update(stdout=subprocess.PIPE, text=True)
        completed = subprocess.run(command, **kwargs)
        record["exit_code"] = completed.returncode
        result["exit_code"] = completed.returncode if completed.returncode else 1
        artifacts.require(completed.returncode == 0, f"required source check failed: {name}")
        return completed.stdout if capture else None
    except FileNotFoundError:
        result["exit_code"] = 127
        raise
    finally:
        record["wall_seconds"] = time.monotonic() - start
        result[collection].append(record)


def verify_sources(plan, sources, result, env):
    _, records = source_records(plan)
    marker = sources / SOURCE_RECORD
    artifacts.require(marker.is_file() and not marker.is_symlink(), "missing exact source materialization receipt")
    artifacts.require(json.loads(marker.read_text()) == {"plan_id": result["plan_id"], "sources": records},
                      "materialized test sources differ from admitted plan")
    for owner, record in records.items():
        directory = sources / owner
        artifacts.require(directory.is_dir() and not directory.is_symlink(),
                          f"source checks require a private repository directory: {owner}")
        actual = action(result, "actions", "source-identity:" + owner,
                        ["git", "rev-parse", "HEAD"], directory, env, capture=True)
        result["actions"][-1].update(expected_sha=record["sha"], actual_sha=actual.strip())
        artifacts.require(actual.strip() == record["sha"], f"materialized test source changed: {owner}")
        action(result, "actions", "source-clean:" + owner,
               ["git", "diff", "--no-ext-diff", "--exit-code", "HEAD", "--"], directory, env)
        untracked = action(result, "actions", "source-untracked:" + owner,
                           ["git", "ls-files", "--others", "--exclude-standard"], directory, env, capture=True)
        artifacts.require(not untracked.strip(), f"materialized test source has untracked inputs: {owner}")
    result["sources"] = records


def workspace(plan, sources, result, env):
    _, records = source_records(plan)
    modules = ["./" + owner for owner in records if (sources / owner / "go.mod").is_file()]
    if not modules:
        return
    path = sources / "go.work"
    # Both phases use the same /src layout. The system-mode copy may already
    # contain the ordinary phase's workspace; accept only the exact module set.
    if not path.exists():
        action(result, "actions", "go-work-init", ["go", "work", "init", *modules], sources,
               {**env, "GOWORK": "off"})
    document = json.loads(action(result, "actions", "go-work-check",
                                 ["go", "work", "edit", "-json", str(path)], sources,
                                 {**env, "GOWORK": "off"}, capture=True))
    expected = {(sources / name).resolve() for name in modules}
    actual = [(sources / record["DiskPath"]).resolve() for record in document.get("Use", [])]
    artifacts.require(len(actual) == len(expected) and set(actual) == expected and not document.get("Replace"),
                      "source-check workspace differs from exact test dependency closure")
    env["GOWORK"] = str(path)


def checks_for(plan, sources, phase):
    selected, _ = source_records(plan)
    checks = []
    if phase != "privileged":
        checks.append(("platform-contracts", ["make", "test-ci-tools", "test-release-tools", "test-perf-tools"], sources / "platform"))
    for owner in ("connector", "sandboxer", "orchestrator"):
        if owner in selected:
            command = ["bash", "scripts/ci-source-checks.sh"]
            if phase != "all":
                command.append("--" + phase)
            name = owner + ("-privileged" if phase == "privileged" else "-unit-race-vet")
            checks.append((name, command, sources / owner))
    if phase != "privileged" and "accelerator" in selected:
        checks.append(("accelerator-fixtures", ["make", "test-e2e-scripts"], sources / "accelerator"))
    if phase != "ordinary" and selected & {"sandboxer", "platform"}:
        checks.append(("uffd-source-benchmark", ["bash", "test/perf/uffd-performance-gate.sh"], sources / "platform"))
    return checks


def execute(plan, sources, output, *, materialized=False, phase="all"):
    result = new_result(plan, phase)
    try:
        artifacts.check_plan(plan)
        artifacts.require(phase in ("all", "ordinary", "privileged"), "unknown source-check phase")
        env = environment()
        if phase == "all":
            # Each test creates its own private state below this short root.
            # An extra random parent would exhaust existing Unix socket paths.
            env["TMPDIR"] = env.get("TMPDIR") or "/tmp"
        else:
            env["TMPDIR"] = "/build/t"
        env.update(ORG=str(sources), TARGET_ARCH="x86_64", PYTHONDONTWRITEBYTECODE="1")
        result["arch"] = "x86_64"
        result["workbench"] = {"image_id": env.get("KUASAR_WORKBENCH_IMAGE_ID"),
                               "framework_sha": env.get("KUASAR_WORKBENCH_FRAMEWORK_SHA")}
        if materialized:
            verify_sources(plan, sources, result, env)
        else:
            checkout_sources(plan, sources, result)
        workspace(plan, sources, result, env)
        for name, command, directory in checks_for(plan, sources, phase):
            action(result, "checks", name, command, directory, env)
        result.update(conclusion="success", exit_code=0)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        result["error"] = str(error)
        raise
    finally:
        save_result(output, result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--materialize-only", action="store_true", help="fetch exact test sources on the trusted host, without compilers")
    mode.add_argument("--materialized", action="store_true", help="verify and use the task's existing exact test sources")
    parser.add_argument("--phase", choices=("all", "ordinary", "privileged"), default="all")
    args = parser.parse_args()
    if args.materialize_only and args.phase != "all":
        parser.error("--materialize-only does not execute a phase")
    try:
        plan = json.loads(args.plan.read_text())
        if args.materialize_only:
            materialize(plan, args.sources.resolve(), args.output.resolve())
        else:
            execute(plan, args.sources.resolve(), args.output.resolve(), materialized=args.materialized, phase=args.phase)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"source checks: {error}", file=sys.stderr)
        status = 1
        if args.output.is_file():
            status = json.loads(args.output.read_text()).get("exit_code", 1) or 1
        sys.exit(status if status >= 0 else 128 - status)
