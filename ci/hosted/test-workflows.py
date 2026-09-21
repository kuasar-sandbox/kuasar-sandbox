#!/usr/bin/env python3
"""Offline public-caller, independent lifecycle and credential boundary contracts."""
import ast
import json
import os
import tempfile
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]


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
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "always":
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "fromJSON":
            return json.loads(visit(node.args[0]))
        raise AssertionError(f"unsupported expression: {ast.dump(node)}")

    return visit(tree)


def load(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def check_request_rejection():
    """Run the real, credential-free request validator; routing is not authorization."""
    script = (ROOT / "ci/integration/validate-request.sh").read_text()
    sha = "1" * 40
    repository = "kuasar-sandbox/kuasar-sandbox"
    with tempfile.TemporaryDirectory() as directory:
        event = Path(directory) / "event.json"
        event.write_text(json.dumps({"repository": {"full_name": repository}, "pull_request": {
            "number": 1, "state": "open", "draft": False,
            "base": {"ref": "main", "sha": sha, "repo": {"full_name": repository}}, "head": {"sha": sha}}}))
        env = dict(os.environ, GITHUB_REPOSITORY=repository, TRUSTED_WORKFLOW_REPOSITORY=repository, TRUSTED_WORKFLOW_SHA=sha,
                   GITHUB_EVENT_NAME="pull_request_target", GITHUB_EVENT_PATH=str(event),
                   CANDIDATE_REPOSITORY=repository, CANDIDATE_SHA=sha, CANDIDATE_BASE_SHA=sha,
                   CANDIDATE_HEAD_SHA=sha, CANDIDATE_BASE_REF="main", CANDIDATE_PR="1",
                   COMPANION_CANDIDATES="[]", INTEGRATION_MODE="source")
        cases = (({}, None), ({"INTEGRATION_MODE": "invalid"}, "mode must be source or exact-assets"),
                 ({"CANDIDATE_REPOSITORY": "kuasar-sandbox/connector"}, "inputs do not match"),
                 ({"CANDIDATE_HEAD_SHA": "2" * 40}, "inputs do not match"),
                 ({"INTEGRATION_MODE": "exact-assets"}, "requires workflow_dispatch"),
                 ({"TRUSTED_WORKFLOW_REPOSITORY": "outside/project"}, "implementation must come from"))
        for overrides, message in cases:
            result = subprocess.run(["bash", "-c", script], env={**env, **overrides},
                                    capture_output=True, text=True, timeout=5)
            assert (result.returncode == 0) == (message is None), (overrides, result.stderr)
            if message:
                assert message in result.stderr, (overrides, result.stderr)


def check():
    entry = load("ci-entry.yml")["jobs"]
    integration = load("integration-tests.yml")["jobs"]
    lanes = load("integration-architecture.yml")["jobs"]
    # Evaluate real allocation guards: candidate inputs, provider publicity and
    # failure/finalization cannot authorize a private or mismatched actual caller.
    count = 0
    for name in ("ci-entry", "integration-tests", "integration-architecture", "aggregate-release",
                 "daily-preview", "daily-preview-branch", "delete-preview", "preview-gc"):
        for job in load(name + ".yml")["jobs"].values():
            assert "if" in job, (name, "missing pre-allocation guard")
            for caller in ("kuasar-sandbox/accelerator", "kuasar-sandbox/kuasar-sandbox", "outside/repo"):
                for visibility in ("private", "internal", ""):
                    context = {"github": {"repository": caller, "event": {"repository": {
                        "full_name": caller, "visibility": visibility, "private": True}}},
                        "inputs": {"candidate_repository": "kuasar-sandbox/kuasar-sandbox", "arch": "aarch64", "mode": "source"}}
                    assert expression(job["if"], context) is False, (name, caller, visibility)
                    count += 1
                context["github"]["event"]["repository"].update(visibility="public", private=False, full_name="provider/other")
                assert expression(job["if"], context) is False, name
            if "runs-on" in job:
                assert "self-hosted" not in str(job["runs-on"]), name
    check_request_rejection()
    assert entry["e2e"]["uses"] == "./.github/workflows/integration-tests.yml"
    assert entry["e2e"]["with"]["candidate_repository"] == "${{ github.repository }}"
    for job in (entry["admission"], entry["finalize"]):
        checkout, bootstrap = job["steps"][:2]
        assert checkout["with"]["ref"] == "${{ job.workflow_sha }}"
        assert checkout["with"]["repository"] == "${{ job.workflow_repository }}"
        assert checkout["with"]["persist-credentials"] is False
        assert "--profile control" in bootstrap["run"]
    for arch in ("x86_64", "aarch64"):
        lane = integration[arch]
        assert lane["needs"] == "resolve"  # no other-architecture build barrier
        assert lane["uses"] == "./.github/workflows/integration-architecture.yml"
        assert lane["with"]["arch"] == arch
    assert lanes["prepare"]["needs"] == "build"
    assert lanes["e2e"]["needs"] == ["build", "prepare"]
    assert "matrix.shard" in lanes["e2e"]["concurrency"]["group"]
    assert "inputs.arch" in lanes["e2e"]["concurrency"]["group"]
    for arch, runner in (("x86_64", "ubuntu-24.04"), ("aarch64", "ubuntu-24.04-arm")):
        assert expression(lanes["e2e"]["runs-on"], {"inputs": {"arch": arch}}) == runner
    assert integration["results"]["needs"] == ["resolve", "x86_64", "aarch64", "source-checks"]
    for job in lanes.values():
        assert "KUASAR_CI_APP_PRIVATE_KEY" not in json.dumps(job)
        assert "continue-on-error" not in json.dumps(job)
        for step in job["steps"]:
            if "actions/checkout@" in step.get("uses", ""):
                assert step["with"]["repository"] == "kuasar-sandbox/kuasar-sandbox"
                assert step["with"]["ref"] == "${{ inputs.framework_sha }}"
                assert step["with"]["persist-credentials"] is False
    executor = json.dumps(lanes["e2e"])
    for forbidden in ("create-github-app-token", "go build", "cargo build", "source-owner.sh build", "src/platform"):
        assert forbidden not in executor, forbidden
    assert "run-artifact-tests.py" in executor
    assert "sparse-checkout" in executor
    assert "run-source-checks.py" in json.dumps(integration["source-checks"])
    source = (ROOT / "ci/integration/run-source-checks.py").read_text()
    assert "uffd-performance-gate.sh" in source and "ci-source-checks.sh" in source
    runtime = (ROOT / "ci/integration/run-artifact-tests.py").read_text()
    assert "working-set-netns.sh" in runtime and "PERF_ITERS" in runtime
    for flag in ("KVM", "EXEC", "CLUSTER_STUB", "CLUSTER_REAL", "ORCH", "PROXY", "BUILDER", "RUNTASK", "CONNECTOR_E2E", "GUEST_RUNTIME"):
        assert '"' + flag + '"' in runtime
    print(f"hosted-workflows: {count} non-public allocation checks; independent lanes, exact requests and required checks PASS")


if __name__ == "__main__":
    check()
    for name in ("test-bootstrap.py", "test-exact-assets-tools.py"):
        subprocess.run([sys.executable, str(Path(__file__).with_name(name))], check=True)
