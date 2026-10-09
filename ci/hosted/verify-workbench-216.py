#!/usr/bin/env python3
"""Temporary #216 acceptance: frozen full builds and fresh-job native-cache packaging."""
from __future__ import annotations

import argparse
import ast
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ci/integration"))
import artifacts
import build_demo_wheels
import build_helpers
import transport

REPOSITORIES = {owner: "kuasar-sandbox/" + owner for owner in (
    "kuasar-sandbox", "accelerator", "connector", "guest-runtime", "sandboxer", "orchestrator")}
NATIVE = ("vmlinux", "erofs", "envd", "rocksdb", "cloud-hypervisor")
UNITS = ("accelerator", "connector", "sandboxer", "orchestrator", "runtime", "vmlinux")
VALIDATORS = ("accelerator", "sandboxer", "orchestrator")
NATIVE_PRODUCTS = {"vmlinux", "mkfs.erofs", "cloud-hypervisor"}
LEGACY_FRAMEWORK = "baae11385c26a55e1c888be4c5334fc0b0b6860d"
LEGACY_BOOTSTRAP_SHA256 = "4b6a50d20d06a8e9307a87df64f02632a2e183a38dec3630a8e4e1b9d530ccf8"
READER_FILES = ("mkfs.erofs", "fsck.erofs", "dump.erofs", "runtime-payloads.py", "erofs-readers.COPYING")
require = artifacts.require


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def output(command, **kwargs):
    return subprocess.check_output(list(map(str, command)), text=True, **kwargs).strip()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(artifacts.canonical(value) + b"\n")


def space_path(path, *, size=False):
    """Read a task path's filesystem counters without following source links."""
    path = path.absolute()
    row = {"path": str(path)}
    try:
        require(path.resolve() == path and not path.is_symlink(), "linked disk measurement path")
        row["exists"] = path.exists()
        measured = path
        while not measured.exists():
            measured = measured.parent
        counters = os.statvfs(measured)
        row["filesystem"] = {"measured_at": str(measured), "device": measured.stat().st_dev,
            "total_bytes": counters.f_blocks * counters.f_frsize,
            "free_bytes": counters.f_bfree * counters.f_frsize,
            "available_bytes": counters.f_bavail * counters.f_frsize,
            "total_inodes": counters.f_files, "available_inodes": counters.f_favail}
        if size and row["exists"]:
            # GNU du defaults to physical traversal: source symlinks and
            # nested mounts are not followed. Never scan the Docker store.
            measured_size = subprocess.run(["du", "--summarize", "--one-file-system", "--block-size=1", "--", path],
                                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            row["size_exit_code"] = measured_size.returncode
            if measured_size.returncode == 0:
                row["allocated_bytes"] = int(measured_size.stdout.split("\t", 1)[0])
            else:
                row["error"] = "task directory size could not be measured"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        row["error"] = type(error).__name__ + ": " + str(error)
    return row


def space_snapshot(phase, paths, *, docker=False, sizes=False):
    started = time.monotonic()
    record = {"phase": phase, "recorded_ns": time.time_ns(),
              "method": "stage snapshot, not peak; filesystem counters include other users of that filesystem; nested path sizes must not be added",
              "paths": {name: space_path(path, size=sizes) for name, path in paths.items()}}
    if docker:
        try:
            query = subprocess.run(["docker", "info", "--format", "{{.DockerRootDir}}"], text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            record["docker_info_exit_code"] = query.returncode
            root = Path(query.stdout.strip())
            require(query.returncode == 0 and root.is_absolute(), "Docker storage directory unavailable")
            record["docker_storage"] = space_path(root)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            record["docker_storage"] = {"error": type(error).__name__ + ": " + str(error)}
    record["measurement_seconds"] = time.monotonic() - started
    return record


def disk_snapshot(args):
    """Temporary host consumer measurements, outside candidate execution."""
    require(os.environ.get("GITHUB_ACTIONS") == "true" and os.getuid() != 0,
            "host disk snapshots require an ordinary Actions runner")
    sources = args.sources.absolute()
    destination = args.output.absolute()
    require(not destination.exists() and destination.resolve() == destination
            and not destination.is_relative_to(sources), "disk evidence requires a fresh host path outside sources")
    record = space_snapshot(args.phase, {"sources": sources,
        "workbench_state": Path(os.environ["RUNNER_TEMP"]) / "kuasar-workbench"}, docker=True, sizes=True)
    record.update(run_id=os.environ.get("GITHUB_RUN_ID"), job=os.environ.get("GITHUB_JOB"),
                  runner={name: os.environ.get(name) for name in ("ImageOS", "ImageVersion", "RUNNER_ARCH")})
    write(destination, record)
    print(json.dumps(record, sort_keys=True), flush=True)


def api(endpoint):
    return json.loads(output(["gh", "api", endpoint]))


def sha(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value), "expected an exact source SHA")
    return value


def check_framework(expected):
    require(output(["git", "-C", ROOT, "rev-parse", "HEAD"]) == sha(expected), "trusted framework checkout changed")


def candidate_request():
    names = ("repository", "sha", "pr", "base_sha", "base_ref", "head_sha")
    request = {name: os.environ.get("CANDIDATE_" + name.upper(), "") for name in names}
    companions = json.loads(os.environ.get("COMPANION_CANDIDATES", "[]"))
    require(isinstance(companions, list), "companion candidates must be an array")
    if not any(request.values()):
        require(not companions, "companions require an exact primary candidate")
        return None
    require(all(request.values()), "all existing primary candidate fields are required")
    require(re.fullmatch(r"[1-9][0-9]*", request["pr"]), "invalid primary pull request number")
    primary = {"repository": request["repository"], "pull_request_number": int(request["pr"]),
               "candidate_sha": request["sha"], "base_sha": request["base_sha"],
               "base_ref": request["base_ref"], "head_sha": request["head_sha"]}
    expected = set(primary)
    seen = set()
    for row in [primary, *companions]:
        require(isinstance(row, dict) and set(row) == expected, "invalid existing candidate record")
        require(row["repository"] in REPOSITORIES.values() and row["repository"] not in seen,
                "foreign or duplicate candidate repository")
        seen.add(row["repository"])
        require(row["base_ref"] == "main" and type(row["pull_request_number"]) is int
                and row["pull_request_number"] > 0, "task candidates must target main")
        for name in ("candidate_sha", "base_sha", "head_sha"):
            sha(row[name])
    return {"primary": primary, "companions": companions}


def admit_candidates(request):
    if request is None:
        return
    # Use the unchanged source-set validator for open PR, declaration, current
    # base/head/merge refs, two exact parents and companion branch admission.
    primary = request["primary"]
    environment = {**os.environ, "CALLER_TOKEN": os.environ["GH_TOKEN"],
                   "COMPANION_CANDIDATES": json.dumps(request["companions"])}
    for variable, field in (("REPOSITORY", "repository"), ("SHA", "candidate_sha"),
                            ("PR", "pull_request_number"), ("BASE_SHA", "base_sha"),
                            ("BASE_REF", "base_ref"), ("HEAD_SHA", "head_sha")):
        environment["CANDIDATE_" + variable] = str(primary[field])
    subprocess.run(["bash", ROOT / "ci/integration/validate-source-set.sh"], env=environment, check=True)
    for row in [primary, *request["companions"]]:
        repository = row["repository"]
        pr = api(f"repos/{repository}/pulls/{row['pull_request_number']}")
        require(pr["head"]["repo"]["full_name"] == repository,
                "this temporary task does not admit fork source")
        require(pr["head"]["sha"] == row["head_sha"] and pr["base"]["sha"] == row["base_sha"]
                and pr["merge_commit_sha"] == row["candidate_sha"], "candidate changed during admission")
        branches = api(f"repos/{repository}/commits/{row['head_sha']}/branches-where-head")
        require(isinstance(branches, list) and branches, "primary/companion needs a branch in its own repository")


def integration_plan(record):
    resolver = module("task216_resolver", ROOT / "ci/integration/resolve-artifacts.py")
    # This remains a real, validated published baseline. All products will be
    # replaced by the task's exact package bytes through the existing source
    # overlay contract; the task version never masquerades as a release.
    baseline = resolver.baseline(record["framework_sha"], "main")
    candidates = ([record["admission"]["primary"], *record["admission"]["companions"]]
                  if record["admission"] else [])
    companions = {row["repository"] for row in candidates[1:]}
    sources = {}
    for owner in artifacts.OWNERS:
        row = record["sources"]["kuasar-sandbox" if owner == "platform" else owner]
        sources[owner] = {**row, "role": "companion" if row["repository"] in companions else "candidate"}
    cases = resolver.case_files(sources)
    products = sorted(artifacts.PRODUCTS)
    kernel_sha = sources["guest-runtime"]["sha"]
    plan = {"schema": 2, "mode": "source", "framework_sha": record["framework_sha"], "baseline": baseline,
            "candidate_records": candidates, "owners": ["platform"], "changes": {}, "sources": sources,
            "kernel_sha": kernel_sha, "test_revisions": sources, "test_overlays": sorted(artifacts.OWNERS),
            "case_files": cases, "product_sources": resolver.product_source_map(products, sources, kernel_sha),
            "embedded_sources": {"envd": {REPOSITORIES["guest-runtime"]: kernel_sha}},
            "task": {"issue": record["task"], "run_id": record["run_id"], "package_version": record["version"],
                     "admission": "main-dispatch exact main heads or existing admitted PR merge records"},
            "lanes": {arch: {"products": products, "embedded_products": ["envd"],
                             "performance": ["working-set-smoke"] if arch == "x86_64" else [],
                             "selection": artifacts.suite_selection(["platform"], arch, cases)}
                      for arch in artifacts.ARCHES}}
    artifacts.check_plan(plan)
    return plan


def freeze(args):
    require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORIES["kuasar-sandbox"]
            and os.environ.get("GITHUB_REF") == "refs/heads/main"
            and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "task entry requires main dispatch")
    check_framework(args.framework_sha)
    require(not args.output.exists(), "frozen task inputs already exist")
    request = candidate_request()
    sources = {}
    for owner, repository in REPOSITORIES.items():
        state = api(f"repos/{repository}")
        require(state.get("visibility") == "public" and state.get("private") is False,
                "non-public task source is not admitted")
        revision = sha(api(f"repos/{repository}/git/ref/heads/main")["object"]["sha"])
        sources[owner] = {"repository": repository, "sha": revision}
    require(sources["kuasar-sandbox"]["sha"] == args.framework_sha, "main moved after workflow selection; dispatch again")
    admit_candidates(request)
    if request:
        for row in [request["primary"], *request["companions"]]:
            owner = next(owner for owner, repository in REPOSITORIES.items() if repository == row["repository"])
            sources[owner]["sha"] = row["candidate_sha"]
    run_id = os.environ["GITHUB_RUN_ID"]
    require(re.fullmatch(r"[1-9][0-9]*", run_id), "invalid run identity")
    version = "v0.0.0-preview." + datetime.now(timezone.utc).strftime("%Y%m%d") + "." + run_id
    record = {"task": "kuasar-sandbox/kuasar-sandbox#216", "framework_sha": args.framework_sha,
              "run_id": run_id, "sources": sources, "test_revisions": sources,
              "admission": request, "version": version,
              "coverage": "full manifest build; six cold/warm packages; exact packaged-product E2E/performance; full source gates use the same frozen test plan"}
    args.output.mkdir(parents=True)
    plan = integration_plan(record)
    write(args.output / "integration-plan.json", plan)
    record["integration_plan_sha256"] = artifacts.digest(args.output / "integration-plan.json")
    subprocess.run([sys.executable, "-B", ROOT / "ci/hosted/workbench.py", "select",
                    "--framework-sha", args.framework_sha, "--plan", args.output / "integration-plan.json",
                    "--output", args.output / "workbench.json"], check=True)
    record["workbench_sha256"] = artifacts.digest(args.output / "workbench.json")
    write(args.output / "frozen.json", record)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            for arch, lane in plan["lanes"].items():
                stream.write(arch + "_shards=" + json.dumps(list(artifacts.shards(lane["selection"]))) + "\n")


