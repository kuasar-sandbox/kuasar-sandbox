#!/usr/bin/env python3
"""Offline regression contracts for the staged guest-runtime runner migration."""
import ast
import json
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
GUEST = "kuasar-sandbox/guest-runtime"


def expression(value, context):
    """Evaluate the small Actions expression subset used by runner selection."""
    value = value.removeprefix("${{").removesuffix("}}").strip()
    tree = ast.parse(value.replace("&&", " and ").replace("||", " or "), mode="eval")

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return context[node.id]
        if isinstance(node, ast.Attribute):
            return visit(node.value)[node.attr]
        if isinstance(node, ast.BoolOp):
            result = visit(node.values[0])
            for term in node.values[1:]:
                if (isinstance(node.op, ast.And) and result) or (isinstance(node.op, ast.Or) and not result):
                    result = visit(term)
            return result
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = visit(node.left), visit(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "fromJSON":
            return json.loads(visit(node.args[0]))
        raise AssertionError(f"unsupported expression: {ast.dump(node)}")

    return visit(tree)


def load(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def check():
    entry = load("ci-entry.yml")["jobs"]
    e2e = load("integration-tests.yml")["jobs"]["e2e"]
    legacy = ["self-hosted", "Linux", "X64", "kuasar-e2e", "kvm", "cgroup-v2"]
    repos = ("guest-runtime", "accelerator", "connector", "kuasar-sandbox", "orchestrator", "sandboxer")
    for name in repos:
        repository = f"kuasar-sandbox/{name}"
        for private in (False, True):
            context = {"github": {"repository": repository, "event": {"repository": {"private": private}}}}
            for job in (entry["admission"], entry["finalize"]):
                expected = "ubuntu-24.04" if repository == GUEST else "kuasar-control" if private else "ubuntu-latest"
                assert expression(job["runs-on"], context) == expected
            for mode in ("source", "exact-assets"):
                context["inputs"] = {"mode": mode, "candidate_repository": repository}
                hosted = mode == "source" and repository == GUEST
                assert expression(e2e["runs-on"], context) == (["ubuntu-24.04"] if hosted else legacy)
                assert expression(e2e["env"]["KUASAR_HOSTED"], context) == hosted
                assert expression(e2e["timeout-minutes"], context) == (180 if hosted else 120 if mode == "exact-assets" else 60)

    assert entry["e2e"]["uses"] == "./.github/workflows/integration-tests.yml"
    assert entry["e2e"]["with"]["candidate_repository"] == "${{ github.repository }}"
    for job in (entry["admission"], entry["finalize"]):
        steps = job["steps"]
        checkout, bootstrap = steps[:2]
        assert checkout["with"]["ref"] == "${{ job.workflow_sha }}"
        assert checkout["with"]["repository"] == "${{ job.workflow_repository }}"
        assert checkout["with"]["persist-credentials"] is False
        assert bootstrap["run"] == "bash trusted/platform/ci/hosted/bootstrap.sh --profile control"
        assert bootstrap["if"] == checkout["if"] == f"github.repository == '{GUEST}'"

    steps = {step["name"]: step for step in e2e["steps"]}
    names = list(steps)
    bootstrap_name = "Bootstrap standard guest source runner from trusted tooling"
    assert steps[bootstrap_name]["run"] == "bash trusted/platform/ci/hosted/bootstrap.sh --profile source"
    assert names.index("Revoke platform tooling token before candidate execution") < names.index(bootstrap_name)
    assert names.index(bootstrap_name) < names.index("Create read-only source token")
    assert names.index("Revoke source token before executing candidate code") < names.index("Finalize source workspace")
    for name in ("Reset interrupted E2E state", "Attach runner caches and test tools", "Configure persistent runner environment"):
        assert steps[name]["if"] == "env.KUASAR_HOSTED != 'true'"
    for step in e2e["steps"]:
        if any(path in step.get("run", "") for path in ("/var/cache/kuasar", "/var/lib/kuasar-ci", "goproxy.cn", "rsproxy.cn", "tsinghua.edu.cn", "daocloud.io")):
            assert not expression(step["if"], {"env": {"KUASAR_HOSTED": "true"}}), step["name"]
        assert "actions/cache@" not in step.get("uses", "")
        if "actions/upload-artifact@" in step.get("uses", ""):
            assert step["with"]["path"] == "${{ env.KUASAR_CI_DIR }}"
    for name in ("Assemble the five component source repositories", "Finalize source workspace"):
        assert 'source_dir="$KUASAR_SOURCE_CACHE_ROOT/$repo"' in steps[name]["run"]
    assert "repos=(accelerator connector guest-runtime orchestrator sandboxer)" in steps["Assemble the five component source repositories"]["run"]
    for name in ("Restore or build verified native artifacts", "Validate platform tooling", "Build and test source candidate"):
        assert steps[name]["shell"] == "bash"
        assert 'taskset -pc "$KUASAR_BUILD_CPUS" "$$"' in steps[name]["run"]
        assert steps[name]["if"] == "inputs.mode == 'source'"
    build = steps["Build and test source candidate"]["run"]
    for required in ('make build e2e-tools assemble-e2e', 'make test-uffd-performance-gate',
                     'owner="${CANDIDATE_REPOSITORY##*/}"', 'bash "$runner"'):
        assert required in build
    smoke = steps["Run working-set performance smoke"]
    assert smoke["if"] == "inputs.mode == 'source'"
    assert smoke["env"]["PERF_ITERS"] == "1"
    assert "PERF_GROUPS" not in smoke["env"]
    assert "bash test/perf/working-set-netns.sh test/perf/sandbox-perf-working-set.sh" in smoke["run"]
    exact = steps["Test exact published assets from the platform package"]
    required = {"KVM", "EXEC", "CLUSTER_STUB", "CLUSTER_REAL", "ORCH", "PROXY", "BUILDER", "RUNTASK", "CONNECTOR_E2E", "GUEST_RUNTIME"}
    assert {key.removeprefix("REQUIRE_") for key, value in exact["env"].items() if key.startswith("REQUIRE_") and value == "1"} == required
    assert exact["run"] == "bash test/e2e/run_all.sh"
    assert exact["if"] == "inputs.mode == 'exact-assets'"
    print("hosted-workflows: runner matrix, trusted bootstrap, ephemeral paths and required coverage PASS")


if __name__ == "__main__":
    check()
    subprocess.run([sys.executable, str(Path(__file__).with_name("test-bootstrap.py"))], check=True)
