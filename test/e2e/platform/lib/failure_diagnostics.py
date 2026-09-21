#!/usr/bin/env python3
"""Collect bounded, allowlisted error excerpts, never raw case capabilities."""
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

FILES = {
    "read-recovery": ("seed.snapshot.log", "recovered.snapshot.log", "fatal.snapshot.log",
                      "cow.log", "seed.log", "recovered.log", "verified.log", "cache.log", "proxy.log", "store.log"),
    "registry-n3": ("build-status.body", "router.log", "node-stub.log", "placer-1.log",
                    "registry-1.log", "registry-2.log", "registry-3.log"),
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
)
LIMIT = 64 * 1024


def excerpts(text):
    result = []
    for number, line in enumerate(text.splitlines(), 1):
        found = [word for word in SIGNALS if re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", line, re.I)]
        if found:
            # Only fixed vocabulary is emitted. All addresses, identifiers,
            # bodies, headers, config values and unrecognized text are omitted.
            result.append({"tail_line": number, "error_terms": found})
    return result[-64:]


def collect(case, phase, source, line, status, work, output, http_status):
    if case not in FILES or not re.fullmatch(r"[a-z-]+", phase):
        raise ValueError("invalid case identity")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.sh", source):
        raise ValueError("invalid source identity")
    record = {"case": case, "phase": phase, "source": source, "line": int(line),
              "exit_code": int(status), "logs": {}, "redaction": "fixed error vocabulary only"}
    if case == "registry-n3" and re.fullmatch(r"[1-5][0-9]{2}", http_status):
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
                                        "excerpts": excerpts(text)}
        finally:
            os.close(directory)
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=case + "-", suffix=".json", dir=destination)
    with os.fdopen(fd, "w") as stream:
        json.dump(record, stream, sort_keys=True)
        stream.write("\n")
        # Only this sanitized report is read by the unprivileged artifact
        # uploader after a case's existing sudo re-exec. Raw logs stay private.
        os.fchmod(stream.fileno(), 0o644)
    return Path(path)


if __name__ == "__main__":
    try:
        collect(*sys.argv[1:])
    except Exception:
        # No traceback containing private paths, config, or file contents.
        print("Cannot collect bounded E2E failure diagnostics", file=sys.stderr)
        sys.exit(1)