def frozen(path):
    record = json.loads(path.read_text())
    require(record["task"] == "kuasar-sandbox/kuasar-sandbox#216", "foreign task inputs")
    check_framework(record["framework_sha"])
    require(set(record["sources"]) == set(REPOSITORIES), "incomplete six-repository source set")
    require(record["test_revisions"] == record["sources"], "full task test sources must be explicitly frozen with products")
    for owner, row in record["sources"].items():
        require(row["repository"] == REPOSITORIES[owner], "foreign source repository")
        sha(row["sha"])
    require(re.fullmatch(r"v0\.0\.0-preview\.[0-9]{8}\.[1-9][0-9]*", record["version"]), "foreign task version")
    require(artifacts.digest(path.with_name("workbench.json")) == record["workbench_sha256"], "frozen image selection changed")
    require(artifacts.digest(path.with_name("integration-plan.json")) == record["integration_plan_sha256"],
            "frozen full integration plan changed")
    artifacts.check_plan(json.loads(path.with_name("integration-plan.json").read_text()))
    return record


def fetch(args):
    record = frozen(args.frozen)
    require(not args.sources.exists(), "each job requires a fresh source directory")
    spec = importlib.util.spec_from_file_location("task216_build_inputs", ROOT / "ci/integration/build-artifacts.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    args.sources.mkdir(parents=True)
    for owner, row in record["sources"].items():
        builder.checkout(row["repository"], row["sha"], args.sources / owner)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # Freeze the shared action's host-only receipt while this directory
        # contains exactly the six clean Git checkouts. Run/version metadata
        # and the warm product transport are not compiler-cache identities.
        source_hash = hashlib.sha256(str(args.sources.resolve()).encode()).hexdigest()
        receipt = Path(os.environ["RUNNER_TEMP"]) / (
            "workbench-cache-scope-" + os.environ["GITHUB_RUN_ID"] + "-" + source_hash + ".json")
        subprocess.run([sys.executable, "-B", ROOT / "ci/hosted/workbench.py", "cache-scope",
                        "--sources", args.sources, "--receipt", receipt], check=True)
    for name in ("frozen.json", "workbench.json", "integration-plan.json"):
        shutil.copy2(args.frozen.with_name(name), args.sources / name)


def manifest(sources):
    rows = []
    for line in (sources / "kuasar-sandbox/release/bin-inputs.manifest").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        require(len(fields) == 2 and fields[0] in {*REPOSITORIES, "guest-runtime/native-deps"}
                and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", fields[1]), "invalid current product manifest")
        rows.append(tuple(fields))
    require(rows and len({name for _, name in rows}) == len(rows), "empty or duplicate product manifest")
    return rows


def products(sources, arch, rows):
    values = {}
    for owner, name in rows:
        path = sources / owner / "bin" / arch / name
        require(path.is_file() and path.resolve().is_relative_to(sources), "missing/private product: " + name)
        if name != "sandbox-runtime.bundle":
            artifacts.check_architecture(path, arch, kernel=name == "vmlinux")
        values[name] = {"path": str(path.relative_to(sources)), "sha256": artifacts.digest(path),
                        "size": path.stat().st_size, "format": "runtime bundle" if name == "sandbox-runtime.bundle"
                        else "ARM kernel Image" if name == "vmlinux" and arch == "aarch64" else "ELF64"}
    path = sources / "guest-runtime/native-deps/bin" / arch / "envd"
    artifacts.check_architecture(path, arch)
    values["embedded/envd"] = {"path": str(path.relative_to(sources)), "sha256": artifacts.digest(path),
                               "size": path.stat().st_size, "format": "ELF64"}
    return values


class Evidence:
    def __init__(self, directory, record, diagnostics=None):
        self.directory, self.record = directory, record
        self.diagnostics = diagnostics or directory
        directory.mkdir(parents=True)
        if self.diagnostics != directory:
            self.diagnostics.mkdir(parents=True)
        self.record.update(conclusion="running", stages=[])
        self.save()

    def save(self):
        write(self.diagnostics / "result.json", self.record)

    def export_success(self):
        require(self.record["conclusion"] == "success", "failed task outputs cannot be exported")
        if self.diagnostics == self.directory:
            return
        # This copy remains inside Workbench. Preserve links rather than
        # dereference them; the shared action stops the instance and checks the
        # entire declared verification tree before any host upload can run.
        try:
            shutil.copytree(self.diagnostics, self.directory, symlinks=True, dirs_exist_ok=True)
        except (OSError, shutil.Error) as error:
            self.record.update(conclusion="failure", error="evidence export: " + str(error))
            self.save()
            raise

    def run(self, label, command, *, cwd=None, env=None):
        log = self.diagnostics / "logs" / (label.replace("/", "-") + ".log")
        log.parent.mkdir(exist_ok=True)
        start = time.monotonic()
        row = {"stage": label, "argv": list(map(str, command)), "log": str(log.relative_to(self.diagnostics))}
        self.record["stages"].append(row)
        self.save()
        try:
            print("task216: " + label, flush=True)
            with log.open("w") as stream:
                with subprocess.Popen(list(map(str, command)), cwd=cwd, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, errors="replace") as process:
                    for line in process.stdout:
                        stream.write(line)
                        stream.flush()
                        print(line, end="", flush=True)
                    row["exit_code"] = process.wait()
            require(row["exit_code"] == 0, f"{label} exited {row['exit_code']}; see {log}")
        finally:
            row["wall_seconds"] = time.monotonic() - start
            self.save()


def native_cache(evidence, operation, environment):
    # Temporary caller of existing functions: no parallel cache implementation,
    # and a warm miss is an error, never a hidden build.
    if operation == "save":
        body = '''source "$1" key "$2"
descriptor=$(mktemp)
key=$(compute_key "$2" "$descriptor")
entry="$CACHE_ROOT/$CACHE_SCHEMA/$TARGET_ARCH/$2/$key"
validate_outputs "$2"
mkdir -p "$(dirname "$entry")"
[ ! -e "$entry" ] || die "cold task entry unexpectedly exists"
publish_entry "$2" "$key" "$descriptor" "$entry"
rm "$descriptor"
'''
    else:
        body = '''source "$1" key "$2"
build_component() { die "fresh-job acceptance cache miss: $1; compilation is forbidden"; }
restore_or_build "$2"
'''
    body += '''descriptor=$(mktemp)
key=$(compute_key "$2" "$descriptor")
entry="$CACHE_ROOT/$CACHE_SCHEMA/$TARGET_ARCH/$2/$key"
mkdir -p "$3"
cp "$entry/inputs.tsv" "$entry/provenance.txt" "$entry/SHA256SUMS" "$3/"
sha256sum "$entry/payload.tar" > "$3/payload.sha256"
rm "$descriptor"
'''
    for component in NATIVE:
        evidence.run("native-" + operation + "/" + component,
                     ["bash", "-euo", "pipefail", "-c", body, "task216", ROOT / "ci/native-cache/native-cache.sh", component,
                      evidence.diagnostics / "native-entries" / component],
                     env=environment)
        log = evidence.diagnostics / evidence.record["stages"][-1]["log"]
        keys = re.findall(r"^" + re.escape(component) + r"\t([0-9a-f]{64})$", log.read_text(), re.M)
        require(len(keys) == 1, "native cache did not record its exact input key")
        evidence.record.setdefault("native_cache_keys", {})[component] = keys[0]
    if operation == "restore":
        with Path(environment["KUASAR_NATIVE_CACHE_METRICS"]).open() as stream:
            hits = list(csv.DictReader(stream, delimiter="\t"))
        require(len(hits) == len(NATIVE) and {row["component"] for row in hits} == set(NATIVE)
                and all(row["status"] in ("hit", "hit-after-wait")
                        and row["input_hash"] == evidence.record["native_cache_keys"][row["component"]] for row in hits),
                "fresh-job acceptance requires all five real native cache hits")
        evidence.record["native_cache_hits"] = hits
    evidence.save()


def warm_cache_negatives(sources, arch, evidence, environment):
    """Exercise the current envd cache without compiling or changing a good entry."""
    key = evidence.record["native_cache_keys"]["envd"]
    good = Path(environment["KUASAR_NATIVE_CACHE_ROOT"]) / "v3" / arch / "envd" / key
    names = ("inputs.tsv", "provenance.txt", "SHA256SUMS", "payload.tar")
    original = {name: artifacts.digest(good / name) for name in names}
    metrics = Path(environment["KUASAR_NATIVE_CACHE_METRICS"])
    original_metrics = artifacts.digest(metrics)
    expected_products = evidence.record["products"]
    diagnostics = evidence.diagnostics / "native-negatives"
    diagnostics.mkdir()
    record = {"component": "envd", "original_key": key, "good_entry_before": original,
              "payload_size": (good / "payload.tar").stat().st_size, "cases": [], "conclusion": "running"}
    evidence.record["native_cache_negatives"] = record
    started = time.monotonic()
    private = None
    # The child retains errexit. Its expected failure is checked by this outer
    # shell, so Evidence.run still fails on any unexpected negative result.
    body = '''source "$1" help >/dev/null
marker=$2
build_component() {
    printf 'TASK216_BUILD_FORBIDDEN\\n' > "$marker"
    printf 'TASK216_BUILD_FORBIDDEN\\n' >&2
    exit 73
}
compute_key envd "$4"
set +e
(set -e; restore_or_build envd)
status=$?
set -e
printf 'negative_restore_exit=%s\\n' "$status"
[ "$status" -eq "$3" ]
'''
    try:
        with tempfile.TemporaryDirectory(prefix="task216-native-negative-", dir=environment["TMPDIR"]) as directory:
            private = Path(directory)
            isolated = private / "cache/v3" / arch / "envd" / key
            isolated.mkdir(parents=True)
            for name in names:
                shutil.copy2(good / name, isolated / name)
            require({name: artifacts.digest(isolated / name) for name in names} == original,
                    "negative fixture copy differs from restored envd entry")
            (private / "tmp").mkdir()
            negative_metrics = diagnostics / "metrics.tsv"
            base = {**environment, "KUASAR_NATIVE_CACHE_ROOT": str(private / "cache"),
                    "KUASAR_NATIVE_CACHE_METRICS": str(negative_metrics), "TMPDIR": str(private / "tmp")}
            changed = {**base, "ENVD_GOFLAGS": (environment.get("ENVD_GOFLAGS") or "-mod=mod")
                       + " -tags=kuasar_native_cache_negative216"}
            for name, selected, expected in (("changed-input-miss", changed, 73),
                                             ("matching-corrupt-refused", base, 1)):
                case = {"name": name, "expected_exit_code": expected}
                record["cases"].append(case)
                if expected == 1:
                    payload = isolated / "payload.tar"
                    payload.chmod(payload.stat().st_mode | 0o200)
                    offset = payload.stat().st_size // 2
                    with payload.open("r+b") as stream:
                        stream.seek(offset)
                        byte = stream.read(1)
                        require(len(byte) == 1, "empty envd payload cannot test corruption")
                        stream.seek(offset)
                        stream.write(bytes([byte[0] ^ 1]))
                    case.update(payload_offset=offset, corrupted_payload_sha256=artifacts.digest(payload))
                    require(case["corrupted_payload_sha256"] != original["payload.tar"], "payload was not corrupted")
                marker, descriptor, trace = (diagnostics / (name + suffix) for suffix in (".marker", ".inputs.tsv", ".execve"))
                label = "native-negative/" + name
                try:
                    evidence.run(label, ["timeout", "--kill-after=5", "60", "strace", "-f", "--seccomp-bpf", "-qq",
                                         "-s", "65535", "-e", "trace=execve", "-o", trace,
                                         "bash", "-euo", "pipefail", "-c", body, "task216", ROOT / "ci/native-cache/native-cache.sh",
                                         marker, str(expected), descriptor], env=selected)
                finally:
                    log = (evidence.diagnostics / "logs" / (label.replace("/", "-") + ".log")).read_text()
                    statuses = re.findall(r"^negative_restore_exit=([0-9]+)$", log, re.M)
                    case.update(exit_code=int(statuses[0]) if len(statuses) == 1 else None,
                                build_hook_invoked=marker.exists())
                    evidence.save()
                keys = re.findall(r"^[0-9a-f]{64}$", log, re.M)
                require(len(keys) == 1 and case["exit_code"] == expected, "negative restore lost its exact key/exit")
                case["input_hash"] = keys[0]
                if expected == 73:
                    require(keys[0] != key and b"ENVD_GOFLAGS" in descriptor.read_bytes()
                            and descriptor.read_bytes() != (good / "inputs.tsv").read_bytes(), "effective envd flags did not miss")
                    require(marker.read_text() == "TASK216_BUILD_FORBIDDEN\n" and "envd cache miss (" in log
                            and not (isolated.parent / keys[0]).exists(), "changed input did not reach the forbidden build hook")
                    case["changed_environment"] = {"ENVD_GOFLAGS": changed["ENVD_GOFLAGS"]}
                else:
                    require(keys[0] == key and descriptor.read_bytes() == (good / "inputs.tsv").read_bytes(),
                            "corruption test did not use the matching envd identity")
                    require(not marker.exists() and "cache entry checksum verification failed" in log
                            and "payload.tar: FAILED" in log and "cache miss" not in log,
                            "matching corruption did not retain the integrity failure without rebuild")
                require(not negative_metrics.exists(), "negative restore unexpectedly recorded a cache hit/build")
                audit_packaging(trace)
                case["conclusion"] = "success"
            record["good_entry_after"] = {name: artifacts.digest(good / name) for name in names}
            require(record["good_entry_after"] == original and artifacts.digest(metrics) == original_metrics,
                    "negative tests changed the good cache or its five hit records")
            require(products(sources, arch, manifest(sources)) == expected_products,
                    "negative tests changed the exact restored products")
            record.update(conclusion="success", original_cache_unchanged=True, original_hit_metrics_unchanged=True,
                          original_products_unchanged=True, product_build_executed=False)
    except BaseException as error:
        record.update(conclusion="failure", error=str(error))
        raise
    finally:
        record.update(private_copy_removed=private is not None and not private.exists(),
                      elapsed_seconds=time.monotonic() - started)
        evidence.save()


def rustc_print_query(arguments):
    """Accept Cargo's metadata probe without admitting rustc output options."""
    prints = {"file-names", "sysroot", "split-debuginfo", "crate-name", "cfg", "target-libdir"}
    remaining, queried = iter(arguments), False
    for value in remaining:
        if value in ("-", "-Wwarnings"):
            continue
        if value == "--print" or value.startswith("--print="):
            query = next(remaining, "") if value == "--print" else value.partition("=")[2]
            if query not in prints:
                return False
            queried = True
        elif value in ("--crate-name", "--crate-type", "--target"):
            parameter = next(remaining, "")
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", parameter):
                return False
            if value == "--crate-type" and parameter not in ("bin", "lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro"):
                return False
        else:
            return False
    return queried


def audit_packaging(path):
    require(path.is_file(), "missing actual package execution trace")
    forbidden = {"make", "cmake", "ninja", "compile", "asm", "cgo", "link", "collect2", "cc1", "cc1plus", "ld", "as"}
    observed = 0
    for line in path.read_text().splitlines():
        match = re.search(r'execve\(("(?:[^"\\]|\\.)*"), (\[.*\]), ', line)
        if not match:
            continue
        observed += 1
        executable, argv = ast.literal_eval(match[1]), ast.literal_eval(match[2])
        name = Path(executable).name
        require(name not in forbidden, "packaging unexpectedly invoked compiler/build tool: " + name)
        if name == "go":
            args = list(argv[1:])
            if args[:1] == ["-C"]:
                args = args[2:]
            require(args[:1] in (["env"], ["version"]) or args[:2] == ["mod", "download"],
                    "packaging unexpectedly invoked Go compilation: " + repr(argv))
        if name == "cargo":
            require(argv[1:2] == ["metadata"], "packaging unexpectedly invoked Cargo compilation")
        if name == "rustc":
            require(argv[1:] in (["--version"], ["-vV"]) or rustc_print_query(argv[1:]),
                    "packaging unexpectedly invoked native compilation: " + repr(argv))
        if re.search(r"(?:^|-)(?:gcc|g\+\+|clang|clang\+\+|cc|c\+\+)(?:-[0-9.]+)?$", name):
            require(all(value.startswith(("-print-file-name=", "-print-libgcc-file-name", "--version", "-vV"))
                        for value in argv[1:]) or argv[1:3] == ["--print", "sysroot"],
                    "packaging unexpectedly invoked native compilation: " + repr(argv))
    require(observed > 0, "package execution trace contains no readable exec records")


def package_all(sources, arch, record, evidence, environment):
    expected = evidence.record["products"]
    archives = {}
    for unit in UNITS:
        owner = "guest-runtime" if unit in ("runtime", "vmlinux") else unit
        version = (unit + "-" if unit in ("runtime", "vmlinux") else "") + record["version"]
        destination = evidence.directory / "packages" / unit
        selected = {**environment, "SOURCE_DATE_EPOCH": output(["git", "-C", sources / owner, "show", "-s", "--format=%ct", "HEAD"]),
                    "SOURCE_SHA": record["sources"][owner]["sha"]}
        for dependency in ("accelerator", "connector", "sandboxer"):
            selected["RELEASE_" + dependency.upper() + "_VERSION"] = record["version"]
            selected["RELEASE_" + dependency.upper() + "_SOURCE_SHA"] = record["sources"][dependency]["sha"]
        if owner in VALIDATORS:
            selected["RELEASE_ARCHIVE_VALIDATOR"] = str(sources / "task-tools" / owner / "release-archive-validator")
        for operation in ("package", "validate"):
            trace = evidence.diagnostics / "logs" / f"{operation}-{unit}.execve"
            command = ["bash", sources / owner / "scripts/release.sh", operation]
            if owner == "guest-runtime":
                command.append(unit)
            command.extend((version, arch, destination))
            evidence.run(operation + "/" + unit,
                         ["strace", "-f", "--seccomp-bpf", "-qq", "-s", "65535", "-e", "trace=execve", "-o", trace, *command],
                         cwd=sources / owner, env=selected)
            audit_packaging(trace)
            require(products(sources, arch, manifest(sources)) == expected, "packaging changed a selected product")
        archives[unit] = artifacts.tree_files(destination / "assets")
    evidence.record["packages"] = archives
    evidence.save()


def carry_paths(sources, arch, rows):
    return [f"{owner}/bin/{arch}/{name}" for owner, name in rows if name not in NATIVE_PRODUCTS] + [
        f"accelerator/build/{arch}/cache-ctl.map", *[f"task-tools/{owner}/release-archive-validator" for owner in VALIDATORS]]


def build_inputs(args):
    sources = args.sources.resolve()
    require(sources == Path("/src") and os.getuid() != 0, "acceptance must run as ordinary UID in Workbench /src")
    arch = args.arch
    require(platform.machine() == arch, "full verification requires the selected native runner")
    record = frozen(sources / "frozen.json")
    image = json.loads((sources / "workbench.json").read_text())["architectures"][arch]["image_id"]
    require(os.environ.get("KUASAR_WORKBENCH_IMAGE_ID") == image
            and os.environ.get("KUASAR_WORKBENCH_FRAMEWORK_SHA") == record["framework_sha"], "Workbench execution identity differs")
    for owner, row in record["sources"].items():
        require(output(["git", "-C", sources / owner, "rev-parse", "HEAD"]) == row["sha"], "source changed before build")
        subprocess.run(["git", "-C", sources / owner, "diff", "--no-ext-diff", "--exit-code", "HEAD", "--"], check=True)
        require(not output(["git", "-C", sources / owner, "ls-files", "--others", "--exclude-standard"]),
                "source contains untracked inputs before build: " + owner)
    return sources, arch, record, image


def compile_cold_products(sources, arch, evidence, environment):
    # Loading the existing cache recipe supplies the same normalized
    # KBUILD user/host/version/timestamp that its key and builds use.
    evidence.run("full-build", ["bash", "-euo", "pipefail", "-c",
                 'source "$1" help >/dev/null; make -C "$2" build', "task216",
                 ROOT / "ci/native-cache/native-cache.sh", sources / "kuasar-sandbox"], env=environment)
    evidence.run("full-manifest", ["make", "-C", sources / "kuasar-sandbox", "verify-prebuilt"], env=environment)
    for owner in VALIDATORS:
        destination = sources / "task-tools" / owner / "release-archive-validator"
        destination.parent.mkdir(parents=True)
        evidence.run("validator/" + owner,
                     ["go", "build", "-p", os.environ["KUASAR_BUILD_JOBS"], "-trimpath", "-o", destination,
                      sources / owner / "scripts/release-archive-validator.go"],
                     env={**environment, "GOWORK": "off", "GO111MODULE": "off", "CGO_ENABLED": "0"})


def legacy_inputs(args):
    """Admission for this one-time host control; never impersonates Workbench."""
    require(os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and platform.machine() == "x86_64" and os.getuid() != 0,
            "legacy control requires an ordinary UID disposable Hosted x86 runner")
    require(not any("TOKEN" in name or "SECRET" in name or name.endswith("_PASSWORD")
                    for name in os.environ), "credentials must not enter legacy candidate execution")
    require(not os.environ.get("KUASAR_WORKBENCH_IMAGE_ID")
            and not os.environ.get("KUASAR_WORKBENCH_FRAMEWORK_SHA"), "host control must not claim Workbench identity")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    require(os.getpid() == 1 and status["NoNewPrivs"].strip() == "1"
            and int(status["CapEff"], 16) == 0 and int(status["CapBnd"], 16) == 0
            and os.statvfs("/").f_flag & os.ST_RDONLY
            and not any(os.access(path, os.R_OK | os.W_OK) for path in ("/dev/kvm", "/run/docker.sock")),
            "legacy candidate lacks its private PID/capability/read-only-root boundary")
    sources = args.sources.resolve()
    task_root = sources.parent
    require(sources.name == "sources" and task_root.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve())
            and task_root != Path(os.environ["RUNNER_TEMP"]).resolve(), "legacy inputs must be task-private")
    require(output(["git", "-C", args.legacy_framework, "rev-parse", "HEAD"]) == LEGACY_FRAMEWORK,
            "legacy bootstrap checkout is not the reviewed baseline")
    bootstrap = args.legacy_framework / "ci/hosted/bootstrap.sh"
    require(bootstrap.read_bytes() == subprocess.check_output(
        ["git", "-C", args.legacy_framework, "show", LEGACY_FRAMEWORK + ":ci/hosted/bootstrap.sh"]),
        "legacy bootstrap bytes changed")
    require(artifacts.digest(bootstrap) == LEGACY_BOOTSTRAP_SHA256, "legacy bootstrap pin differs")
    require(len(os.sched_getaffinity(0)) == 2 and os.environ.get("KUASAR_BUILD_JOBS") == "2"
            and all(os.environ.get(name) == "2" for name in ("GOMAXPROCS", "CARGO_BUILD_JOBS", "CMAKE_BUILD_PARALLEL_LEVEL"))
            and os.environ.get("GOFLAGS") == "-p=2", "legacy control requires the same two-job compiler budget")
    group = next(line.removeprefix("0::") for line in Path("/proc/self/cgroup").read_text().splitlines()
                 if line.startswith("0::"))
    limits = Path("/sys/fs/cgroup") / group.lstrip("/")
    memory = (limits / "memory.max").read_text().strip()
    quota, period = (limits / "cpu.max").read_text().split()
    require(memory.isdigit() and int(memory) == 8 * 1024**3 and quota.isdigit()
            and int(quota) == 2 * int(period), "legacy control requires task-local 2 CPU / 8 GiB limits")
    private = {"HOME": task_root / "home", "TMPDIR": task_root / "tmp",
               "GOPATH": task_root / "home/go", "GOCACHE": task_root / "home/go-cache",
               "GOMODCACHE": task_root / "home/go/pkg/mod", "CARGO_HOME": task_root / "home/.cargo"}
    for name, path in private.items():
        require(os.environ.get(name) == str(path) and not path.is_symlink(), "foreign legacy writable path: " + name)
        require(not path.exists() or not any(path.iterdir()), "legacy cold control received a warm directory: " + name)
    for path in private.values():
        path.mkdir(parents=True, exist_ok=True)
    record = frozen(sources / "frozen.json")
    for owner, row in record["sources"].items():
        require(output(["git", "-C", sources / owner, "rev-parse", "HEAD"]) == row["sha"], "legacy product source changed")
        subprocess.run(["git", "-C", sources / owner, "diff", "--no-ext-diff", "--exit-code", "HEAD", "--"], check=True)
        require(not output(["git", "-C", sources / owner, "ls-files", "--others", "--exclude-standard"]),
                "legacy source has untracked inputs: " + owner)
        require(not output(["git", "-C", sources / owner, "ls-files", "--others", "--ignored", "--exclude-standard"]),
                "legacy cold source has ignored build inputs: " + owner)
    return sources, record, int(memory), bootstrap


def legacy_cold(args):
    sources, record, memory, bootstrap = legacy_inputs(args)
    arch = args.arch
    rows = manifest(sources)
    require({name for _, name in rows} == set(artifacts.PRODUCTS), "legacy manifest differs from full candidate products")
    evidence = Evidence(sources.parent / "legacy-verification", {
        "phase": "legacy-cold", "arch": arch, "host_arch": platform.machine(), "uid": os.getuid(),
        "image_id": None, "legacy_framework_sha": LEGACY_FRAMEWORK,
        "legacy_bootstrap_sha256": artifacts.digest(bootstrap), "memory_max": memory,
        "source_root": str(sources),
        "go_toolchain_mode": os.environ["GOTOOLCHAIN"],
        "isolation": "private PID namespace; read-only root; no capabilities/new privileges; no Docker socket/KVM",
        "cpus": sorted(os.sched_getaffinity(0)), "build_jobs": 2,
        "frozen_sha256": artifacts.digest(sources / "frozen.json"), "inputs": record,
        "manifest": rows, "started_ns": time.time_ns(),
        "comparison_limits": "original x86 host for both targets; ARM is cross-built; products then helpers share this job and compiler caches; the temporary Workbench treatment uses separate parallel helper jobs, unlike either normal PR path; task-private host paths differ from Workbench /src; no cache restore/save or product rebuild during packaging"})
    environment = {**os.environ, "GOWORK": str(sources / "go.work"), "TARGET_ARCH": arch,
                   "ORG": str(sources), "KUASAR_WORKSPACE_ROOT": str(sources),
                   "KUASAR_NATIVE_CACHE_ROOT": str(sources.parent / "native-cache-unused"),
                   "KUASAR_CI_TIMINGS": str(evidence.diagnostics / "build-timings.tsv"),
                   "KUASAR_REVISION_MANIFEST": str(sources / "frozen.json")}
    try:
        for tool, command in (("go", ["go", "version"]), ("cargo", ["cargo", "--version"]),
                              ("rustc", ["rustc", "-vV"]),
                              ("cc", ["aarch64-linux-gnu-gcc" if arch == "aarch64" else "gcc", "--version"])):
            evidence.run("toolchain/" + tool, command, env={**environment, "GOWORK": "off"})
        evidence.run("workspace", ["go", "work", "init", *("./" + owner for owner in REPOSITORIES if owner != "kuasar-sandbox")],
                     cwd=sources, env={**environment, "GOWORK": "off"})
        # Preserve the old environment's GOTOOLCHAIN selection. The initial
        # Hosted Go can download a newer compiler required by the current mods;
        # workspace/full-build logs and elapsed time include that cold cost.
        evidence.run("toolchain/go-effective", ["go", "env", "GOTOOLCHAIN", "GOVERSION", "GOROOT"],
                     cwd=sources, env=environment)
        compile_cold_products(sources, arch, evidence, environment)
        evidence.record["products"] = products(sources, arch, rows)
        evidence.save()
        plan = json.loads((sources / "integration-plan.json").read_text())
        helper_environment = {**environment, "RUNNER_TEMP": str(sources.parent / "tmp"),
                              "KUASAR_CI_DIR": str(evidence.diagnostics / "helper-metrics")}
        destination = evidence.directory / "integration-helpers"
        helper_payload(sources, arch, plan, destination, evidence, helper_environment,
                       {"image_id": None, "legacy_framework_sha": LEGACY_FRAMEWORK,
                        "legacy_bootstrap_sha256": LEGACY_BOOTSTRAP_SHA256,
                        "host_arch": platform.machine(), "source_root": str(sources)})
        evidence.record["helpers_sha256"] = artifacts.digest(destination / "helpers.json")
        require(products(sources, arch, rows) == evidence.record["products"], "helper compilation changed product outputs")
        package_all(sources, arch, record, evidence, environment)
        evidence.record["conclusion"] = "success"
    except BaseException as error:
        evidence.record.update(conclusion="failure", error=str(error))
        raise
    finally:
        evidence.record["elapsed_seconds"] = (time.time_ns() - evidence.record["started_ns"]) / 1e9
        evidence.save()


class LegacyInterrupted(Exception):
    def __init__(self, signum):
        self.signum = signum


def legacy_stage(evidence, label, command, *, environment=None, timeout=3600):
    """One-off host control stages; never runs arbitrary candidate commands."""
    log = evidence.directory / "logs" / (label + ".log")
    log.parent.mkdir(exist_ok=True)
    row = {"stage": label, "argv": list(map(str, command)), "log": str(log.relative_to(evidence.directory))}
    evidence.record["stages"].append(row)
    started = time.monotonic()
    print("task216 legacy control: " + label, flush=True)
    try:
        with log.open("w") as stream:
            process = subprocess.Popen(list(map(str, command)), stdout=stream, stderr=subprocess.STDOUT,
                                       env=environment, start_new_session=True)
            try:
                row["exit_code"] = process.wait(timeout=timeout)
                if row["exit_code"] < 0:
                    row["exit_code"] = 128 - row["exit_code"]
            except (subprocess.TimeoutExpired, LegacyInterrupted) as error:
                row["exit_code"] = 124 if isinstance(error, subprocess.TimeoutExpired) else 128 + error.signum
                # This process group was created above for this exact stage.
                # sudo handles only descendants of the trusted apt bootstrap;
                # a candidate service is stopped separately by its owned unit.
                subprocess.run(["sudo", "-n", "kill", "-TERM", "--", "-" + str(process.pid)], check=False)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    subprocess.run(["sudo", "-n", "kill", "-KILL", "--", "-" + str(process.pid)], check=False)
                    process.wait(timeout=10)
    finally:
        row["wall_seconds"] = time.monotonic() - started
        evidence.save()
    return row["exit_code"]


def stop_legacy_unit(record):
    unit = record["unit"]
    require(re.fullmatch(r"task216-legacy-[0-9a-f]{24}\.service", unit), "foreign legacy unit")
    inspected = subprocess.run(["systemctl", "show", unit, "--property=LoadState,Description,ActiveState,MainPID"],
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    state = dict(line.split("=", 1) for line in inspected.stdout.splitlines() if "=" in line)
    if state.get("LoadState") == "not-found":
        return 0
    require(inspected.returncode == 0, "cannot inspect owned legacy unit")
    require(state.get("Description") == record["unit_description"], "legacy unit ownership changed")
    completed = subprocess.run(["sudo", "-n", "systemctl", "stop", unit], timeout=60)
    if completed.returncode:
        return completed.returncode
    state = dict(line.split("=", 1) for line in output(
        ["systemctl", "show", unit, "--property=ActiveState,MainPID"]).splitlines())
    require(state.get("MainPID") == "0" and state.get("ActiveState") in ("inactive", "failed"),
            "legacy candidate writers have not stopped")
    return 0


def legacy_bootstrap_stage(evidence, bootstrap, profile, cpus, task_root):
    """Bound only the exact former build/reader prerequisite profiles."""
    require(profile in ("artifact-build", "artifact-cross", "artifact-arm"), "unapproved legacy bootstrap profile")
    bootstrap = bootstrap.resolve(strict=True)
    require(artifacts.digest(bootstrap) == LEGACY_BOOTSTRAP_SHA256 and len(cpus) == 2,
            "legacy bootstrap identity or CPU budget changed")
    provision = evidence.directory / "provision"
    provision.mkdir()
    for name in ("bootstrap-home", "bootstrap-tmp"):
        (task_root / name).mkdir()
    environment = {"PATH": os.environ["PATH"], "HOME": str(task_root / "bootstrap-home"),
        "TMPDIR": str(task_root / "bootstrap-tmp"), "GOWORK": "off", "RUNNER_TEMP": str(provision),
        "GITHUB_ENV": str(evidence.directory / "bootstrap.env"), "GITHUB_PATH": str(evidence.directory / "bootstrap.path"),
        "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": "Linux",
        "RUNNER_ARCH": "ARM64" if profile == "artifact-arm" else "X64",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    # Retain the provided toolchain selection while using a private HOME and
    # passing only the allowlisted environment to pinned third-party compilation.
    for key in ("GOROOT", "GOTOOLCHAIN", "RUSTUP_HOME", "RUSTUP_TOOLCHAIN"):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    if "RUSTUP_HOME" not in environment:
        rustup_home = Path(os.environ["HOME"]) / ".rustup"
        if rustup_home.is_dir():
            environment["RUSTUP_HOME"] = str(rustup_home)
    unit = "task216-legacy-" + hashlib.sha256((str(task_root) + ":bootstrap").encode()).hexdigest()[:24] + ".service"
    description = "Kuasar #216 legacy bootstrap " + str(task_root)
    evidence.record.update(bootstrap_unit=unit, bootstrap_unit_description=description,
                          bootstrap_environment=environment,
                          bootstrap_profile=profile, legacy_bootstrap_sha256=LEGACY_BOOTSTRAP_SHA256,
                          bootstrap_budget={"cpus": cpus, "memory_max": 8 * 1024**3, "build_jobs": 2})
    evidence.save()
    command = ["sudo", "-n", "systemd-run", "--wait", "--pipe", "--service-type=exec", "--unit=" + unit,
        "--description=" + description, "--property=CPUQuota=200%", "--property=MemoryMax=8G",
        "--property=RuntimeMaxSec=3600", "--property=TimeoutStopSec=20", "--property=KillMode=control-group",
        "/usr/bin/setpriv", "--reuid=" + str(os.getuid()), "--regid=" + str(os.getgid()), "--init-groups",
        "/usr/bin/taskset", "-c", ",".join(map(str, cpus)), "/usr/bin/env", "-i",
        *[key + "=" + value for key, value in environment.items()], "bash", bootstrap, "--profile", profile]
    code = 1
    try:
        code = legacy_stage(evidence, "bootstrap-readers" if profile == "artifact-arm" else "bootstrap", command, timeout=3660)
    finally:
        cleanup = 1
        try:
            cleanup = stop_legacy_unit({"unit": unit, "unit_description": description})
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
            evidence.record["bootstrap_cleanup_error"] = str(error)
        evidence.record["bootstrap_cleanup_exit_code"] = cleanup
        evidence.save()
    return code or cleanup


def retain_legacy_readers(root, destination, arch, frozen_path):
    """Retain only this trusted bootstrap's native readers, never target tools."""
    require(root.resolve() == root and not root.is_symlink(), "linked legacy reader source")
    files = {name: root / ("erofs-readers.COPYING" if name == "erofs-readers.COPYING" else "bin/" + name)
             for name in READER_FILES}
    require(all(path.resolve() == path and path.is_file() and not path.is_symlink() for path in files.values()),
            "legacy reader output is missing or linked")
    for name in READER_FILES[:3]:
        artifacts.check_architecture(files[name], arch)
    require(artifacts.digest(files["runtime-payloads.py"]) == "01d97e1cc7aed550305e13b4dfd1daa95a3740be84512d195aed46a277e661fd",
            "legacy runtime reader pin changed")
    destination.mkdir(parents=True)
    for name, path in files.items():
        shutil.copy2(path, destination / name)
    identities = {name: artifacts.digest(destination / name) for name in READER_FILES}
    (destination / "SHA256SUMS").write_text("".join(identities[name] + "  " + name + "\n" for name in READER_FILES))
    write(destination / "readers.json", {"phase": "legacy-readers", "arch": arch,
          "framework_sha": json.loads(frozen_path.read_text())["framework_sha"],
          "frozen_sha256": artifacts.digest(frozen_path), "legacy_framework_sha": LEGACY_FRAMEWORK,
          "legacy_bootstrap_sha256": LEGACY_BOOTSTRAP_SHA256, "files": identities})


def check_legacy_readers(args):
    record = frozen(args.frozen)
    root = args.readers.absolute()
    require(root.resolve() == root and not root.is_symlink(), "linked legacy reader directory")
    expected = set(READER_FILES) | {"SHA256SUMS", "readers.json"}
    require({path.name for path in root.iterdir()} == expected
            and all(path.is_file() and not path.is_symlink() for path in root.iterdir()), "unexpected legacy reader files")
    receipt = json.loads((root / "readers.json").read_text())
    identities = {name: artifacts.digest(root / name) for name in READER_FILES}
    require(receipt == {"phase": "legacy-readers", "arch": args.arch,
            "framework_sha": record["framework_sha"], "frozen_sha256": artifacts.digest(args.frozen),
            "legacy_framework_sha": LEGACY_FRAMEWORK, "legacy_bootstrap_sha256": LEGACY_BOOTSTRAP_SHA256,
            "files": identities}, "legacy readers belong to another frozen input or producer")
    for name in READER_FILES[:3]:
        artifacts.check_architecture(root / name, args.arch)
    require(identities["runtime-payloads.py"] == "01d97e1cc7aed550305e13b4dfd1daa95a3740be84512d195aed46a277e661fd",
            "legacy runtime reader pin changed")
    require((root / "SHA256SUMS").read_text() == "".join(identities[name] + "  " + name + "\n" for name in READER_FILES),
            "legacy reader checksums changed")


def legacy_finish(args):
    destination = args.output.absolute()
    receipt = destination / "host/result.json"
    supplied_root = args.task_root.absolute()
    require(supplied_root.resolve() == supplied_root
            and supplied_root.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve())
            and supplied_root.name.startswith("task216-legacy."), "foreign legacy task directory")
    if not receipt.exists():
        if supplied_root.exists():
            require(supplied_root.stat().st_uid == os.getuid(), "foreign legacy source owner")
            shutil.rmtree(supplied_root)
        return 0
    require(destination.resolve() == destination and destination.stat().st_uid == os.getuid()
            and not receipt.is_symlink(), "foreign legacy receipt")
    record = json.loads(receipt.read_text())
    require(record["owner_uid"] == os.getuid() and record["framework_sha"] == output(["git", "-C", ROOT, "rev-parse", "HEAD"]),
            "legacy receipt owner/framework differs")
    source_root = Path(record["task_root"])
    require(source_root == supplied_root, "legacy cleanup source differs from its host receipt")
    if record["conclusion"] == "running":
        record.update(conclusion="failure", finish_error="legacy controller did not record completion")
    status = 0
    try:
        if record.get("bootstrap_unit"):
            record["bootstrap_cleanup_exit_code"] = 1
            try:
                record["bootstrap_cleanup_exit_code"] = stop_legacy_unit({
                    "unit": record["bootstrap_unit"], "unit_description": record["bootstrap_unit_description"]})
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
                record["bootstrap_cleanup_error"] = str(error)
        record["cleanup_exit_code"] = stop_legacy_unit(record)
        require(record["cleanup_exit_code"] == 0 and record.get("bootstrap_cleanup_exit_code", 0) == 0,
                "legacy service cleanup failed")
        copier = module("task216_legacy_output", ROOT / "ci/hosted/workbench.py").copy_evidence
        candidate = source_root / "legacy-verification"
        if record.get("finished"):
            pass
        elif record["conclusion"] == "success":
            # Stop all writers before checking or copying. The shared copier
            # refuses all symlinks/FIFOs/devices without dereferencing them.
            copier(candidate, destination / "validated")
            readers = destination / "host/readers"
            if readers.exists():
                require(not (destination / "validated/readers").exists(), "candidate supplied trusted legacy readers")
                copier(readers, destination / "validated/readers")
        elif candidate.exists() and not candidate.is_symlink():
            diagnostics = destination / "diagnostics"
            diagnostics.mkdir(exist_ok=True)
            for name in ("result.json", "logs", "build-timings.tsv"):
                path = candidate / name
                if path.exists() or path.is_symlink():
                    copier(path, diagnostics / name)
        record["finished"] = True
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        record.update(conclusion="failure", finish_error=str(error))
        status = 1
    finally:
        try:
            if record.get("cleanup_exit_code") == 0 and record.get("bootstrap_cleanup_exit_code", 0) == 0:
                if source_root.exists():
                    # Go's downloaded modules may be read-only. Only this
                    # verified task directory is removed without following links.
                    subprocess.run(["sudo", "-n", "rm", "-rf", "--", source_root], check=True, timeout=300)
                provision = destination / "host/provision"
                if provision.exists():
                    shutil.rmtree(provision)
        except (OSError, subprocess.SubprocessError) as error:
            record.update(conclusion="failure", delete_error=str(error))
            status = 1
        record.setdefault("space_snapshots", []).append(space_snapshot("after-cleanup", {
            "task": source_root, "output": destination}, sizes=True))
        write(receipt, record)
    return status


def legacy_run(args):
    """Temporary #216 host/cross control using the exact former bootstrap."""
    sources = args.sources.resolve()
    task_root = sources.parent
    require(task_root.name.startswith("task216-legacy.") and task_root.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve())
            and not args.output.exists() and not args.output.resolve().is_relative_to(task_root), "legacy run requires private fresh paths")
    require(os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and platform.machine() == "x86_64" and os.getuid() != 0, "legacy controller requires an ordinary Hosted x86 job")
    record = frozen(sources / "frozen.json")
    require(output(["git", "-C", args.legacy_framework, "rev-parse", "HEAD"]) == LEGACY_FRAMEWORK
            and artifacts.digest(args.legacy_framework / "ci/hosted/bootstrap.sh") == LEGACY_BOOTSTRAP_SHA256,
            "legacy bootstrap is not the exact reviewed baseline")
    cpus = sorted(os.sched_getaffinity(0))[:2]
    require(len(cpus) == 2, "legacy control needs two actual CPUs")
    unit = "task216-legacy-" + hashlib.sha256(str(task_root).encode()).hexdigest()[:24] + ".service"
    description = "Kuasar #216 legacy " + str(task_root)
    evidence = Evidence(args.output / "host", {"phase": "legacy-host", "arch": args.arch,
                        "framework_sha": record["framework_sha"], "owner_uid": os.getuid(), "task_root": str(task_root),
                        "unit": unit, "unit_description": description, "frozen_sha256": artifacts.digest(sources / "frozen.json"),
                        "runner": {name: os.environ.get(name) for name in ("ImageOS", "ImageVersion", "RUNNER_ARCH")},
                        "started_ns": time.time_ns()})
    evidence.record["space_snapshots"] = [space_snapshot("before-bootstrap", {"task": task_root}, sizes=True)]
    provision = evidence.directory / "provision"
    env_file, path_file = evidence.directory / "bootstrap.env", evidence.directory / "bootstrap.path"
    profile = "artifact-cross" if args.arch == "aarch64" else "artifact-build"
    def interrupted(signum, _frame):
        raise LegacyInterrupted(signum)
    handlers = {signum: signal.signal(signum, interrupted) for signum in (signal.SIGINT, signal.SIGTERM)}
    code = 1
    try:
        mode = output(["go", "env", "GOTOOLCHAIN"], env={**os.environ, "GOWORK": "off"})
        evidence.record["go_toolchain_mode"] = mode
        code = legacy_bootstrap_stage(evidence, args.legacy_framework / "ci/hosted/bootstrap.sh", profile, cpus, task_root)
        if code:
            return code
        code = 1
        evidence.record["space_snapshots"].append(space_snapshot("after-bootstrap", {"task": task_root}, sizes=True))
        exported = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
        require(exported["KUASAR_BUILD_JOBS"] == "2", "legacy bootstrap selected a different compiler budget")
        if not shutil.which("strace"):
            code = legacy_stage(evidence, "instrumentation", ["sudo", "-n", "apt-get", "install", "-y", "--no-install-recommends", "strace"])
            if code:
                return code
            code = 1
        compiler_bins = []
        for name in ("go", "rustc", "cargo"):
            path = Path(shutil.which(name) or "missing").resolve()
            if path.name == "rustup":
                path = Path(output(["rustup", "which", name])).resolve()
            require(path.is_file(), "missing provided compiler: " + name)
            compiler_bins.append(path.parent)
        readers = Path(exported["KUASAR_HOSTED_ROOT"]).resolve()
        require(readers.is_relative_to(provision.resolve()), "bootstrap output escaped this task")
        retain_legacy_readers(readers, evidence.directory / "readers", "x86_64", sources / "frozen.json")
        candidate = {"PATH": ":".join(map(str, dict.fromkeys([*compiler_bins, readers / "bin", Path("/usr/local/bin"), Path("/usr/bin"), Path("/bin")]))),
                     "HOME": str(task_root / "home"), "TMPDIR": str(task_root / "tmp"),
                     "GOPATH": str(task_root / "home/go"), "GOCACHE": str(task_root / "home/go-cache"),
                     "GOMODCACHE": str(task_root / "home/go/pkg/mod"), "CARGO_HOME": str(task_root / "home/.cargo"),
                     "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_TEMP": os.environ["RUNNER_TEMP"],
                     "KUASAR_BUILD_JOBS": "2", "GOMAXPROCS": "2", "GOFLAGS": "-p=2", "CARGO_BUILD_JOBS": "2",
                     "CMAKE_BUILD_PARALLEL_LEVEL": "2", "GOTOOLCHAIN": mode, "GOPROXY": "https://proxy.golang.org,direct",
                     "GOSUMDB": "sum.golang.org", "CARGO_REGISTRIES_CRATES_IO_PROTOCOL": "sparse",
                     "CARGO_NET_GIT_FETCH_WITH_CLI": "true", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                     "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                     "KUASAR_RUNTIME_READER": exported["KUASAR_RUNTIME_READER"]}
        readonly = list(dict.fromkeys([ROOT, args.legacy_framework.resolve(), readers, *(path.parent for path in compiler_bins)]))
        require(not any(re.search(r"\s", str(path)) for path in [task_root, *readonly]), "legacy mount paths contain whitespace")
        command = ["sudo", "-n", "systemd-run", "--wait", "--pipe", "--service-type=exec", "--unit=" + unit,
                   "--description=" + description, "--property=CPUQuota=200%", "--property=MemoryMax=8G",
                   "--property=RuntimeMaxSec=12600", "--property=TimeoutStopSec=20", "--property=KillMode=control-group",
                   "--property=NoNewPrivileges=yes", "--property=PrivateDevices=yes", "--property=PrivateTmp=yes",
                   "--property=ProtectSystem=strict", "--property=ProtectHome=tmpfs", "--property=ProtectControlGroups=yes",
                   "--property=CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SETUID CAP_SETGID CAP_SETPCAP",
                   "--property=BindPaths=" + str(task_root), "--property=BindReadOnlyPaths=" + " ".join(map(str, readonly)),
                   "--property=InaccessiblePaths=-/run/docker.sock -/run/systemd/private -/run/dbus/system_bus_socket",
                   "--property=WorkingDirectory=" + str(sources),
                   "/usr/bin/unshare", "--mount", "--pid", "--fork", "--mount-proc", "--kill-child",
                   "/usr/bin/setpriv", "--reuid=" + str(os.getuid()), "--regid=" + str(os.getgid()), "--clear-groups",
                   "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs",
                   "/usr/bin/taskset", "-c", ",".join(map(str, cpus)), "/usr/bin/env", "-i",
                   *[name + "=" + value for name, value in candidate.items()],
                   sys.executable, "-B", ROOT / "ci/hosted/verify-workbench-216.py", "legacy-cold", "--arch", args.arch,
                   "--sources", sources, "--legacy-framework", args.legacy_framework.resolve()]
        evidence.record["candidate_environment"] = candidate
        code = legacy_stage(evidence, "candidate", command, timeout=12700)
        evidence.record["conclusion"] = "success" if code == 0 else "failure"
        return code
    except LegacyInterrupted as error:
        code = 128 + error.signum
        return code
    except BaseException as error:
        evidence.record["error"] = str(error)
        raise
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        evidence.record["space_snapshots"].append(space_snapshot("before-cleanup", {"task": task_root}, sizes=True))
        evidence.record.update(exit_code=code, elapsed_seconds=(time.time_ns() - evidence.record["started_ns"]) / 1e9)
        if evidence.record["conclusion"] == "running":
            evidence.record["conclusion"] = "failure"
        evidence.save()


def legacy_readers_run(args):
    """One native ARM reader producer; no source gate or E2E compilation."""
    task_root = args.task_root.absolute()
    require(task_root.resolve() == task_root and task_root.name.startswith("task216-legacy.")
            and task_root.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve())
            and task_root.is_dir() and not any(task_root.iterdir()) and not args.output.exists()
            and not args.output.resolve().is_relative_to(task_root), "legacy readers require fresh private state")
    require(os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and platform.machine() == "aarch64" and os.getuid() != 0, "legacy readers require ordinary Hosted ARM")
    record = frozen(args.frozen)
    bootstrap = args.legacy_framework / "ci/hosted/bootstrap.sh"
    require(output(["git", "-C", args.legacy_framework, "rev-parse", "HEAD"]) == LEGACY_FRAMEWORK
            and artifacts.digest(bootstrap) == LEGACY_BOOTSTRAP_SHA256, "legacy reader bootstrap changed")
    cpus = sorted(os.sched_getaffinity(0))[:2]
    require(len(cpus) == 2, "legacy readers require two CPUs")
    unit = "task216-legacy-" + hashlib.sha256(str(task_root).encode()).hexdigest()[:24] + ".service"
    evidence = Evidence(args.output / "host", {"phase": "legacy-reader-host", "arch": "aarch64",
        "framework_sha": record["framework_sha"], "owner_uid": os.getuid(), "task_root": str(task_root),
        "unit": unit, "unit_description": "Kuasar #216 legacy readers " + str(task_root),
        "frozen_sha256": artifacts.digest(args.frozen), "started_ns": time.time_ns(),
        "runner": {name: os.environ.get(name) for name in ("ImageOS", "ImageVersion", "RUNNER_ARCH")},
        "comparison_limits": "one native ARM reader producer, reused by compiler-free E2E; not the former per-job repeated bootstrap cost"})
    evidence.record["space_snapshots"] = [space_snapshot("before-bootstrap", {"task": task_root}, sizes=True)]
    provision = evidence.directory / "provision"
    env_file = evidence.directory / "bootstrap.env"
    def interrupted(signum, _frame):
        raise LegacyInterrupted(signum)
    handlers = {signum: signal.signal(signum, interrupted) for signum in (signal.SIGINT, signal.SIGTERM)}
    code = 1
    try:
        # Only this pinned trusted profile may install host prerequisites. It
        # has with_vm=false; no source profile, KVM/sysctl/udev setup is invoked.
        code = legacy_bootstrap_stage(evidence, bootstrap, "artifact-arm", cpus, task_root)
        if code:
            return code
        code = 1
        require(stop_legacy_unit(evidence.record) == 0, "legacy reader writers have not stopped")
        exported = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
        readers = Path(exported["KUASAR_HOSTED_ROOT"])
        require(readers.resolve() == readers and readers.is_relative_to(provision.resolve())
                and exported["KUASAR_BUILD_JOBS"] == "2", "legacy reader output or budget differs")
        payload = task_root / "legacy-verification"
        retain_legacy_readers(readers, payload / "readers", "aarch64", args.frozen)
        write(payload / "result.json", {"conclusion": "success", "phase": "legacy-readers", "arch": "aarch64",
              "frozen_sha256": evidence.record["frozen_sha256"], "legacy_framework_sha": LEGACY_FRAMEWORK,
              "legacy_bootstrap_sha256": LEGACY_BOOTSTRAP_SHA256,
              "readers_sha256": artifacts.digest(payload / "readers/readers.json")})
        evidence.record["conclusion"] = "success"
        code = 0
        return code
    except LegacyInterrupted as error:
        code = 128 + error.signum
        return code
    except BaseException as error:
        evidence.record["error"] = str(error)
        raise
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        evidence.record["space_snapshots"].append(space_snapshot("before-cleanup", {"task": task_root}, sizes=True))
        evidence.record.update(exit_code=code, elapsed_seconds=(time.time_ns() - evidence.record["started_ns"]) / 1e9)
        if evidence.record["conclusion"] == "running":
            evidence.record["conclusion"] = "failure"
        evidence.save()


def build(args):
    sources, arch, record, image = build_inputs(args)
    rows = manifest(sources)
    require({name for _, name in rows} == set(artifacts.PRODUCTS), "current product manifest differs from the existing integration contract")
    evidence = Evidence(sources / "verification", {"phase": args.phase, "arch": arch, "uid": os.getuid(),
                        "frozen_sha256": artifacts.digest(sources / "frozen.json"), "inputs": record,
                        "image_id": image, "manifest": rows, "started_ns": time.time_ns()},
                        diagnostics=Path("/output/verification"))
    space_paths = {"sources": sources, "build": Path("/build"), "home": Path("/work/home"), "output": Path("/output")}
    evidence.record["space_snapshots"] = [space_snapshot("before-build", space_paths)]
    environment = {**os.environ, "GOWORK": str(sources / "go.work"),
                   "KUASAR_CI_TIMINGS": str(evidence.diagnostics / "build-timings.tsv"),
                   "KUASAR_NATIVE_CACHE_METRICS": str(evidence.diagnostics / "native-cache.tsv"),
                   "KUASAR_REVISION_MANIFEST": str(sources / "frozen.json")}
    try:
        evidence.run("workspace", ["go", "work", "init", *("./" + owner for owner in REPOSITORIES if owner != "kuasar-sandbox")],
                     cwd=sources, env={**environment, "GOWORK": "off"})
        if args.phase == "cold":
            # These directories are solely this action's writable state. Image
            # layers and host/global caches are never removed.
            cleared = []
            for path in map(Path, ("/build/native-cache", "/work/home/go/pkg/mod", "/work/home/go-cache",
                                   "/work/home/.cargo/registry", "/work/home/.cargo/git", "/work/home/.cargo/release-materials")):
                if path.exists():
                    require(not path.is_symlink() and path.resolve() == path, "linked task cache cannot be cleared")
                    for entry in path.rglob("*"):
                        if not entry.is_symlink():
                            entry.chmod(entry.stat().st_mode | 0o200)
                    shutil.rmtree(path)
                path.mkdir(parents=True)
                cleared.append(str(path))
            evidence.record["cleared_task_cache_paths"] = cleared
            compile_cold_products(sources, arch, evidence, environment)
            native_cache(evidence, "save", environment)
        else:
            previous = json.loads((sources / "cold-result.json").read_text())
            require(previous["conclusion"] == "success" and previous["arch"] == arch
                    and previous["frozen_sha256"] == evidence.record["frozen_sha256"]
                    and previous["image_id"] == image, "cold artifact belongs to another input set")
            require(artifacts.digest(sources / "carried-products.tar") == previous["carried_products_sha256"], "cold products transport changed")
            native_cache(evidence, "restore", environment)
            transport.extract(sources / "carried-products.tar", sources / "restored-products")
            restored = sources / "restored-products"
            expected = set(carry_paths(sources, arch, rows))
            require(set(artifacts.tree_files(restored)) == expected, "transport includes missing/extra or native build materials")
            for relative in sorted(expected):
                destination = sources / relative
                require(not destination.exists(), "fresh job unexpectedly contains product output")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(restored / relative, destination)
            ch = sources / "sandboxer/bin" / arch / "cloud-hypervisor"
            ch.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sources / "sandboxer/native-deps/bin" / arch / "cloud-hypervisor", ch)
            evidence.record["restore_boundary"] = "five native caches plus exact cold non-native products and validators; no product recompilation"
        evidence.record["products"] = products(sources, arch, rows)
        evidence.save()
        if args.phase == "warm":
            require(evidence.record["products"] == previous["products"], "restored native/non-native product bytes differ from cold inputs")
            warm_cache_negatives(sources, arch, evidence, environment)
        package_all(sources, arch, record, evidence, environment)
        if args.phase == "warm":
            evidence.record["package_bytes_equal_to_cold"] = evidence.record["packages"] == previous["packages"]
        if args.phase == "cold":
            archive = evidence.directory / "carried-products.tar"
            with tarfile.open(archive, "w") as stream:
                for relative in carry_paths(sources, arch, rows):
                    path = sources / relative
                    require(path.is_file() and not path.is_symlink(), "only exact regular non-native products may travel")
                    stream.add(path, arcname=relative, recursive=False)
            evidence.record["carried_products_sha256"] = artifacts.digest(archive)
        evidence.record["conclusion"] = "success"
    except BaseException as error:
        evidence.record.update(conclusion="failure", error=str(error))
        raise
    finally:
        evidence.record["space_snapshots"].append(space_snapshot("before-export-and-cleanup", space_paths))
        evidence.record["elapsed_seconds"] = (time.time_ns() - evidence.record["started_ns"]) / 1e9
        evidence.save()
        if evidence.record["conclusion"] == "success":
            evidence.export_success()


