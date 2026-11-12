#!/usr/bin/env python3
"""Materialize exact test pins, then run the complete source gate in Workbench."""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import artifacts
import source_inputs
import go_workspace
import source_lane

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("artifact_build", Path(__file__).with_name("build-artifacts.py"))
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
spec = importlib.util.spec_from_file_location("source_documentation", Path(__file__).with_name("check-source-documentation.py"))
source_documentation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source_documentation)


SOURCE_RECORD = ".source-checks.json"
TEST_SCRIPT_EXECUTOR = "/inputs/release/ci/integration/run-source-checks.py"

# Run unittest modules in a fresh interpreter: the executor's trusted imports
# must not replace the candidate modules the regression intends to exercise.
# importlib also handles maintained test names containing '-', which unittest
# discovery silently omits because they are not Python identifier names.
UNITTEST_MODULE = '''
import importlib.util
from pathlib import Path
import sys
import unittest

path = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(path.parent))
sys.argv = [str(path)]
spec = importlib.util.spec_from_file_location(path.stem, path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
try:
    spec.loader.exec_module(module)
except SystemExit as error:
    raise RuntimeError('changed unittest module exited before discovery: ' + str(path)) from error
suite = unittest.defaultTestLoader.loadTestsFromModule(module)
if not suite.countTestCases():
    raise SystemExit('changed unittest module discovered zero tests: ' + str(path))
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
'''

# Standalone contracts use assertions instead of unittest. Count assertions
# actually reached in the selected script, not merely definitions or logging
# calls that an AST entrypoint guess could mistake for running a test.
STANDALONE_MODULE = '''
import ast
from pathlib import Path
import runpy
import sys

path = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(path.parent))
sys.argv = [str(path)]
text = path.read_text()
lines = text.splitlines()
# An inline assertion shares its line event with an if/def statement that may
# not execute it. Only an assertion on its own statement line proves coverage.
assertions = {node.lineno for node in ast.walk(ast.parse(text)) if isinstance(node, ast.Assert)
              and not lines[node.lineno - 1][:node.col_offset].strip()}
executed = set()

def trace(frame, event, argument):
    if frame.f_code.co_filename != str(path):
        return None
    if event == 'line' and frame.f_lineno in assertions:
        executed.add(frame.f_lineno)
    return trace

sys.settrace(trace)
try:
    try:
        runpy.run_path(str(path), run_name='__main__')
    except SystemExit as error:
        if error.code not in (None, 0):
            raise
finally:
    sys.settrace(None)
if not executed:
    raise SystemExit('changed standalone test executed zero assertions: ' + str(path))
print('source standalone assertions executed: ' + str(len(executed)), flush=True)
'''


def source_records(plan):
    if "source_scope" in plan:
        # Documentation-only companions and guide metadata need packaging
        # validation, not unrelated component unit/race suites.
        selected = {owner for owner, paths in plan["source_scope"]["changes"].items()
                    if any(source_lane.go_test(plan["test_revisions"][owner]["repository"], path) or
                           source_lane.test_script(plan["test_revisions"][owner]["repository"], path) or
                           source_lane.fixture(plan["test_revisions"][owner]["repository"], path)
                           for path in paths)}
        # The platform contracts use fixed component readers and guide inputs.
        # Keep those sources available without selecting their source gates.
        owners = set(artifacts.OWNERS)
    else:
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
    start = time.monotonic()
    action = {'name': 'restore-fixed-inputs', 'exit_code': 1}
    try:
        source_inputs.copy_run_inputs(plan, records, sources)
        action['exit_code'] = 0
    except subprocess.CalledProcessError as error:
        action['exit_code'] = result['exit_code'] = error.returncode
        raise
    finally:
        action['wall_seconds'] = time.monotonic() - start
        result['actions'].append(action)
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


def workspace(sources, owner, env):
    if owner in ('sandboxer', 'orchestrator'):
        return go_workspace.helper_environment(sources, owner, env, source_checks=True)
    if owner in go_workspace.DEPENDENCIES:
        return go_workspace.configure(sources, [owner], env, adapt_requirements=True)
    return dict(env, GOWORK='off')


def changed_test_checks(plan, sources):
    """Keep the changed-test coverage reconstructible without a source tree."""
    checks = []
    for owner, paths in sorted(plan["source_scope"]["changes"].items()):
        repository = plan["test_revisions"][owner]["repository"]
        for path in paths:
            if source_lane.test_script(repository, path):
                checks.append((owner + "-test-script:" + path,
                               ["python3", "-B", TEST_SCRIPT_EXECUTOR,
                                "--test-script", repository, path], sources / owner))
    return checks


