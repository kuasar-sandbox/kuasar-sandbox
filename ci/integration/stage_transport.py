#!/usr/bin/env python3
"""Partition already validated aggregate bytes; reconstruct the existing stage layout.

This module never retrieves credentials, downloads an artifact, opens an archive
or executes a payload. The existing bounded download action authenticates each
selected artifact and its ZIP digest. This layer binds parts to one stage and
checks the exact regular-file layout before exposing it to existing consumers.
"""
import argparse
import csv
import errno
import json
import os
from pathlib import Path
import re
import shutil
import stat

import artifacts
import download_artifact

selection = artifacts.delivery_module()
require = artifacts.require
ARCHES = artifacts.ARCHES
FORMAT = "partitioned-v1"
ROLES = ("common", "core-x86_64", "core-aarch64", "workbench-x86_64", "workbench-aarch64")
INDEX = "stage-index.json"


def json_value(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "duplicate stage JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= 1024**2, "stage JSON is not regular or bounded")
        data = stream.read(1024**2 + 1)
    require(len(data) <= 1024**2, "oversized stage JSON")
    return json_value(data)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2) + "\n")
    path.chmod(0o644)


def context():
    repository, run_id = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            and re.fullmatch(r"[1-9][0-9]*", run_id)
            and re.fullmatch(r"[1-9][0-9]*", attempt), "missing stage run context")
    return repository, run_id, int(attempt)


def part_name(role, version, run_id):
    if role.startswith("workbench-"):
        return "workbench-image-" + role.removeprefix("workbench-") + "-" + run_id
    require(role in ROLES, "unknown stage part")
    return f"aggregate-stage-{role}-{version}-{run_id}"


def metadata_name(version, run_id):
    return f"aggregate-stage-{version}-{run_id}"


def identity(name, database_id, digest):
    if isinstance(digest, str):
        digest = digest.removeprefix("sha256:")
    repository, run_id, _ = context()
    value = {"schema": 1, "repository": repository, "run_id": run_id,
             "artifacts": [{"name": name, "id": str(database_id), "digest": digest}]}
    return download_artifact.expected_identity(value)


def layout(version, units, delivery):
    require(isinstance(units, dict) and set(units) == set(artifacts.UNITS), "stage needs all six units")
    require(delivery in selection.MODERN_DELIVERIES, "unsupported partitioned delivery")
    # These existing name functions also validate the aggregate version/arch.
    selection.workbench_archive(version, "x86_64")
    result = {name: ("metadata", name) for name in
              ("selection.tsv", "source-records.json", "release-notes.md", "source-inputs.tar")}
    result["assets/platform-" + version + ".tar.gz"] = ("common", "assets/platform-" + version + ".tar.gz")
    for arch in ARCHES:
        for unit, tag in units.items():
            require(isinstance(tag, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", tag), "unsafe component version")
            path = "assets/" + artifacts.archive_name(unit, tag, arch)
            require(path not in result, "duplicate component archive")
            result[path] = ("core-" + arch, path)
        name = selection.workbench_archive(version, arch)
        result["assets/" + name] = ("workbench-" + arch, name)
        result["workbench/workbench-" + arch + ".json"] = ("workbench-" + arch, "workbench-" + arch + ".json")
        if delivery in selection.NODE_INFRA_DELIVERIES:
            name = selection.node_infra_archive(version, arch)
            result["assets/" + name] = ("core-" + arch, "assets/" + name)
            if delivery == selection.NODE_INFRA_DELIVERY:
                name = selection.node_infra_docker_archive(version, arch)
                result["assets/" + name] = ("core-" + arch, "assets/" + name)
            path = "node-infra/node-infra-" + arch + ".json"
            result[path] = ("metadata", path)
    if delivery in selection.NODE_INFRA_DELIVERIES:
        name = selection.node_infra_deploy_archive(version)
        result["assets/" + name] = ("common", "assets/" + name)
        result["node-infra/node-infra-index.json"] = ("metadata", "node-infra/node-infra-index.json")
    return result


def regular_files(root):
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), "missing regular stage directory")
    files = {}
    for directory, subdirs, names in os.walk(root, followlinks=False):
        for name in subdirs:
            require(not (Path(directory) / name).is_symlink(), "linked stage directory")
        for name in names:
            path = Path(directory) / name
            require(stat.S_ISREG(path.lstat().st_mode), "non-regular stage file")
            relative = path.relative_to(root).as_posix()
            artifacts.relative(relative)
            files[relative] = path
            require(len(files) <= 64, "too many stage files")
    return files