def helper_payload(sources, arch, plan, destination, evidence, environment, producer):
    """The same case-selected owner helpers, without another product build."""
    artifacts.check_plan(plan)
    for owner, row in plan["test_revisions"].items():
        pinned = sources / ("kuasar-sandbox" if owner == "platform" else owner)
        require(output(["git", "-C", pinned, "rev-parse", "HEAD"]) == row["sha"], "helper source revision changed")
        subprocess.run(["git", "-C", pinned, "diff", "--no-ext-diff", "--no-textconv", "--exit-code", "HEAD", "--"], check=True)
    destination.mkdir()
    for owner in plan["test_overlays"]:
        pinned = sources / ("kuasar-sandbox" if owner == "platform" else owner)
        target = destination / artifacts.test_overlay_root(owner)
        if owner == "platform":
            for name in artifacts.tree_files(pinned / "test"):
                if artifacts.platform_test_path(name):
                    path = target / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(pinned / "test" / name, path)
        else:
            shutil.copytree(pinned / "test/e2e", target)
    selected = artifacts.planned_helpers(plan["lanes"][arch]["selection"])
    # Fixed module calls keep separate command/exit/log evidence while reusing
    # exactly the existing owner recipes. No new runner or compiler fallback.
    environment = {**environment, "PYTHONPATH": str(ROOT / "ci/integration")}
    if "basic.demo.sh" in plan["lanes"][arch]["selection"]["cases"]:
        demo = destination / artifacts.test_overlay_root("platform") / "demo"
        evidence.run("helper-wheels", [sys.executable, "-B", "-c",
                     "import sys; from pathlib import Path; import build_demo_wheels; "
                     "build_demo_wheels.build(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))",
                     demo, arch, demo / "wheels" / arch], cwd=ROOT, env=environment)
    evidence.run("helper-build", [sys.executable, "-B", "-c",
                 "import json, os, sys; from pathlib import Path; import build_helpers; "
                 "build_helpers.build(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), "
                 "json.loads(sys.argv[4]), dict(os.environ))",
                 sources, arch, destination / "helpers", json.dumps(selected, sort_keys=True)], cwd=ROOT, env=environment)
    for relative in artifacts.tree_files(destination):
        path = destination / relative
        path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
    metadata = {"plan_id": artifacts.identity(plan), "arch": arch, "test_revisions": plan["test_revisions"],
                "frozen_sha256": evidence.record["frozen_sha256"], **producer,
                "tests": {owner: artifacts.tree_files(destination / artifacts.test_overlay_root(owner))
                          for owner in plan["test_overlays"]},
                "helpers": {name: {"sha256": artifacts.digest(destination / "helpers" / name),
                                   "source_sha": plan["framework_sha"] if owner == "framework"
                                   else plan["test_revisions"][owner]["sha"]} for name, owner in selected.items()}}
    write(destination / "helpers.json", metadata)