def selected_test_blob(root, path, *, allow_deleted):
    """Read an exact regular Git blob, including a changed helper's driver."""
    relative = Path(path)
    artifacts.require(not relative.is_absolute() and
                      all(part not in ("", ".", "..") for part in path.split("/")),
                      "invalid source test path")
    source = root / relative
    artifacts.require(not any((root / parent).is_symlink() for parent in (relative, *relative.parents)),
                      "symbolic link in changed source test: " + path)
    entry = subprocess.check_output(["git", "ls-tree", "-z", "HEAD", "--", path], cwd=root)
    if not entry:
        artifacts.require(allow_deleted, "missing source test driver: " + path)
        artifacts.require(not source.exists() and not source.is_symlink(),
                          "deleted source test has an unexpected working file: " + path)
        print("source test deleted from the selected tree: " + path, flush=True)
        return None
    artifacts.require(entry.count(b"\0") == 1 and entry.endswith(b"\0"), "ambiguous source test tree entry")
    header, recorded = entry[:-1].split(b"\t", 1)
    mode, kind, identity = header.split()
    artifacts.require(recorded.decode() == path and kind == b"blob" and mode in (b"100644", b"100755")
                      and source.is_file(), "missing regular source test: " + path)
    data = source.read_bytes()
    actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest().encode()
    artifacts.require(actual == identity, "changed source test differs from its selected Git blob: " + path)
    return data


def run_test_script(repository, path, root=None):
    """Run one admitted test, or record its deletion from the selected tree."""
    artifacts.require(source_lane.test_script(repository, path), "unsupported source test script: " + path)
    root = (Path.cwd() if root is None else Path(root)).resolve()
    data = selected_test_blob(root, path, allow_deleted=True)
    driver = source_lane.test_driver(repository, path)
    if driver != path:
        artifacts.require(source_lane.test_script(repository, driver), "unsupported source test driver: " + driver)
        print("source test driver: " + path + " -> " + driver, flush=True)
        # A deleted helper still needs its caller exercised: otherwise a
        # lingering reference could be accepted without executing the suite.
        data = selected_test_blob(root, driver, allow_deleted=False)
        path = driver
    elif data is None:
        return 0
    relative = Path(path)
    if relative.suffix == ".py":
        tree = ast.parse(data, filename=path)
        unittest_module = any(
            isinstance(node, ast.Import) and any(alias.name == "unittest" or
                                                 alias.name.startswith("unittest.") and not alias.name.startswith("unittest.mock")
                                                 for alias in node.names) or
            isinstance(node, ast.ImportFrom) and node.module is not None and
            (node.module == "unittest" and any(alias.name != "mock" for alias in node.names) or
             node.module.startswith("unittest.") and not node.module.startswith("unittest.mock"))
            for node in ast.walk(tree))
        if unittest_module:
            command = ["python3", "-B", "-c", UNITTEST_MODULE, path]
        else:
            artifacts.require(any(isinstance(node, ast.Assert) for node in ast.walk(tree)),
                              "source test has no supported executable assertion: " + path)
            command = ["python3", "-B", "-c", STANDALONE_MODULE, path]
    else:
        artifacts.require(relative.suffix == ".sh", "unsupported source test interpreter")
        command = ["bash", path]
    if repository == "kuasar-sandbox/kuasar-sandbox" and path.startswith("test/perf/"):
        command = ["sudo", "-n", "-E", *command]
    return subprocess.run(command, cwd=root).returncode


