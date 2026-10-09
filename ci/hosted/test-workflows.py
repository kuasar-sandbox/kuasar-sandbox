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


def check_helper_materialization():
    """Execute the workflow's exact materializer with real Git and local fetches."""
    import contextlib
    import importlib
    import importlib.util
    from unittest.mock import patch

    steps = load("aggregate-release.yml")["jobs"]["helper-build"]["steps"]
    script = next(step["run"] for step in steps
                  if step.get("name") == "Materialize exact helper inputs without running a compiler")
    program = script.split("python3 -B - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    assert "cp -a inputs/fetched/test-sources" not in script
    sys.path.insert(0, str(ROOT / "ci/integration"))
    build = importlib.import_module("build-artifacts")
    spec = importlib.util.spec_from_file_location("workflow_cache_scope", ROOT / "ci/hosted/cache-scope.py")
    scope = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scope)
    with tempfile.TemporaryDirectory(prefix="helper-materialize-") as temporary:
        directory = Path(temporary)
        environment = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_TERMINAL_PROMPT": "0", "GITHUB_REPOSITORY": "kuasar-sandbox/kuasar-sandbox",
                       "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
                       "GITHUB_RUN_ID": "fixture", "GITHUB_EVENT_PATH": str(directory / "event.json")}
        (directory / "event.json").write_text('{"inputs": {}}')

        def git(path, *arguments):
            return subprocess.check_output(["git", "-C", str(path), *arguments], text=True).strip()

        def repository(path, owner):
            path.mkdir(parents=True)
            git(path, "init", "--quiet", "--template=")
            for key, value in (("user.name", "Chen Xiaohui"), ("user.email", "graych@gmail.com"),
                               ("commit.gpgsign", "false")):
                git(path, "config", "--local", key, value)
            git(path, "remote", "add", "origin", "https://github.com/" + scope.OWNERS[owner] + ".git")
            (path / "source.txt").write_text("product input\n")
            git(path, "add", "source.txt")
            git(path, "commit", "--quiet", "-m", "fixture product")
            return git(path, "rev-parse", "HEAD")

        with patch.dict(os.environ, environment):
            pins, product_pins = {}, {}
            for owner in sorted(set(build.artifacts.OWNERS) - {"platform"}):
                upstream = directory / "upstream" / owner
                product_pins[owner] = repository(upstream, owner)
                (upstream / "source.txt").write_text("independent helper test input\n")
                git(upstream, "add", "source.txt")
                git(upstream, "commit", "--quiet", "-m", "fixture test")
                pins[owner] = git(upstream, "rev-parse", "HEAD")
            fetched = []
            original_run = build.run

            def local_run(command, **kwargs):
                command = [str(argument) for argument in command]
                if command[0] == "git" and "fetch" in command:
                    owner = Path(command[2]).name
                    assert command[3:7] == ["fetch", "--quiet", "--depth=1", "origin"], command
                    fetched.append((owner, command[7]))
                    command[6] = "file://" + str(directory / "upstream" / owner)
                original_run(command, **kwargs)

            cases = {"valid": pins, "missing-owner": {key: value for key, value in pins.items() if key != "sandboxer"},
                     "malformed": {**pins, "sandboxer": "not-a-commit"}, "extra-owner": {**pins, "other": "e" * 40},
                     "missing-file": None}
            for name, declared in cases.items():
                case = directory / name
                sources = case / "helper-work"
                platform_sha = repository(sources / "platform", "platform")
                (case / "control").symlink_to(ROOT, target_is_directory=True)
                inputs = case / "inputs/fetched"
                inputs.mkdir(parents=True)
                (inputs / "product-revisions.json").write_text(json.dumps(product_pins))
                if declared is not None:
                    (inputs / "test-revisions.json").write_text(json.dumps(declared))
                fetched.clear()
                with contextlib.chdir(case), patch.object(build, "run", side_effect=local_run):
                    if name != "valid":
                        try:
                            exec(compile(program, "aggregate-release.yml helper materializer", "exec"), {})
                        except (ValueError, FileNotFoundError):
                            pass
                        else:
                            raise AssertionError("invalid helper pins accepted: " + name)
                        assert not fetched and set(path.name for path in sources.iterdir()) == {"platform"}, name
                        continue
                    exec(compile(program, "aggregate-release.yml helper materializer", "exec"), {})
                assert fetched == sorted(pins.items())
                for owner, sha in pins.items():
                    assert git(sources / owner, "rev-parse", "HEAD") == sha != product_pins[owner]
                    assert (sources / owner / "source.txt").read_text() == "independent helper test input\n"
                (sources / ".ci").mkdir()
                (sources / ".ci/test-revisions.json").write_text(json.dumps(pins))
                expected = {**pins, "platform": platform_sha}
                with patch.object(scope, "public_main", side_effect=lambda repo, sha: expected[
                        "platform" if repo.endswith("/kuasar-sandbox") else repo.split("/")[-1]] == sha):
                    receipt = scope.decide(sources, case / "scope.json")
                assert receipt["scope"] == receipt["namespace"] == "trusted"
                assert {row["path"]: row["sha"] for row in receipt["sources"]} == expected
                assert all(row["clean"] and row["on_main"] for row in receipt["sources"])
    print("helper materialization: independent test pins and 4 invalid-input cases passed")