def file_record(path):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and stat.S_IMODE(before.st_mode) == 0o644
                and 0 <= before.st_size <= 20 * 1024**3, "invalid stage file mode or size")
        import hashlib
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024**2), b""):
            digest.update(block)
        after = os.fstat(stream.fileno())
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), "stage file changed")
    return {"size": before.st_size, "sha256": digest.hexdigest(), "mode": 0o644}


def link_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    require(not os.path.lexists(target), "stage destination collision")
    try:
        os.link(source, target, follow_symlinks=False)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        with Path(source).open("rb") as src, target.open("xb") as dst:
            shutil.copyfileobj(src, dst, 1024**2)
        target.chmod(0o644)


def prepare(stage, output, version, source_sha, framework_sha, delivery, workbench_rows):
    stage, output = Path(stage), Path(output)
    repository, run_id, attempt = context()
    require(re.fullmatch(r"[0-9a-f]{40}", source_sha) and re.fullmatch(r"[0-9a-f]{40}", framework_sha),
            "stage source/framework must be exact")
    rows = list(csv.reader((stage / "selection.tsv").read_text().splitlines(), delimiter="\t"))
    require(len(rows) == len(artifacts.UNITS) and all(len(row) == 2 for row in rows)
            and [row[0] for row in rows] == list(selection.UNITS), "invalid complete stage selection")
    units = dict(rows)
    expected = layout(version, units, delivery)
    files = regular_files(stage)
    require(set(files) == set(expected), "complete stage layout differs")
    require(isinstance(workbench_rows, list) and len(workbench_rows) == 2, "need both original Workbench producers")
    parts = []
    for role in ROLES:
        name = part_name(role, version, run_id)
        if role.startswith("workbench-"):
            matches = [row for row in workbench_rows if row.get("name") == name]
            require(len(matches) == 1, "missing or duplicate original Workbench producer")
            row = matches[0]
            bound = identity(name, row["database_id"], row["digest"])["artifacts"][0]
        else:
            bound = {"name": name, "id": None, "digest": None}
        parts.append({"role": role, **bound})
    records = [{"path": name, "role": expected[name][0], "member": expected[name][1],
                **file_record(path)} for name, path in sorted(files.items())]
    document = {"schema": 1, "format": FORMAT, "repository": repository, "run_id": run_id,
                "producer_attempt": attempt, "artifact": metadata_name(version, run_id),
                "version": version, "source_sha": source_sha, "framework_sha": framework_sha,
                "delivery": delivery, "units": units, "parts": parts, "files": records}
    require(sum(row["size"] for row in records) <= 40 * 1024**3, "stage expansion limit")
    require(not os.path.lexists(output), "stage parts output must be new")
    output.mkdir()
    for role in ("metadata", "common", "core-x86_64", "core-aarch64"):
        (output / role).mkdir()
    for row in records:
        if not row["role"].startswith("workbench-"):
            link_file(files[row["path"]], output / row["role"] / row["member"])
    write_json(output / "draft-index.json", document)
    return document


def seal(root, uploaded):
    root = Path(root)
    document = read_json(root / "draft-index.json")
    require(set(uploaded) == {"common", "core-x86_64", "core-aarch64"}, "need all three uploaded stage parts")
    for part in document["parts"]:
        if part["role"] in uploaded:
            row = uploaded[part["role"]]
            bound = identity(part["name"], row["id"], row["digest"])["artifacts"][0]
            part.update(bound)
    validate_index(document, document["artifact"], document["version"], document["source_sha"],
                   document["framework_sha"])
    write_json(root / "metadata" / INDEX, document)
    return document