def checks_for(plan, sources, group="all"):
    artifacts.require(group in ("all", "A", "B"), "unknown source check group")
    selected, _ = source_records(plan)
    scope = plan.get("source_scope")
    checks = [("platform-contracts", ["make", "test-ci-tools", "test-release-tools"], sources / "platform")]
    if source_documentation.required(plan):
        checks.append(("source-documentation-package",
                       ["python3", "-B", "/inputs/release/ci/integration/check-source-documentation.py",
                        "--plan", "/src/.source-plan.json", "--sources", "/src",
                        "--output", "/output/source-documentation.json"], sources / "platform"))
    if scope is None or any(path.startswith("test/perf/") for path in scope["changes"].get("platform", [])):
        # These existing TAP/netns fixtures require actual root, while
        # the other contracts include ordinary-UID permission assertions.
        checks.append(("platform-privileged-perf-contracts", ["sudo", "-n", "-E", "make", "test-perf-tools"],
                       sources / "platform"))
    # Each exact owner revision remains authoritative for its mixed source
    # checks. Published pins need no new CLI or reconstructed command list.
    for owner in ("connector", "sandboxer", "orchestrator"):
        if owner in selected:
            if scope is not None and owner == "connector":
                # Its existing source entry covers selected race/BPF packages;
                # the source-only lane must also exercise changed cmd tests.
                checks.append(("connector-go-tests", ["env", "CGO_ENABLED=0", "go", "test", "-count=1", "./..."],
                               sources / owner))
            checks.append((owner + "-unit-race-vet", ["bash", "scripts/ci-source-checks.sh"], sources / owner))
    if "accelerator" in selected:
        if scope is None:
            checks.append(("accelerator-fixtures", ["make", "test-e2e-scripts"], sources / "accelerator"))
        else:
            checks.append(("accelerator-source-tests", ["make", "test", "vet"], sources / "accelerator"))
            checks.append(("accelerator-no-rocksdb-tests",
                           ["env", "CGO_ENABLED=0", "go", "test", "-count=1", "-tags", "no_rocksdb", "./..."],
                           sources / "accelerator"))
    if scope is not None and "guest-runtime" in selected:
        checks.append(("guest-runtime-source-tests", ["make", "test", "vet"], sources / "guest-runtime"))
    if scope is None and selected & {"sandboxer", "platform"}:
        checks.append(("uffd-source-benchmark", ["bash", "test/perf/uffd-performance-gate.sh"], sources / "platform"))
    if scope is not None:
        checks.extend(changed_test_checks(plan, sources))
    if group == "all":
        return checks
    return [check for check in checks
            if ((check[0].startswith("orchestrator-") if scope is not None else
                 check[0] == "orchestrator-unit-race-vet")) == (group == "B")]


def execute(plan, sources, output, *, materialized=False, group="all"):
    result = new_result(plan, "all")
    result["group"] = group
    try:
        artifacts.check_plan(plan)
        env = environment()
        # Workbench supplies /build/t. Tests own their state below this short
        # root; an extra random parent would exhaust existing Unix socket paths.
        env["TMPDIR"] = env.get("TMPDIR") or "/tmp"
        env.update(ORG=str(sources), TARGET_ARCH="x86_64", PYTHONDONTWRITEBYTECODE="1",
                   REQUIRE_WORKING_SET_PRIVILEGED="1")
        result["arch"] = "x86_64"
        result["workbench"] = {"image_id": env.get("KUASAR_WORKBENCH_IMAGE_ID"),
                               "framework_sha": env.get("KUASAR_WORKBENCH_FRAMEWORK_SHA")}
        if materialized:
            verify_sources(plan, sources, result, env)
        else:
            checkout_sources(plan, sources, result)
        for name, command, directory in checks_for(plan, sources, group):
            owner = "sandboxer" if name == "uffd-source-benchmark" else directory.name
            selected = workspace(sources, owner, env)
            action(result, "checks", name, command, directory, selected)
        result.update(conclusion="success", exit_code=0)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        result["error"] = str(error)
        raise
    finally:
        save_result(output, result)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--test-script"]:
        parser = argparse.ArgumentParser(description="Run a changed source test from the selected Git tree")
        parser.add_argument("--test-script", nargs=2, metavar=("REPOSITORY", "PATH"), required=True)
        args = parser.parse_args()
        try:
            status = run_test_script(*args.test_script)
        except (OSError, ValueError, SyntaxError, subprocess.CalledProcessError) as error:
            print(f"source test: {error}", file=sys.stderr)
            status = 1
        sys.exit(status if status >= 0 else 128 - status)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--group", choices=("all", "A", "B"), default="all")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--materialize-only", action="store_true", help="fetch exact test sources on the trusted host, without compilers")
    mode.add_argument("--materialized", action="store_true", help="verify and use the task's existing exact test sources")
    args = parser.parse_args()
    try:
        source_inputs.INPUT_ARCHIVE = args.plan.resolve().parent / "source-inputs.tar"
        plan = json.loads(args.plan.read_text())
        if args.materialize_only:
            materialize(plan, args.sources.resolve(), args.output.resolve())
        else:
            execute(plan, args.sources.resolve(), args.output.resolve(), materialized=args.materialized, group=args.group)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"source checks: {error}", file=sys.stderr)
        status = 1
        if args.output.is_file():
            status = json.loads(args.output.read_text()).get("exit_code", 1) or 1
        sys.exit(status if status >= 0 else 128 - status)
