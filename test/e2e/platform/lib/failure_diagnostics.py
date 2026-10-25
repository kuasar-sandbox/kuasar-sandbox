#!/usr/bin/env python3
"""Collect bounded, allowlisted error excerpts, never raw case capabilities."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time

FILES = {
    "snapshot.read-recovery.sh": ("seed.snapshot.log", "recovered.snapshot.log", "fatal.snapshot.log",
                      "cow.log", "seed.log", "recovered.log", "verified.log", "cache.log", "proxy.log", "store.log"),
    "orchestrator.cluster-recovery.sh": ("create.response", "router.log", "node.log", "placer-1.log",
                    "registry-1.log", "registry-2.log", "registry-3.log"),
    "sandbox.cgroup.sh": tuple(f"{control}-{mode}{suffix}.log" for control in ("false", "true")
                              for mode in ("shared", "private") for suffix in ("", "-exec")),
    "orchestrator.resource-startup.sh": ("low-allocatable-1.journal", "low-allocatable-2.journal"),
    "orchestrator.proxy-inplace-restart.sh": tuple(f"{prefix}-{mode}.log"
        for mode in ("builtin-address", "builtin-datafd", "builtin-mmdsfd", "builtin-bothfd", "custom-bothfd", "app-bothfd")
        for prefix in ("client", "service-exit")),
}
# Closed vocabulary: retaining arbitrary log messages with a token denylist
# cannot guarantee that an unfamiliar credential encoding is removed.
SIGNALS = (
    "context deadline exceeded", "context canceled", "connection refused", "connection reset by peer",
    "broken pipe", "unexpected EOF", "EOF", "i/o timeout", "no such file or directory",
    "permission denied", "no space left on device", "resource temporarily unavailable",
    "device or resource busy", "transport is closing", "ciphertext hash mismatch",
    "invalid argument", "not found", "unavailable", "failed precondition", "fatal",
    "Kernel panic", "panic:", "snapshot", "capture", "upload", "quiesce", "Bad Gateway", "Gateway Timeout",
    "CG-FAIL", "received terminated", "CH exited code=", "CH didn't exit within", "SIGKILL",
    "proxy candidate shutdown", "completed_with_forced_close", "completed", "proxy master serving",
    "State 'stop-sigterm' timed out", "app_started", "notification timed out",
    "connection lost before the command reported an exit status", "inherited-guest-global-cgroupfs",
    "leaked-fd", "immediate-fork", "cannot-see-sandbox-init-pid1", "did-not-join-primary-pidns",
    "controller-not-enabled", "missing-controller-file", "real-app-root-not-empty",
    "missing-real-app-init", "missing-real-app",
)
LIMIT = 64 * 1024
UNIT_PROPERTIES = ("ActiveState", "SubState", "MainPID", "Result", "ExecMainCode", "ExecMainStatus",
                   "InvocationID", "KillMode", "SendSIGKILL")


def excerpts(text):
    result = []
    for number, line in enumerate(text.splitlines(), 1):
        found = [word for word in SIGNALS if re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", line, re.I)]
        observation = {}
        code = re.search(r"\[sandbox-ctl\] CH exited code=(-?[0-9]{1,10})(?![0-9])", line)
        if code:
            observation["ch_exit_code"] = int(code[1])
        # Cgroup probes have a finite marker grammar; no guest text is copied.
        markers = re.findall(r"\bCG-(?:primary|plugin|exec)(?:-GEN-[12])?-(?:FORK-OK 128|PID-(?:shared|private)-OK|REAL-OK|CONTROLLERS-OK)\b", line)
        if markers:
            observation["cgroup_markers"] = markers[:16]
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if isinstance(event, dict):
            if isinstance(event.get("kind"), str) and event["kind"] in {"exec_stop_term_pending", "exec_stop_incomplete", "exec_stop_complete",
                                     "full_shutdown", "worker_term_complete", "operation_complete"}:
                observation["kind"] = event["kind"]
                for key in ("main_pid", "forced", "elapsed"):
                    value = event.get(key)
                    if type(value) in (int, float, bool) and 0 <= value < 10**12:
                        observation[key] = value
            for key, allowed in {"SERVICE_RESULT": {"success", "timeout", "signal", "exit-code", "oom-kill"},
                                 "EXIT_CODE": {"exited", "killed", "dumped"},
                                 "EXIT_STATUS": {"0", "TERM", "KILL", "ABRT"}}.items():
                if isinstance(event.get(key), str) and event[key] in allowed:
                    observation[key] = event[key]
            if isinstance(event.get("INVOCATION_ID"), str) and re.fullmatch(r"[a-f0-9]{32}", event["INVOCATION_ID"]):
                observation["invocation"] = event["INVOCATION_ID"]
        if found or observation:
            # Only fixed vocabulary is emitted. All addresses, identifiers,
            # bodies, headers, config values and unrecognized text are omitted.
            result.append({"tail_line": number, "error_terms": found, **observation})
    return result[-64:]


def read_command(command):
    """Private bounded temporary output; stderr and failed command text are never emitted."""
    with tempfile.TemporaryFile() as stream:
        try:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"error": type(error).__name__}, ""
        size = stream.tell()
        stream.seek(max(0, size - LIMIT))
        text = stream.read(LIMIT).decode("utf-8", errors="replace")
        return {"exit_code": result.returncode, "tail_truncated": size > LIMIT}, text


def process_state(pid):
    if not re.fullmatch(r"[0-9]{1,10}", str(pid)) or int(pid) <= 1:
        return {}
    result = {"pid": int(pid)}
    try:
        with open(f"/proc/{int(pid)}/status") as stream:
            text = stream.read(8192)
        for field in ("State", "PPid", "Threads", "TracerPid"):
            value = re.search(r"^" + field + r":\s+([A-Z]|[0-9]+)\b", text, re.M)
            if value:
                result[field] = value[1]
    except OSError as error:
        result["errno"] = error.errno
    return result


def unit_state(unit):
    if not re.fullmatch(r"(?:sandbox-runner-|kuasar-proxy-)[A-Za-z0-9_.@-]{1,180}\.service", unit):
        return {"unavailable": True}
    result = {"unit": unit, "observed_ns": time.time_ns()}
    result["show"], text = read_command(["systemctl", "show", unit,
                                        *["--property=" + prop for prop in UNIT_PROPERTIES]])
    properties = {}
    for row in text.splitlines():
        key, _, value = row.partition("=")
        if key in UNIT_PROPERTIES and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
            properties[key] = value
    result["properties"] = properties
    result["journal"], text = read_command(["journalctl", "--no-pager", "--quiet", "--output=json",
        "--output-fields=MESSAGE,PRIORITY,_PID,__REALTIME_TIMESTAMP,_SYSTEMD_INVOCATION_ID",
        "--lines=200", "--unit=" + unit])
    events = []
    for row in text.splitlines():
        try:
            event = json.loads(row)
        except ValueError:
            continue
        if not isinstance(event, dict) or not isinstance(event.get("MESSAGE"), str):
            continue
        terms = excerpts(event["MESSAGE"])
        if not terms:
            continue
        item = {"excerpts": terms}
        for key in ("__REALTIME_TIMESTAMP", "_PID", "PRIORITY", "_SYSTEMD_INVOCATION_ID"):
            value = event.get(key)
            if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{1,32}", value):
                item[key] = value
        events.append(item)
    result["journal"].update(rows_observed=len(text.splitlines()), row_limit=200, events=events)
    result["main_process"] = process_state(properties.get("MainPID", ""))
    return result


def cgroup_state(group):
    # Only the current case's service subtree; never inspect other tasks or recurse.
    # systemd escapes template-slice hyphens as literal backslash-x2d names.
    component = r"(?:[A-Za-z0-9_@.-]|\\x[0-9a-fA-F]{2})+"
    if not re.fullmatch(r"/(?:" + component + r"\.slice/)+(?:sandbox-runner-|kuasar-proxy-)" + component + r"\.service", group):
        return {"unavailable": True}
    result = {}
    for child in ("", "/ctl", "/vmm"):
        path = Path("/sys/fs/cgroup" + group + child)
        entry = {}
        try:
            # Kernel-created cgroup files are bounded and contain only numeric fields.
            with (path / "cgroup.procs").open() as stream:
                pids = stream.read(4096).split()
            entry["pids"] = [int(pid) for pid in pids[:16] if pid.isdecimal()]
            entry["pids_truncated"] = len(pids) > 16
        except OSError as error:
            entry["errno"] = error.errno
        result[child or "/"] = entry
    return result


def collect(case, phase, source, line, status, work, output, http_status, unit="", pid="", cgroup="", command="case-command"):
    if case not in FILES or not re.fullmatch(r"[a-z-]+", phase):
        raise ValueError("invalid case identity")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.sh", source):
        raise ValueError("invalid source identity")
    if command not in {"case-command", "sandbox-ctl-exec", "proxy-lifecycle-client"}:
        raise ValueError("invalid command identity")
    record = {"case": case, "phase": phase, "source": source, "line": int(line),
              "exit_code": int(status), "command": command, "observed_ns": time.time_ns(), "logs": {},
              "redaction": "fixed error vocabulary and selected terminal fields only"}
    if case == "orchestrator.cluster-recovery.sh" and re.fullmatch(r"[1-5][0-9]{2}", http_status):
        record["http_status"] = int(http_status)
    if work:
        try:
            directory = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            directory = None
            record["logs_unavailable"] = True
    else:
        directory = None
    if directory is not None:
        try:
            for name in FILES[case]:
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                except OSError:
                    continue
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    stream.seek(max(0, info.st_size - LIMIT))
                    text = stream.read(LIMIT).decode("utf-8", errors="replace")
                record["logs"][name] = {"bytes": info.st_size, "tail_truncated": info.st_size > LIMIT,
                                        "excerpt_limit": 64, "excerpts": excerpts(text)}
        finally:
            os.close(directory)
    if case in {"orchestrator.resource-startup.sh", "orchestrator.proxy-inplace-restart.sh"}:
        record["service"] = unit_state(unit)
    if case in {"sandbox.cgroup.sh", "orchestrator.resource-startup.sh"}:
        record["process"] = process_state(pid)
    if case == "orchestrator.resource-startup.sh":
        record["cgroup"] = cgroup_state(cgroup)
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=case + "-", suffix=".json", dir=destination)
    with os.fdopen(fd, "w") as stream:
        json.dump(record, stream, sort_keys=True)
        stream.write("\n")
    return Path(path)


if __name__ == "__main__":
    try:
        record = collect(*sys.argv[1:])
        # The case owns its private state, which CI removes after execution.
        # Retain only the collector's closed-vocabulary result in the job log.
        print(record.read_text(), end="", file=sys.stderr)
    except Exception:
        # No traceback containing private paths, config, or file contents.
        print("Cannot collect bounded E2E failure diagnostics", file=sys.stderr)
        sys.exit(1)