def helpers(args):
    sources, arch, record, image = build_inputs(args)
    plan = json.loads((sources / "integration-plan.json").read_text())
    evidence = Evidence(sources / "verification", {"phase": "helpers", "arch": arch, "uid": os.getuid(),
                        "frozen_sha256": artifacts.digest(sources / "frozen.json"), "image_id": image,
                        "started_ns": time.time_ns()}, diagnostics=Path("/output/verification"))
    try:
        environment = {**os.environ, "GOWORK": str(sources / "go.work")}
        evidence.run("helper-workspace", ["go", "work", "init", *("./" + owner for owner in REPOSITORIES
                     if owner != "kuasar-sandbox")], cwd=sources, env={**environment, "GOWORK": "off"})
        started = time.monotonic()
        helper_payload(sources, arch, plan, evidence.directory / "integration-helpers", evidence, environment,
                       {"image_id": image})
        evidence.record["helper_build_seconds"] = time.monotonic() - started
        evidence.record["conclusion"] = "success"
    except BaseException as error:
        evidence.record.update(conclusion="failure", error=str(error))
        raise
    finally:
        evidence.record["elapsed_seconds"] = (time.time_ns() - evidence.record["started_ns"]) / 1e9
        evidence.save()
        if evidence.record["conclusion"] == "success":
            evidence.export_success()


