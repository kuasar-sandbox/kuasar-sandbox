#!/usr/bin/env python3
"""Temporary #216 acceptance: frozen full builds and fresh-job native-cache packaging."""
from __future__ import annotations

import argparse
import ast
import csv
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ci/integration"))
import artifacts
import transport

REPOSITORIES = {owner: "kuasar-sandbox/" + owner for owner in (
    "kuasar-sandbox", "accelerator", "connector", "guest-runtime", "sandboxer", "orchestrator")}
NATIVE = ("vmlinux", "erofs", "envd", "rocksdb", "cloud-hypervisor")
UNITS = ("accelerator", "connector", "sandboxer", "orchestrator", "runtime", "vmlinux")
VALIDATORS = ("accelerator", "sandboxer", "orchestrator")
NATIVE_PRODUCTS = {"vmlinux", "mkfs.erofs", "cloud-hypervisor"}
require = artifacts.require


def output(command, **kwargs):
    return subprocess.check_output(list(map(str, command)), text=True, **kwargs).strip()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(artifacts.canonical(value) + b"\n")


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
              "coverage": "full manifest build; six cold/warm packages; source/E2E gates recorded separately"}
    args.output.mkdir(parents=True)
    subprocess.run([sys.executable, "-B", ROOT / "ci/hosted/workbench.py", "select",
                    "--framework-sha", args.framework_sha, "--output", args.output / "workbench.json"], check=True)
    record["workbench_sha256"] = artifacts.digest(args.output / "workbench.json")
    write(args.output / "frozen.json", record)


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
    for name in ("frozen.json", "workbench.json"):
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
    def __init__(self, directory, record):
        self.directory, self.record = directory, record
        directory.mkdir(parents=True)
        self.record.update(conclusion="running", stages=[])
        self.save()

    def save(self):
        write(self.directory / "result.json", self.record)

    def run(self, label, command, *, cwd=None, env=None):
        log = self.directory / "logs" / (label.replace("/", "-") + ".log")
        log.parent.mkdir(exist_ok=True)
        start = time.monotonic()
        row = {"stage": label, "argv": list(map(str, command)), "log": str(log.relative_to(self.directory))}
        self.record["stages"].append(row)
        self.save()
        try:
            print("task216: " + label, flush=True)
            with log.open("w") as stream:
                with subprocess.Popen(list(map(str, command)), cwd=cwd, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, errors="replace") as process:
                    for line in process.stdout:
                        stream.write(line)
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
                      evidence.directory / "native-entries" / component],
                     env=environment)
        log = evidence.directory / evidence.record["stages"][-1]["log"]
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
        if re.search(r"(?:^|-)(?:gcc|g\+\+|clang|clang\+\+|cc|c\+\+)(?:-[0-9.]+)?$", name) or name == "rustc":
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
            trace = evidence.directory / "logs" / f"{operation}-{unit}.execve"
            command = ["bash", sources / owner / "scripts/release.sh", operation]
            if owner == "guest-runtime":
                command.append(unit)
            command.extend((version, arch, destination))
            evidence.run(operation + "/" + unit,
                         ["strace", "-f", "-qq", "-s", "65535", "-e", "trace=execve", "-o", trace, *command],
                         cwd=sources / owner, env=selected)
            audit_packaging(trace)
            require(products(sources, arch, manifest(sources)) == expected, "packaging changed a selected product")
        archives[unit] = artifacts.tree_files(destination / "assets")
    evidence.record["packages"] = archives
    evidence.save()


def carry_paths(sources, arch, rows):
    return [f"{owner}/bin/{arch}/{name}" for owner, name in rows if name not in NATIVE_PRODUCTS] + [
        f"accelerator/build/{arch}/cache-ctl.map", *[f"task-tools/{owner}/release-archive-validator" for owner in VALIDATORS]]


def build(args):
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
    rows = manifest(sources)
    evidence = Evidence(sources / "verification", {"phase": args.phase, "arch": arch, "uid": os.getuid(),
                        "frozen_sha256": artifacts.digest(sources / "frozen.json"), "inputs": record,
                        "image_id": image, "manifest": rows, "started_ns": time.time_ns()})
    environment = {**os.environ, "GOWORK": str(sources / "go.work"),
                   "KUASAR_CI_TIMINGS": str(evidence.directory / "build-timings.tsv"),
                   "KUASAR_NATIVE_CACHE_METRICS": str(evidence.directory / "native-cache.tsv"),
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
        evidence.record["elapsed_seconds"] = (time.time_ns() - evidence.record["started_ns"]) / 1e9
        evidence.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("freeze")
    prepare.add_argument("--framework-sha", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    materialize = commands.add_parser("fetch")
    materialize.add_argument("--frozen", type=Path, required=True)
    materialize.add_argument("--sources", type=Path, required=True)
    verify = commands.add_parser("build")
    verify.add_argument("--phase", choices=("cold", "warm"), required=True)
    verify.add_argument("--arch", choices=artifacts.ARCHES, required=True)
    verify.add_argument("--sources", type=Path, default=Path("/src"))
    args = parser.parse_args()
    {"freeze": freeze, "fetch": fetch, "build": build}[args.command](args)


if __name__ == "__main__":
    main()