def check():
    entry = load("ci-entry.yml")["jobs"]
    integration = load("integration-tests.yml")["jobs"]
    lanes = load("integration-architecture.yml")["jobs"]
    # Evaluate real allocation guards: candidate inputs, provider publicity and
    # failure/finalization cannot authorize a private or mismatched actual caller.
    count = 0
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        name = path.name
        for job_name, job in load(name)["jobs"].items():
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
                    expected = "ubuntu-24.04" if (name, job_name) == ("aggregate-release.yml", "collect") else "ubuntu-latest"
                    if (name, job_name) == ("integration-tests.yml", "source-checks"):
                        expected = "ubuntu-24.04"
                    if name == "workbench-216-validation.yml":
                        expected = {"prepare": "ubuntu-24.04", "source-checks": "ubuntu-24.04", "cold-x86": "ubuntu-24.04", "warm-x86": "ubuntu-24.04",
                                    "cold-arm": "ubuntu-24.04-arm", "warm-arm": "ubuntu-24.04-arm",
                                    "helpers-x86": "ubuntu-24.04", "prepare-integration-x86": "ubuntu-24.04",
                                    "e2e-x86": "ubuntu-24.04", "performance-x86": "ubuntu-24.04",
                                    "helpers-arm": "ubuntu-24.04-arm", "prepare-integration-arm": "ubuntu-24.04-arm",
                                    "e2e-arm": "ubuntu-24.04-arm", "validation-results": "ubuntu-24.04",
                                    "legacy-x86": "ubuntu-latest", "legacy-arm": "ubuntu-latest",
                                    "legacy-readers-arm": "ubuntu-24.04-arm", "legacy-source-control": "ubuntu-24.04",
                                    "prepare-legacy-x86": "ubuntu-24.04", "prepare-legacy-arm": "ubuntu-24.04",
                                    "e2e-legacy-x86": "ubuntu-24.04", "e2e-legacy-arm": "ubuntu-24.04-arm",
                                    "performance-legacy-x86": "ubuntu-24.04"}[job_name]
                    assert job["runs-on"] == expected, (name, job["runs-on"])
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
    assert "github.event_name" in load("ci-entry.yml")["concurrency"]["group"]
    assert any("validate-source-set.sh" in step.get("run", "") for step in integration["results"]["steps"])
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
        for name in ("build", "prepare", "e2e", "performance"):
            assert expression(lanes[name]["runs-on"], {"inputs": {"arch": arch}}) == runner
    building = {step.get("name"): step for step in lanes["build"]["steps"]}
    for name in ("Build trusted native archive readers in Workbench", "Build the affected delta and test helpers once"):
        assert building[name]["uses"] == "./framework/.github/actions/workbench"
        assert building[name]["with"]["selection"] == "plan/workbench.json"
        assert "env" not in building[name]
    assert building["Build trusted native archive readers in Workbench"]["with"]["cache"] == "false"
    assert "--materialize-only" in building["Fetch the exact private source and helper set without a compiler"]["run"]
    assert "--materialized" in building["Build the affected delta and test helpers once"]["with"]["run"]
    assert "artifact-cross" not in json.dumps(lanes["build"])
    for name in ("prepare", "e2e", "performance"):
        steps = {step.get("name"): step for step in lanes[name]["steps"]}
        assert steps["Download trusted native archive readers"]["with"]["name"] == "integration-readers-${{ inputs.arch }}-${{ github.run_id }}"
        assert "sha256sum --check SHA256SUMS" in steps["Verify and select the prebuilt archive readers"]["run"]
    aggregate = load("aggregate-release.yml")["jobs"]
    assert aggregate["helper-build"]["needs"] == "prepare"
    assert aggregate["collect"]["needs"] == ["prepare", "helper-build"]
    assert aggregate["workbench-build"]["needs"] == ["prepare", "collect"]
    assert aggregate["helper-build"]["strategy"]["matrix"]["include"] == [
        {"arch": "x86_64", "runner": "ubuntu-24.04"}, {"arch": "aarch64", "runner": "ubuntu-24.04-arm"}]
    assert aggregate["release-asset-validation"]["with"]["workbench_selection_artifact"] == "aggregate-sources-${{ github.run_id }}"
    assert "helper-build" not in json.dumps(aggregate["prepare"])
    helper = next(step for step in aggregate["helper-build"]["steps"] if step.get("uses") == "./control/.github/actions/workbench")
    assert '--arch "$TARGET_ARCH"' in helper["with"]["run"]
    assert "GH_TOKEN" not in helper["with"]["run"]
    check_helper_materialization()
    assert integration["results"]["needs"] == ["resolve", "x86_64", "aarch64", "source-checks", "workbench-native", "workbench-release"]
    native = integration['workbench-native']
    assert native['needs'] == 'resolve'
    assert {(item['arch'], item['runner']) for item in native['strategy']['matrix']['include']} == {
        ('x86_64', 'ubuntu-24.04'), ('aarch64', 'ubuntu-24.04-arm')}
    assert 'continue-on-error' not in json.dumps(native)
    released = integration['workbench-release']
    assert released['needs'] == 'resolve'
    assert released['strategy']['matrix'] == native['strategy']['matrix']
    release_script = next(step['run'] for step in released['steps'] if step.get('name', '').startswith('Qualify staged'))
    assert "matrix.arch" in release_script and '--artifact-only' in release_script
    assert 'continue-on-error' not in json.dumps(released)
    assert 'KUASAR_CI_APP_PRIVATE_KEY' not in json.dumps(released)
    for step in released['steps']:
        if 'actions/checkout@' in step.get('uses', ''):
            assert step['with']['ref'] == '${{ needs.resolve.outputs.framework_sha }}'
            assert step['with']['persist-credentials'] is False
    required = integration['results']['steps'][0]
    assert required['env']['WORKBENCH_RELEASE_RESULT'] == '${{ needs.workbench-release.result }}'
    script = required['run']
    for mode, value, success in [('exact-assets', 'success', True), ('exact-assets', 'skipped', False),
                                 ('exact-assets', 'failure', False), ('source', 'skipped', True),
                                 ('source', 'success', False), ('', 'skipped', False)]:
        environment = dict(os.environ, RESOLVE_RESULT='success', X86_RESULT='success', ARM_RESULT='success',
                           SOURCE_RESULT='success', WORKBENCH_SELECTED='false', WORKBENCH_RESULT='skipped',
                           INTEGRATION_MODE=mode, WORKBENCH_RELEASE_RESULT=value)
        outcome = subprocess.run(['bash', '-c', script], env=environment, capture_output=True)
        assert (outcome.returncode == 0) == success, (mode, value, outcome.stderr)
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
    source_job = integration["source-checks"]
    assert source_job["needs"] == "resolve"
    assert "strategy" not in source_job
    source_steps = {step.get("name"): step for step in source_job["steps"]}
    source_fetch = source_steps["Fetch exact test sources without a compiler"]["run"]
    assert "--materialize-only" in source_fetch
    assert "--profile source" not in json.dumps(source_job) and "taskset" not in json.dumps(source_job)
    system = source_steps["Run the complete required source gate in Workbench system mode"]["run"]
    assert "start --mode system" in system
    assert system.index('chown -hR "$source_uid:$source_gid" /src /work/home /build\n') < system.index("run-source-checks.py")
    assert 'chgrp "$source_gid" /output\n' in system
    assert 'chmod 2775 /output\n' in system
    assert 'setpriv --reuid "$source_uid" --regid "$source_gid" --groups "$source_docker_gid"' in system
    assert '--inh-caps=-all --ambient-caps=-all' in system
    assert 'source_docker_gid=$(stat -c %g /run/docker.sock)' in system
    assert 'docker --host unix:///run/docker.sock info' in system
    assert '[ "$(id -u)" -ne 0 ]' in system
    assert 'visudo -cf /etc/sudoers.d/kuasar-source' in system
    assert "--selection plan/workbench.json" in system
    assert "bash -euo pipefail -c \"$source_script\"" in system
    assert "<<'WORKBENCH_SOURCE'" in system
    assert "sudo -n true" in system
    assert "ip netns add ks-source-probe" in system and "ip netns del ks-source-probe" in system
    assert "ip tuntap add dev ks-source-probe mode tap" in system
    assert "ip link del dev ks-source-probe" in system and "trap - EXIT" in system
    assert "install_readers" in system and "install_runtime_reader" in system
    assert "--materialized" in system and "--phase" not in system
    assert "/output/source-result.json" in system
    assert "cache" not in system
    cleanup = source_steps["Stop the owned system instance and retain safe source evidence"]
    assert cleanup["if"] == "always()" and "workbench.py finish" in cleanup["run"]
    for step in source_job["steps"]:
        assert "GH_TOKEN" not in json.dumps(step) and "create-github-app-token" not in json.dumps(step)
        if "actions/upload-artifact@" in step.get("uses", ""):
            assert "source-checks" not in step["with"]["path"], "candidate sources must not be uploaded directly"
            assert "source-system-evidence" in step["with"]["path"]
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