def packaged_delta(args):
    """Transport validated package bytes to the existing source-mode overlay."""
    record = frozen(args.frozen)
    plan = json.loads(args.frozen.with_name("integration-plan.json").read_text())
    arch = args.arch
    require(not args.output.exists(), "packaged delta requires a fresh directory")
    cold = json.loads((args.packages / "result.json").read_text())
    selection = json.loads(args.frozen.with_name("workbench.json").read_text())
    image = selection["architectures"][arch]["image_id"]
    input_digest = artifacts.digest(args.frozen)
    legacy = getattr(args, "command", "packaged-delta") == "legacy-packaged-delta"
    require(cold["conclusion"] == "success" and cold["arch"] == arch and cold["frozen_sha256"] == input_digest,
            "packages belong to another frozen input set or did not pass")
    if legacy:
        require(cold["phase"] == "legacy-cold" and cold["image_id"] is None
                and cold["legacy_framework_sha"] == LEGACY_FRAMEWORK
                and cold["legacy_bootstrap_sha256"] == LEGACY_BOOTSTRAP_SHA256
                and cold["host_arch"] == "x86_64" and type(cold["uid"]) is int and cold["uid"] > 0
                and cold["inputs"] == record and cold["memory_max"] == 8 * 1024**3
                and cold["build_jobs"] == 2 and len(set(cold["cpus"])) == 2
                and cold["go_toolchain_mode"], "legacy packages lack their exact host/bootstrap/budget identity")
        stages = {row["stage"]: row["exit_code"] for row in cold["stages"]}
        expected = {"workspace", "full-build", "full-manifest", "toolchain/go", "toolchain/go-effective", "helper-build"}
        if "basic.demo.sh" in plan["lanes"][arch]["selection"]["cases"]:
            expected.add("helper-wheels")
        expected.update("validator/" + owner for owner in VALIDATORS)
        expected.update(operation + "/" + unit for unit in UNITS for operation in ("package", "validate"))
        require(len(stages) == len(cold["stages"]) and expected <= stages.keys()
                and all(value == 0 for value in stages.values()), "legacy package stages did not all pass")
        build_context = {"legacy_control": {key: cold[key] for key in (
            "legacy_framework_sha", "legacy_bootstrap_sha256", "host_arch", "arch", "uid", "cpus",
            "build_jobs", "memory_max", "go_toolchain_mode", "source_root")}}
        build_context["test_helpers"] = {"legacy_control": build_context["legacy_control"]}
    else:
        require(cold["phase"] == "cold" and cold["image_id"] == image,
                "packages belong to another frozen input set or did not pass")
        build_context = {"workbench": {"image_id": image, "framework_sha": plan["framework_sha"]}}
    require(set(cold["products"]) == set(artifacts.PRODUCTS) | {"embedded/envd"}
            and set(cold["packages"]) == set(UNITS), "incomplete cold products or release units")
    helper_root = args.helpers / "integration-helpers"
    helper = json.loads((helper_root / "helpers.json").read_text())
    require(helper["plan_id"] == artifacts.identity(plan) and helper["arch"] == arch
            and helper["test_revisions"] == plan["test_revisions"] and helper["frozen_sha256"] == input_digest,
            "helpers belong to another exact test/image set")
    if legacy:
        require(helper.get("image_id") is None
                and all(helper.get(key) == cold[key] for key in (
                    "legacy_framework_sha", "legacy_bootstrap_sha256", "host_arch", "source_root"))
                and artifacts.digest(helper_root / "helpers.json") == cold["helpers_sha256"],
                "legacy helpers lack their same-job producer identity")
    else:
        require(helper.get("image_id") == image and not helper.get("legacy_framework_sha"),
                "helpers belong to another exact test/image set")
    expected_files = {"helpers.json"}
    require(set(helper["tests"]) == set(plan["test_overlays"])
            and set(helper["helpers"]) == set(artifacts.planned_helpers(plan["lanes"][arch]["selection"])),
            "helper/test ownership differs from the complete plan")
    for owner, values in helper["tests"].items():
        directory = artifacts.test_overlay_root(owner)
        require(artifacts.tree_files(helper_root / directory) == values, "helper test overlay changed")
        expected_files.update(directory + "/" + name for name in values)
    for name, owner in artifacts.planned_helpers(plan["lanes"][arch]["selection"]).items():
        revision = plan["framework_sha"] if owner == "framework" else plan["test_revisions"][owner]["sha"]
        require(helper["helpers"][name] == {"sha256": artifacts.digest(helper_root / "helpers" / name),
                                           "source_sha": revision}, "compiled helper identity changed")
        artifacts.check_architecture(helper_root / "helpers" / name, arch)
        expected_files.add("helpers/" + name)
    require(set(artifacts.tree_files(helper_root)) == expected_files, "undeclared helper output")
    archive_records = {}
    with tempfile.TemporaryDirectory(prefix="task216-packages-") as temporary:
        unpacked = Path(temporary) / "unpacked"
        unpacked.mkdir()
        ownership = {}
        for unit in UNITS:
            assets = args.packages / "packages" / unit / "assets"
            require(artifacts.tree_files(assets) == cold["packages"][unit], "validated package bytes changed: " + unit)
            version = (unit + "-" if unit in ("runtime", "vmlinux") else "") + record["version"]
            name = artifacts.archive_name(unit, version, arch)
            archive = assets / name
            archive_records[unit] = {"archive": name, "sha256": artifacts.digest(archive),
                                     "size": archive.stat().st_size}
            artifacts.unpack(archive, unpacked, unit, ownership)
        for name in artifacts.PRODUCTS:
            path = unpacked / "bin" / name
            require(artifacts.digest(path) == cold["products"][name]["sha256"], "packaged product differs from cold build: " + name)
            if name != "sandbox-runtime.bundle":
                artifacts.check_architecture(path, arch, kernel=name == "vmlinux")
        payloads = artifacts.runtime_payloads(unpacked, arch, cold["products"]["sandbox-init"]["sha256"],
                                             cold["products"]["embedded/envd"]["sha256"])
        embedded = Path(temporary) / "embedded"
        subprocess.run([sys.executable, os.environ["KUASAR_RUNTIME_READER"], unpacked / "bin/sandbox-runtime.bundle",
                        shutil.which("fsck.erofs"), shutil.which("dump.erofs"), embedded], check=True)
        require(artifacts.digest(embedded / "envd") == payloads["envd"], "extracted packaged envd changed")
        args.output.mkdir(parents=True)
        for directory in ("test", "helpers"):
            shutil.copytree(helper_root / directory, args.output / directory)
        (args.output / "bin").mkdir()
        for name in artifacts.PRODUCTS:
            shutil.copy2(unpacked / "bin" / name, args.output / "bin" / name)
        (args.output / "embedded").mkdir()
        shutil.copy2(embedded / "envd", args.output / "embedded/envd")
        (args.output / "embedded/envd").chmod(0o755)
    metadata = {"plan_id": artifacts.identity(plan), "arch": arch, "test_revisions": plan["test_revisions"],
                "products": {name: {"sha256": cold["products"][name]["sha256"], "sources": plan["product_sources"][name]}
                             for name in artifacts.PRODUCTS},
                "embedded": {"envd": {"sha256": payloads["envd"], "sources": plan["embedded_sources"]["envd"]}},
                "tests": helper["tests"], "helpers": helper["helpers"],
                "build_context": {**build_context,
                                  "task": {"cold_result_sha256": artifacts.digest(args.packages / "result.json"),
                                           "frozen_sha256": input_digest, "package_origins": archive_records,
                                           "materials": "task package archives retain their original source/license materials; composition baseline materials identify only the published baseline"}}}
    write(args.output / "outputs.json", metadata)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("freeze")
    prepare.add_argument("--framework-sha", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    materialize = commands.add_parser("fetch")
    materialize.add_argument("--frozen", type=Path, required=True)
    materialize.add_argument("--sources", type=Path, required=True)
    disk = commands.add_parser("disk-snapshot")
    disk.add_argument("--sources", type=Path, required=True)
    disk.add_argument("--phase", required=True)
    disk.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("build")
    verify.add_argument("--phase", choices=("cold", "warm"), required=True)
    verify.add_argument("--arch", choices=artifacts.ARCHES, required=True)
    verify.add_argument("--sources", type=Path, default=Path("/src"))
    legacy = commands.add_parser("legacy-cold")
    legacy.add_argument("--arch", choices=artifacts.ARCHES, required=True)
    legacy.add_argument("--sources", type=Path, required=True)
    legacy.add_argument("--legacy-framework", type=Path, required=True)
    host = commands.add_parser("legacy-run")
    host.add_argument("--arch", choices=artifacts.ARCHES, required=True)
    host.add_argument("--sources", type=Path, required=True)
    host.add_argument("--legacy-framework", type=Path, required=True)
    host.add_argument("--output", type=Path, required=True)
    readers = commands.add_parser("legacy-readers-run")
    readers.add_argument("--frozen", type=Path, required=True)
    readers.add_argument("--task-root", type=Path, required=True)
    readers.add_argument("--legacy-framework", type=Path, required=True)
    readers.add_argument("--output", type=Path, required=True)
    reader_check = commands.add_parser("check-legacy-readers")
    reader_check.add_argument("--frozen", type=Path, required=True)
    reader_check.add_argument("--readers", type=Path, required=True)
    reader_check.add_argument("--arch", choices=artifacts.ARCHES, required=True)
    finish = commands.add_parser("legacy-finish")
    finish.add_argument("--output", type=Path, required=True)
    finish.add_argument("--task-root", type=Path, required=True)
    helper = commands.add_parser("helpers")
    helper.add_argument("--arch", choices=artifacts.ARCHES, required=True)
    helper.add_argument("--sources", type=Path, default=Path("/src"))
    for name in ("packaged-delta", "legacy-packaged-delta"):
        delta = commands.add_parser(name)
        delta.add_argument("--frozen", type=Path, required=True)
        delta.add_argument("--arch", choices=artifacts.ARCHES, required=True)
        delta.add_argument("--packages", type=Path, required=True)
        delta.add_argument("--helpers", type=Path, required=True)
        delta.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return {"freeze": freeze, "fetch": fetch, "build": build, "helpers": helpers, "legacy-cold": legacy_cold,
            "disk-snapshot": disk_snapshot,
            "legacy-run": legacy_run, "legacy-finish": legacy_finish,
            "legacy-readers-run": legacy_readers_run, "check-legacy-readers": check_legacy_readers,
            "packaged-delta": packaged_delta, "legacy-packaged-delta": packaged_delta}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main() or 0)