def validate_index(document, artifact, version, source_sha, framework_sha, plan=None):
    repository, run_id, attempt = context()
    keys = {"schema", "format", "repository", "run_id", "producer_attempt", "artifact", "version",
            "source_sha", "framework_sha", "delivery", "units", "parts", "files"}
    require(isinstance(document, dict) and set(document) == keys
            and type(document["schema"]) is int and document["schema"] == 1
            and document["format"] == FORMAT, "invalid partitioned stage schema")
    require(document["repository"] == repository and document["run_id"] == run_id
            and type(document["producer_attempt"]) is int and 0 < document["producer_attempt"] <= attempt,
            "stage run/attempt mismatch")
    require(document["artifact"] == artifact == metadata_name(version, run_id)
            and document["version"] == version and document["source_sha"] == source_sha
            and document["framework_sha"] == framework_sha
            and re.fullmatch(r"[0-9a-f]{40}", source_sha) and re.fullmatch(r"[0-9a-f]{40}", framework_sha),
            "stage source/framework/version mismatch")
    expected = layout(version, document["units"], document["delivery"])
    parts = document["parts"]
    require(isinstance(parts, list) and len(parts) == len(ROLES), "missing stage parts")
    ids, roles = set(), set()
    for part in parts:
        require(isinstance(part, dict) and set(part) == {"role", "name", "id", "digest"}
                and part["role"] in ROLES and part["role"] not in roles
                and part["name"] == part_name(part["role"], version, run_id), "duplicate or wrong stage part")
        identity(part["name"], part["id"], part["digest"])
        require(isinstance(part["id"], str) and part["id"] not in ids, "duplicate stage artifact ID")
        roles.add(part["role"]); ids.add(part["id"])
    rows = document["files"]
    require(isinstance(rows, list) and len(rows) == len(expected), "missing stage files")
    seen = set()
    for row in rows:
        require(isinstance(row, dict) and set(row) == {"path", "role", "member", "size", "sha256", "mode"}
                and isinstance(row["path"], str) and row["path"] in expected and row["path"] not in seen,
                "unknown or duplicate stage path")
        require((row["role"], row["member"]) == expected[row["path"]], "mixed stage role/architecture")
        require(type(row["size"]) is int and 0 <= row["size"] <= 20 * 1024**3
                and isinstance(row["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
                and type(row["mode"]) is int and row["mode"] == 0o644, "invalid stage file record")
        seen.add(row["path"])
    require(sum(row["size"] for row in rows) <= 40 * 1024**3, "stage expansion limit")
    if plan is not None:
        artifacts.check_plan(plan)
        baseline = plan["baseline"]
        require(plan["mode"] == "exact-assets" and baseline.get("staged") is True
                and plan["framework_sha"] == framework_sha and baseline["sha"] == source_sha
                and baseline["version"] == version and baseline["delivery"] == document["delivery"]
                and {u: v["version"] for u, v in baseline["units"].items()} == document["units"],
                "stage differs from exact plan")
        planned = {r["name"]: (r["size"], r["digest"]) for r in baseline["assets"]}
        observed = {r["path"].removeprefix("assets/"): (r["size"], "sha256:" + r["sha256"])
                    for r in rows if r["path"].startswith("assets/")}
        require(planned == observed and len(planned) == len(baseline["assets"]), "stage assets differ from plan")
    return document


def selected_roles(scope, arch):
    require(scope in ("full", "core", "workbench"), "unknown stage consumer scope")
    if scope == "full":
        require(arch is None, "full stage does not select one architecture")
        return ROLES
    require(arch in ARCHES, "stage consumer needs an exact architecture")
    return ("common", "core-" + arch) + (("workbench-" + arch,) if scope == "workbench" else ())


def verify_files(root, rows, extra=()):
    files = regular_files(root)
    require(set(files) == {r["member"] for r in rows} | set(extra), "stage part layout differs")
    for row in rows:
        require(file_record(files[row["member"]]) ==
                {key: row[key] for key in ("size", "sha256", "mode")}, "stage part bytes differ")
    return files


def load_bound(metadata, artifact, version, source_sha, framework_sha, plan=None):
    metadata = Path(metadata)
    require(metadata.is_dir() and not metadata.is_symlink(), "missing regular metadata directory")
    document = validate_index(read_json(metadata / INDEX), artifact, version, source_sha, framework_sha, plan)
    verify_files(metadata, [r for r in document["files"] if r["role"] == "metadata"], (INDEX,))
    return document


def requests(metadata, output, scope, arch, artifact, version, source_sha, framework_sha, plan=None):
    document = load_bound(metadata, artifact, version, source_sha, framework_sha, plan)
    output = Path(output)
    require(not os.path.lexists(output), "stage request output must be new")
    output.mkdir()
    roles = selected_roles(scope, arch)
    names = {}
    for part in document["parts"]:
        if part["role"] in roles:
            write_json(output / (part["role"] + ".json"), identity(part["name"], part["id"], part["digest"]))
            names[part["role"].replace("-", "_")] = part["name"]
    return names


def restore(parts_root, output, scope, arch, artifact, version, source_sha, framework_sha, plan=None):
    parts_root, output = Path(parts_root), Path(output)
    document = load_bound(parts_root / "metadata", artifact, version, source_sha, framework_sha, plan)
    roles = selected_roles(scope, arch)
    require({p.name for p in parts_root.iterdir()} == {"metadata", *roles}, "unexpected downloaded stage part")
    chosen = [row for row in document["files"] if row["role"] == "metadata" or row["role"] in roles]
    for role in roles:
        verify_files(parts_root / role, [r for r in chosen if r["role"] == role])
    require(not os.path.lexists(output), "restored stage output must be new")
    output.mkdir()
    for row in chosen:
        link_file(parts_root / row["role"] / row["member"], output / row["path"])
    return {"format": FORMAT, "scope": scope, "arch": arch, "files": len(chosen),
            "asset_files": len([r for r in chosen if r["path"].startswith("assets/")]),
            "bytes": sum(r["size"] for r in chosen), "index_sha256": artifacts.digest(parts_root / "metadata" / INDEX)}


def emit(values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as stream:
            for key, value in values.items():
                stream.write(key + "=" + (value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    for key in ("stage", "output"):
        p.add_argument("--" + key, type=Path, required=True)
    for key in ("version", "source-sha", "framework-sha", "delivery"):
        p.add_argument("--" + key, required=True)
    p = commands.add_parser("seal"); p.add_argument("--root", type=Path, required=True)
    p = commands.add_parser("metadata-identity"); p.add_argument("--artifact", required=True)
    p = commands.add_parser("request-metadata")
    p.add_argument("--artifact", required=True); p.add_argument("--output", type=Path, required=True)
    for name in ("requests", "restore"):
        p = commands.add_parser(name)
        p.add_argument("--metadata" if name == "requests" else "--parts", type=Path, required=True)
        p.add_argument("--output", type=Path, required=True)
        p.add_argument("--scope", choices=("full", "core", "workbench"), required=True)
        p.add_argument("--arch", choices=ARCHES)
        p.add_argument("--artifact", required=True)
        p.add_argument("--version"); p.add_argument("--source-sha")
        p.add_argument("--framework-sha", required=True)
        p.add_argument("--plan", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.stage, args.output, args.version, args.source_sha, args.framework_sha, args.delivery,
                json_value(os.environ["WORKBENCH_ARTIFACTS"]))
    elif args.command == "seal":
        uploaded = {role: {"id": os.environ[key + "_ID"], "digest": os.environ[key + "_DIGEST"]}
                    for role, key in (("common", "COMMON"), ("core-x86_64", "CORE_X86_64"), ("core-aarch64", "CORE_AARCH64"))}
        seal(args.root, uploaded)
    elif args.command == "metadata-identity":
        emit({"identity": identity(args.artifact, os.environ["STAGE_ID"], os.environ["STAGE_DIGEST"])})
    elif args.command == "request-metadata":
        value = download_artifact.expected_identity(json_value(os.environ["STAGE_IDENTITY"]))
        require(len(value["artifacts"]) == 1 and value["artifacts"][0]["name"] == args.artifact,
                "wrong stage metadata identity")
        write_json(args.output, value)
    else:
        plan = read_json(args.plan) if args.plan else None
        version = plan["baseline"]["version"] if plan else args.version
        source_sha = plan["baseline"]["sha"] if plan else args.source_sha
        if args.command == "requests":
            emit(requests(args.metadata, args.output, args.scope, args.arch, args.artifact,
                          version, source_sha, args.framework_sha, plan))
        else:
            print(json.dumps(restore(args.parts, args.output, args.scope, args.arch, args.artifact,
                                     version, source_sha, args.framework_sha, plan), sort_keys=True))


if __name__ == "__main__":
    main()
