#!/usr/bin/env python3
"""Freeze repository-local cache trust before executing freshly fetched inputs."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess


OWNERS = {name: "kuasar-sandbox/" + name for name in
          ("accelerator", "connector", "guest-runtime", "orchestrator", "sandboxer")}
OWNERS.update(platform="kuasar-sandbox/kuasar-sandbox", **{"kuasar-sandbox": "kuasar-sandbox/kuasar-sandbox"})
GROUPS = {"test-helpers", "test-overlays", "kernel-unit"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read_file(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "cache identity input is not a regular file")
        return stream.read()


def snapshot(path, *, git=False):
    """Hash actual bytes/modes/link text, never a symlink's destination."""
    entries = {}

    def visit(current, relative):
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            contents = os.fsencode(os.readlink(current))
            entries[relative] = {"mode": "120000", "sha256": hashlib.sha256(contents).hexdigest(),
                                 "blob": hashlib.sha1(b"blob " + str(len(contents)).encode() + b"\0" + contents).hexdigest()}
        elif stat.S_ISREG(mode):
            with os.fdopen(os.open(current, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
                info = os.fstat(stream.fileno())
                require(stat.S_ISREG(info.st_mode), "cache source changed type while reading")
                content = hashlib.sha256()
                blob = hashlib.sha1(b"blob " + str(info.st_size).encode() + b"\0")
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    content.update(chunk)
                    blob.update(chunk)
            entries[relative] = {"mode": "100755" if mode & 0o111 else "100644",
                                 "sha256": content.hexdigest(), "blob": blob.hexdigest()}
        elif stat.S_ISDIR(mode):
            if relative:
                entries[relative] = {"mode": "040000"}
            for child in sorted(current.iterdir()):
                if git and relative == "" and child.name == ".git":
                    continue
                visit(child, child.name if not relative else relative + "/" + child.name)
        else:
            raise ValueError("special file in cache source identity: " + str(current))

    visit(path, "")
    return digest(entries), entries


def git_read(source, *arguments):
    # These plumbing operations do not run status/diff, clean filters or
    # fsmonitor. Do not pass orchestration credentials to any Git subprocess.
    environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                       GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    return subprocess.check_output(
        ["git", "--git-dir=" + str(source / ".git"), "--work-tree=" + str(source),
         "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *arguments],
        env=environment, timeout=60)


def public_main(repository, sha):
    def api(path):
        return json.loads(subprocess.check_output(["gh", "api", "repos/" + repository + path],
                                                 text=True, timeout=60))
    state = api("")
    require(state.get("full_name") == repository and state.get("private") is False
            and state.get("visibility", "public") == "public", "cache source repository is not public")
    return api("/compare/" + sha + "...main").get("merge_base_commit", {}).get("sha") == sha


def source_record(source, relative, known, *, admit):
    private_git = not source.is_symlink() and source.is_dir() and not (source / ".git").is_symlink() and (source / ".git").is_dir()
    content, entries = snapshot(source, git=private_git)
    record = {"path": relative, "content_sha256": content, "on_main": False}
    if not private_git:
        record["kind"] = "snapshot"
        return record
    record["kind"] = "git"
    sha = git_read(source, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "cache source must have an exact commit")
    remote = git_read(source, "config", "--local", "--no-includes", "--get", "remote.origin.url").decode().strip()
    match = re.fullmatch(r"https://github.com/(kuasar-sandbox/(?:kuasar-sandbox|accelerator|connector|sandboxer|orchestrator|guest-runtime))(?:\.git)?", remote)
    repository = match.group(1) if match else None
    expected = {}
    for row in git_read(source, "ls-tree", "-rz", "--full-tree", sha).split(b"\0"):
        if not row:
            continue
        header, name = row.split(b"\t", 1)
        mode, kind, blob = header.decode().split()
        expected[os.fsdecode(name)] = {"mode": mode, "blob": blob}
    actual = {name: {key: row[key] for key in ("mode", "blob")} for name, row in entries.items()
              if row["mode"] != "040000"}
    directories = {str(parent) for name in expected for parent in Path(name).parents if str(parent) != "."}
    clean = actual == expected and {name for name, row in entries.items() if row["mode"] == "040000"} == directories
    record.update(repository=repository, sha=sha, clean=clean)
    if repository is None:
        record["remote_sha256"] = hashlib.sha256(remote.encode()).hexdigest()
    if admit and known and clean and repository == OWNERS[source.name]:
        record["on_main"] = public_main(repository, sha)
    return record


def selected_revisions(value):
    require(isinstance(value, dict), "invalid cache source revision map")
    result = {}
    for owner, record in sorted(value.items()):
        if isinstance(record, str):
            record = {"repository": OWNERS.get(owner), "sha": record}
        require(isinstance(record, dict) and record.get("repository") in OWNERS.values()
                and re.fullmatch(r"[0-9a-f]{40}", record.get("sha", "")), "invalid exact cache source revision")
        result[owner] = {"repository": record["repository"], "sha": record["sha"]}
    return result


def candidate_records(value):
    require(isinstance(value, list), "invalid cache companion set")
    keys = ("repository", "pull_request_number", "base_ref", "base_sha", "head_sha", "candidate_sha")
    return sorted(({key: row[key] for key in keys if key in row} for row in value), key=canonical)


def transport_identity(path):
    """Only the existing host-created .ci handoff is transport, not source."""
    stable, understood = {}, True
    plan_path = path / "plan.json"
    plan = json.loads(read_file(plan_path)) if plan_path.is_file() and not plan_path.is_symlink() else {}
    for child in sorted(path.iterdir()):
        if child.is_symlink():
            understood = False
            stable[child.name] = snapshot(child)[0]
        elif child.name == "plan.json" and child.is_file():
            stable[child.name] = {key: selected_revisions(plan[key]) for key in ("sources", "test_revisions") if key in plan}
            for key in ("kernel_sha", "framework_sha"):
                if key in plan:
                    require(re.fullmatch(r"[0-9a-f]{40}", plan[key]), "invalid exact cache plan revision")
                    stable[child.name][key] = plan[key]
            stable[child.name]["candidate_records"] = candidate_records(plan.get("candidate_records", []))
        elif child.name == "test-revisions.json" and child.is_file():
            stable[child.name] = selected_revisions(json.loads(read_file(child)))
        elif child.name == "workbench.json" and child.is_file():
            # Actual compiler/ABI inputs belong to the inner recipe key. The
            # full immutable image selection remains in the Workbench receipt.
            json.loads(read_file(child))
        elif child.name in ("assets", "readers") and child.is_dir():
            content, entries = snapshot(child)
            regular = all(row["mode"] in ("100644", "100755") for row in entries.values())
            if child.name == "assets":
                declared = {row["name"]: row["digest"] for row in plan.get("baseline", {}).get("assets", [])}
                known = regular and all(name in declared and declared[name] == "sha256:" + row["sha256"]
                                        for name, row in entries.items())
                # Release labels are only locators after verifying the actual
                # archive against the plan. Source pins bind its producer.
                stable[child.name] = (sorted(({key: row[key] for key in ("mode", "sha256")}
                                              for name, row in entries.items() if name != "SHA256SUMS"), key=canonical)
                                      if known else content)
            else:
                known = regular and set(entries) == {"mkfs.erofs", "fsck.erofs", "dump.erofs",
                    "runtime-payloads.py", "erofs-readers.COPYING", "SHA256SUMS"}
                # Reader names select different executables; keep that binding.
                stable[child.name] = {name: {key: row[key] for key in ("mode", "sha256")}
                                      for name, row in entries.items()} if known else content
            understood = understood and known
        else:
            understood = False
            stable[child.name] = snapshot(child)[0]
    return stable, understood


def event_identity():
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event not in ("pull_request", "pull_request_target", "workflow_dispatch"):
        return {}, False
    path = os.environ.get("GITHUB_EVENT_PATH")
    require(path or event == "workflow_dispatch", "PR cache identity needs the actual event")
    data = json.loads(read_file(Path(path))) if path else {}
    if event in ("pull_request", "pull_request_target"):
        pr = data["pull_request"]
        number = data.get("number", pr.get("number"))
        require(type(number) is int and number > 0, "invalid actual PR cache identity")
        result = {"number": number}
        for name in ("base", "head"):
            side = pr[name]
            require(re.fullmatch(r"[0-9a-f]{40}", side.get("sha", "")), "invalid actual PR revision")
            result[name] = {"repository": side["repo"]["full_name"], "sha": side["sha"], "ref": side["ref"]}
        require(result["base"]["repository"] == os.environ.get("GITHUB_REPOSITORY"), "PR cache caller mismatch")
        return result, True
    inputs = data.get("inputs") or {}
    selected = {key: inputs[key] for key in ("candidate_repository", "pull_request_number", "candidate_sha",
                "base_sha", "head_sha", "base_ref", "companion_candidates") if inputs.get(key) not in (None, "", "0", "[]")}
    if "companion_candidates" in selected and isinstance(selected["companion_candidates"], str):
        selected["companion_candidates"] = json.loads(selected["companion_candidates"])
    if "companion_candidates" in selected:
        selected["companion_candidates"] = candidate_records(selected["companion_candidates"])
    return selected, bool(selected)


def decide(sources: Path, receipt: Path) -> dict:
    sources = sources.resolve()
    context = {"repository": os.environ.get("GITHUB_REPOSITORY"), "ref": os.environ.get("GITHUB_REF"),
               "event": os.environ.get("GITHUB_EVENT_NAME"), "run_id": os.environ.get("GITHUB_RUN_ID"),
               "source_root": str(sources)}
    require(not receipt.is_symlink() and not receipt.resolve().is_relative_to(sources), "cache scope receipt must stay outside candidate sources")
    if receipt.exists():
        previous = json.loads(read_file(receipt))
        require(all(previous.get(key) == value for key, value in context.items()), "foreign cache scope receipt")
        require(previous.get("scope") in ("trusted", "candidate") and
                (previous.get("namespace") == "trusted" if previous["scope"] == "trusted" else
                 re.fullmatch(r"candidate-[0-9a-f]{64}", previous.get("namespace", ""))), "invalid cache scope receipt")
        return previous  # Never inspect candidate-modified Git or source again.
    event, candidate = event_identity()
    transport, understood = {}, True
    if (sources / ".ci").is_dir() and not (sources / ".ci").is_symlink():
        transport, understood = transport_identity(sources / ".ci")
    if transport.get("plan.json", {}).get("candidate_records"):
        candidate = True
    admit = context["ref"] == "refs/heads/main" and not candidate
    records = []
    for source in sorted(sources.iterdir()):
        if source.name == ".ci" and source.is_dir() and not source.is_symlink():
            continue
        if source.name in GROUPS and source.is_dir() and not source.is_symlink():
            children = sorted(source.iterdir())
            if not children:
                records.append(source_record(source, source.name, False, admit=admit))
            for child in children:
                known = child.name in OWNERS and (source.name != "kernel-unit" or child.name == "guest-runtime")
                records.append(source_record(child, source.name + "/" + child.name, known, admit=admit))
        else:
            records.append(source_record(source, source.name, source.name in OWNERS, admit=admit))
    scope = "trusted" if understood and records and all(record["on_main"] for record in records) else "candidate"
    identity = {key: context[key] for key in ("repository", "ref", "event")}
    identity.update(event_inputs=event, sources=records, transport=transport)
    namespace = "trusted" if scope == "trusted" else "candidate-" + digest(identity)
    result = {**context, "scope": scope, "namespace": namespace, "sources": records, "event_inputs": event,
              "transport": transport}
    receipt.parent.mkdir(parents=True, exist_ok=True)
    temporary = receipt.with_name(receipt.name + ".tmp")
    temporary.write_bytes(canonical(result) + b"\n")
    temporary.replace(receipt)
    return result
