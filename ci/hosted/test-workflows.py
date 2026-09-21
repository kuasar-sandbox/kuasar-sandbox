#!/usr/bin/env python3
"""Offline regression contracts for the visibility-only standard runner selection."""
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
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "fromJSON":
            return json.loads(visit(node.args[0]))
        raise AssertionError(f"unsupported expression: {ast.dump(node)}")

    return visit(tree)


def load(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def check_request_rejection(e2e):
    """Run the real, credential-free request validator; routing is not authorization."""
    script = next(step["run"] for step in e2e["steps"] if step["name"] == "Validate reusable workflow request")
    script = script.replace("${{ inputs.mode }}", "$TEST_MODE")
    sha = "1" * 40
    repository = "kuasar-sandbox/kuasar-sandbox"
    with tempfile.TemporaryDirectory() as directory:
        event = Path(directory) / "event.json"
        event.write_text(json.dumps({"repository": {"full_name": repository}, "pull_request": {
            "number": 1, "state": "open", "draft": False,
            "base": {"ref": "main", "sha": sha, "repo": {"full_name": repository}}, "head": {"sha": sha}}}))
        env = dict(os.environ, TRUSTED_WORKFLOW_REPOSITORY=repository, TRUSTED_WORKFLOW_SHA=sha,
                   GITHUB_EVENT_NAME="pull_request_target", GITHUB_EVENT_PATH=str(event),
                   CANDIDATE_REPOSITORY=repository, CANDIDATE_SHA=sha, CANDIDATE_BASE_SHA=sha,
                   CANDIDATE_HEAD_SHA=sha, CANDIDATE_BASE_REF="main", CANDIDATE_PR="1",
                   COMPANION_CANDIDATES="[]", TEST_MODE="source")
        cases = (({}, None), ({"TEST_MODE": "invalid"}, "mode must be source or exact-assets"),
                 ({"CANDIDATE_REPOSITORY": "kuasar-sandbox/connector"}, "inputs do not match"),
                 ({"CANDIDATE_HEAD_SHA": "2" * 40}, "inputs do not match"),
                 ({"TEST_MODE": "exact-assets"}, "requires workflow_dispatch"),
                 ({"TRUSTED_WORKFLOW_REPOSITORY": "outside/project"}, "implementation must come from"))
        for overrides, message in cases:
            result = subprocess.run(["bash", "-c", script], env={**env, **overrides},
                                    capture_output=True, text=True, timeout=5)
            assert (result.returncode == 0) == (message is None), (overrides, result.stderr)
            if message:
                assert message in result.stderr, (overrides, result.stderr)


def check():
    entry = load("ci-entry.yml")["jobs"]
    e2e = load("integration-tests.yml")["jobs"]["e2e"]
    legacy = ["self-hosted", "Linux", "X64", "kuasar-e2e", "kvm", "cgroup-v2"]
    shard_expr = e2e["strategy"]["matrix"]["shard"]
    for shard in ("core", "sandboxer", "orchestrator"):
        assert shard in shard_expr
    assert '["source"]' in shard_expr
    assert e2e["env"]["KUASAR_E2E_SHARD"] == "${{ matrix.shard }}"
    assert "inputs.mode == 'source' && 'e2e'" in e2e["name"]
    aggregate_runner = (ROOT / "test/e2e/run_all.sh").read_text()
    for shard in ("core", "sandboxer", "orchestrator"):
        assert shard in aggregate_runner
    repos = ("guest-runtime", "accelerator", "connector", "kuasar-sandbox", "orchestrator", "sandboxer")
    callers = tuple(f"kuasar-sandbox/{name}" for name in repos) + ("outside/kuasar-sandbox",)
    cases = 0
    for repository in callers:
        for visibility in ("private", "public", "private", "internal", ""):
            context = {"github": {"repository": repository, "event": {"repository": {
                "private": visibility != "public", "visibility": visibility}}}}
            for job in (entry["admission"], entry["finalize"]):
                expected = "ubuntu-latest" if visibility == "public" else "kuasar-control"
                assert expression(job["runs-on"], context) == expected
                for step in job["steps"][:2]:
                    assert expression(step["if"], context) == (visibility == "public")
            for mode in ("source", "exact-assets", "invalid"):
                for candidate in (*callers, ""):
                    context["inputs"] = {"mode": mode, "candidate_repository": candidate}
                    hosted = visibility == "public"
                    assert expression(e2e["runs-on"], context) == (["ubuntu-latest"] if hosted else legacy), context
                    assert expression(e2e["env"]["KUASAR_HOSTED"], context) == hosted, context
                    assert expression(e2e["timeout-minutes"], context) == (120 if mode == "exact-assets" else 180 if hosted else 60), context
                    context["env"] = {"KUASAR_HOSTED": str(hosted).lower()}
                    steps = {step["name"]: step for step in e2e["steps"]}
                    for name, enabled in (
                        ("Attach job-local source caches and test tools", hosted and mode == "source"),
                        ("Attach job-local exact-assets test tools", hosted and mode == "exact-assets"),
                        ("Attach runner caches and test tools", not hosted),
                        ("Bootstrap standard runner from trusted tooling", hosted),
                        ("Pull standard runner test images", hosted),
                    ):
                        assert expression(steps[name]["if"], context) == enabled, (name, context)
                    cases += 1

    check_request_rejection(e2e)

    assert entry["e2e"]["uses"] == "./.github/workflows/integration-tests.yml"
    assert entry["e2e"]["with"]["candidate_repository"] == "${{ github.repository }}"
    for job in (entry["admission"], entry["finalize"]):
        steps = job["steps"]
        checkout, bootstrap = steps[:2]
        assert checkout["with"]["ref"] == "${{ job.workflow_sha }}"
        assert checkout["with"]["repository"] == "${{ job.workflow_repository }}"
        assert checkout["with"]["persist-credentials"] is False
        assert bootstrap["run"] == "bash trusted/platform/ci/hosted/bootstrap.sh --profile control"
        assert bootstrap["if"] == checkout["if"]

    steps = {step["name"]: step for step in e2e["steps"]}
    names = list(steps)
    bootstrap_name = "Bootstrap standard runner from trusted tooling"
    assert steps[bootstrap_name]["run"] == 'bash trusted/platform/ci/hosted/bootstrap.sh --profile "$BOOTSTRAP_PROFILE"'
    assert steps[bootstrap_name]["env"] == {"BOOTSTRAP_PROFILE": "${{ inputs.mode }}"}
    assert names.index("Validate reusable workflow request") < names.index(bootstrap_name)
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
            assert step["with"]["path"] == "${{ steps.ci-metadata.outputs.path }}"
    for name in ("Assemble the five component source repositories", "Finalize source workspace"):
        assert 'source_dir="$KUASAR_SOURCE_CACHE_ROOT/$repo"' in steps[name]["run"]
    assert "repos=(accelerator connector guest-runtime orchestrator sandboxer)" in steps["Assemble the five component source repositories"]["run"]
    for name in ("Restore or build verified native artifacts", "Validate platform tooling", "Build and test source candidate"):
        assert steps[name]["shell"] == "bash"
        assert 'taskset -pc "$KUASAR_BUILD_CPUS" "$$"' in steps[name]["run"]
        assert steps[name]["if"] == "inputs.mode == 'source'"
    native = steps["Restore or build verified native artifacts"]["run"]
    assert 'source-owner.sh native-components "$owner"' in native
    build = steps["Build and test source candidate"]["run"]
    for required in ('source-owner.sh build "$owner"', 'source-owner.sh needs-uffd "$owner"',
                     'owner="${CANDIDATE_REPOSITORY##*/}"', 'bash "$runner"'):
        assert required in build
    smoke = steps["Run working-set performance smoke"]
    assert smoke["if"] == "inputs.mode == 'source'"
    assert smoke["env"]["PERF_ITERS"] == "1"
    assert "PERF_GROUPS" not in smoke["env"]
    assert "bash test/perf/working-set-netns.sh test/perf/sandbox-perf-working-set.sh" in smoke["run"]
    assert 'source-owner.sh needs-working-set "$owner"' in smoke["run"]
    exact = steps["Test exact published assets from the platform package"]
    required = {"KVM", "EXEC", "CLUSTER_STUB", "CLUSTER_REAL", "ORCH", "PROXY", "BUILDER", "RUNTASK", "CONNECTOR_E2E", "GUEST_RUNTIME"}
    assert {key.removeprefix("REQUIRE_") for key, value in exact["env"].items() if key.startswith("REQUIRE_") and value == "1"} == required
    assert exact["run"] == "bash test/e2e/run_all.sh"
    assert exact["if"] == "inputs.mode == 'exact-assets'"
    metadata = steps["Stage revision and timing metadata"]
    assert metadata["if"] == "always()"
    assert metadata["id"] == "ci-metadata"
    assert 'sudo find -P "$KUASAR_CI_DIR" -xdev' in metadata["run"]
    assert 'sudo tar --one-file-system -C "$KUASAR_CI_DIR" -cf - .' in metadata["run"]
    assert 'tar --no-same-owner -C "$upload_dir" -xf -' in metadata["run"]
    assert 'chmod -R u+rwX "$upload_dir"' in metadata["run"]
    assert names.index(exact["name"]) < names.index(metadata["name"]) < names.index("Upload revision and timing metadata")
    tools_name = "Attach job-local exact-assets test tools"
    assert steps[tools_name]["run"] == "bash trusted/platform/ci/hosted/exact-assets-tools.sh"
    assert names.index("Download exact aggregate bundle") < names.index("Validate and extract exact aggregate bundle") < names.index(tools_name) < names.index(exact["name"])
    images = steps["Pull standard runner test images"]["run"]
    assert 'source-owner.sh images "$owner"' in images
    assert "alpine:3.19" not in images and "alpine:3.20" not in images
    assert names.index(tools_name) < names.index("Pull standard runner test images") < names.index(exact["name"])
    validate = steps["Validate and extract exact aggregate bundle"]
    assert 'trusted/platform/release/aggregate-release.sh validate "$RELEASE_VERSION" release-bundle' in validate["run"]
    assert '"$RELEASE_VERSION" release-bundle release-install' in validate["run"]
    assert exact["working-directory"] == "release-install"
    assert exact["env"]["BIN"] == "${{ github.workspace }}/release-install/bin"
    # No private source token, sibling assembly, native/product build or source cache attach in exact mode.
    context = {"inputs": {"mode": "exact-assets", "pull_request_number": "0"}, "env": {"KUASAR_HOSTED": "true"}}
    for name in ("Create read-only source token", "Validate pull request integration set",
                 "Assemble the five component source repositories", "Finalize source workspace",
                 "Attach job-local source caches and test tools", "Restore or build verified native artifacts",
                 "Validate platform tooling", "Build and test source candidate", "Run working-set performance smoke"):
        assert not expression(steps[name]["if"], context), name
    materialize = steps["Materialize exact platform sources"]
    assert materialize["env"]["TRUSTED_SHA"] == "${{ job.workflow_sha }}"
    assert materialize["env"]["TRUSTED_REPOSITORY"] == "${{ job.workflow_repository }}"
    assert 'materialize "$TRUSTED_REPOSITORY" "$TRUSTED_SHA" trusted/platform' in materialize["run"]
    for name in (bootstrap_name, tools_name):
        assert "src/platform" not in steps[name]["run"]
    for name in ("docs", "aggregate-release", "daily-preview", "daily-preview-branch", "delete-preview", "preview-gc"):
        for job in load(name + ".yml")["jobs"].values():
            if "runs-on" in job:
                assert job["runs-on"] == "ubuntu-latest", name
    print(f"hosted-workflows: {cases} caller/visibility/mode/candidate combinations, trusted bootstrap, exact assets and required coverage PASS")


if __name__ == "__main__":
    check()
    for name in ("test-bootstrap.py", "test-exact-assets-tools.py"):
        subprocess.run([sys.executable, str(Path(__file__).with_name(name))], check=True)
