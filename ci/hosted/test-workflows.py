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
            "base": {"ref": "main", "sha": sha, "repo": {"full_name": repository}},
            "head": {"sha": sha, "repo": {"full_name": repository}}}}))
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
        env.update(GITHUB_EVENT_NAME="pull_request", GITHUB_SHA=sha)
        for overrides, allowed in (({}, True), ({"GITHUB_SHA": "2" * 40}, False),
                                   ({"TRUSTED_WORKFLOW_SHA": "2" * 40}, False),
                                   ({"CANDIDATE_REPOSITORY": "kuasar-sandbox/connector"}, False),
                                   ({"CANDIDATE_BASE_SHA": "2" * 40}, False),
                                   ({"CANDIDATE_HEAD_SHA": "2" * 40}, False),
                                   ({"COMPANION_CANDIDATES": '[{}]'}, False)):
            result = subprocess.run(["bash", "-c", script], env={**env, **overrides},
                                    capture_output=True, text=True, timeout=5)
            assert (result.returncode == 0) == allowed, (overrides, result.stderr)
        original = event.read_text()
        for fault in ("fork", "draft", "closed"):
            data = json.loads(original)
            if fault == "fork": data["pull_request"]["head"]["repo"]["full_name"] = "outside/fork"
            elif fault == "draft": data["pull_request"]["draft"] = True
            else: data["pull_request"]["state"] = "closed"
            event.write_text(json.dumps(data))
            result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=5)
            assert result.returncode != 0, fault
        env['GITHUB_EVENT_NAME'] = 'push'
        result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=5)
        assert result.returncode != 0



def check():
    entry = load("ci-entry.yml")["jobs"]
    integration = load("integration-tests.yml")["jobs"]
    lanes = load("integration-architecture.yml")["jobs"]
    # Evaluate real allocation guards: candidate inputs, provider publicity and
    # failure/finalization cannot authorize a private or mismatched actual caller.
    count = 0
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        name = path.name
        for job in load(name)["jobs"].values():
            # A thin reusable caller allocates no runner itself. Every actual
            # allocation, and guarded nested invocation, is evaluated below.
            if "runs-on" not in job and "if" not in job:
                continue
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
                if not str(job["runs-on"]).startswith("${{"):
                    assert job["runs-on"] == "ubuntu-latest", (name, job["runs-on"])
    check_request_rejection()
    caller = load("ci.yml")
    assert caller["permissions"] == {"contents": "read", "pull-requests": "read"}
    platform = caller["jobs"]["ci"]
    assert platform["uses"] == "./.github/workflows/integration-tests.yml"
    assert "secrets" not in platform and "permissions" not in platform
    assert platform["with"]["candidate_sha"] == "${{ github.sha }}"
    context = {"github": {"repository": "kuasar-sandbox/kuasar-sandbox", "event_name": "pull_request",
               "event": {"repository": {"visibility": "public", "full_name": "kuasar-sandbox/kuasar-sandbox"},
                         "pull_request": {"draft": False, "head": {"repo": {"full_name": "kuasar-sandbox/kuasar-sandbox"}}}}},
               "false": False}
    assert expression(platform["if"], context) is True
    assert expression(integration["results"]["name"], context) == "finalize"
    for event in ("pull_request_target", "workflow_dispatch"):
        context["github"]["event_name"] = event
        assert expression(platform["if"], context) is False
        assert expression(integration["results"]["name"], context) == "results"
    context["github"]["event_name"] = "pull_request_target"
    context["github"]["event"]["pull_request"]["head"]["repo"]["full_name"] = "outside/fork"
    fork = caller["jobs"]["trusted-fork"]
    assert expression(fork["if"], context) is True
    assert fork["name"] == "ci" and fork["uses"].endswith("/ci-entry.yml@main")
    context["github"]["event_name"] = "pull_request"
    assert expression(platform["if"], context) is False
    assert expression(fork["if"], context) is False
    assert "github.event_name" in load("integration-tests.yml")["concurrency"]["group"]
    assert any("validate-source-set.sh" in step.get("run", "") for step in integration["results"]["steps"])
    finalizer = caller['jobs']['framework-result']
    assert finalizer['needs'] == 'ci' and finalizer['if'].startswith('always()')
    assert finalizer['permissions'] == {'contents': 'read', 'pull-requests': 'read', 'statuses': 'write'}
    assert 'secrets' not in finalizer and 'KUASAR_CI_APP_PRIVATE_KEY' not in json.dumps(finalizer)
    assert finalizer['steps'][0]['with']['ref'] == '${{ github.sha }}'
    assert finalizer['steps'][0]['with']['persist-credentials'] is False
    assert finalizer['steps'][-1]['env']['INTEGRATION_RESULT'] == '${{ needs.ci.result }}'
    assert 'finalize_framework.py' in finalizer['steps'][-1]['run']
    context['github']['event']['pull_request']['head']['repo']['full_name'] = 'kuasar-sandbox/kuasar-sandbox'
    context['github']['event_name'] = 'pull_request'
    assert expression(finalizer['if'], context) is True
    for event in ('pull_request_target', 'push', 'workflow_dispatch'):
        context['github']['event_name'] = event
        assert expression(finalizer['if'], context) is False
    context['github']['event_name'] = 'pull_request'
    context['github']['event']['pull_request']['draft'] = True
    assert expression(finalizer['if'], context) is False
    context['github']['event']['pull_request']['draft'] = False
    context['github']['event']['pull_request']['head']['repo']['full_name'] = 'outside/fork'
    assert expression(finalizer['if'], context) is False
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
    for arch, runner in (("x86_64", "ubuntu-latest"), ("aarch64", "ubuntu-24.04-arm")):
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
    performance = (ROOT / "ci/integration/run-artifact-performance.py").read_text()
    assert 'test/e2e/e2e' in runtime and 'working-set-netns.sh' not in runtime
    assert "working-set-netns.sh" in performance and "PERF_ITERS" in performance
    assert lanes["performance"]["needs"] == ["build", "prepare"]
    assert lanes["result"]["needs"] == ["build", "prepare", "e2e", "performance"]
    for required in ("true", "false", ""):
        context = {"github": {"repository": "kuasar-sandbox/kuasar-sandbox", "event": {"repository": {
                    "visibility": "public", "full_name": "kuasar-sandbox/kuasar-sandbox"}}},
                   "needs": {"build": {"outputs": {"performance": required}}}}
        assert expression(lanes["performance"]["if"], context) == (required == "true")
    print(f"hosted-workflows: {count} non-public allocation checks; independent lanes, exact requests and required checks PASS")


if __name__ == "__main__":
    check()
    for name in ("test-bootstrap.py", "test-exact-assets-tools.py"):
        subprocess.run([sys.executable, str(Path(__file__).with_name(name))], check=True)
