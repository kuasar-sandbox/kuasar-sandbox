#!/usr/bin/env python3
"""Validate and compare fixed-concurrency and density Agent work completion evidence."""
import argparse
import json
from pathlib import Path


def load_run(path):
    root = Path(path)
    data = json.loads((root / "summary.json").read_text())
    exits = json.loads((root / "process-exit.json").read_text())
    waves = data["results"]
    if not waves or exits != {"driver_exit": 0, "observer_exit": 0}:
        raise ValueError("missing waves or nonzero driver/observer exit")
    if any(not w["cleanup"]["passed"] or w.get("safety_stop") for w in waves):
        raise ValueError("unsafe termination or incomplete cleanup")
    tasks = [task for w in waves for task in w["tasks"]]
    ids = [task["task"] for task in tasks]
    if len(ids) != len(set(ids)) or len(ids) != sum(w["offered"] for w in waves):
        raise ValueError("duplicate or rejected agent: no business retry is allowed")
    if any(w["accepted"] != w["offered"] or w["succeeded"] != w["offered"] for w in waves):
        raise ValueError("admission or business completion incomplete")
    if any(task["status"] != "success" for task in tasks):
        raise ValueError("non-successful task")
    if any(w["data_requests_success"] != w["data_requests"] for w in waves):
        raise ValueError("failed data-plane request")
    if any(w["parent_end"]["memory_events"].get("oom_kill", 0) != w["parent_start"]["memory_events"].get("oom_kill", 0) for w in waves):
        raise ValueError("host OOM kill")
    return {"tasks": {t["task"]: t for t in tasks}, "waves": waves,
            "seconds": sum(w["warmup_seconds"] + w["task_wall_seconds"] for w in waves),
            "peak_running": max(w["peak_running"] for w in waves),
            "peak_memory_gib": max(w["peak_parent_bytes"] for w in waves) / 2**30,
            "peak_recovery_queue": max(w["peak_q"] for w in waves)}


def compare(fixed, density):
    a, b = load_run(fixed), load_run(density)
    if set(a["tasks"]) != set(b["tasks"]):
        raise ValueError("agent identity mismatch")
    for key in a["tasks"]:
        x, y = a["tasks"][key], b["tasks"][key]
        signature = lambda t: (t["peak_mib"], t["result"]["action_count"],
                               t["result"]["completed_tools"], t["result"]["artifact_sha256"])
        if signature(x) != signature(y):
            raise ValueError("workload or output mismatch for agent %d" % key)
    return {"agents": len(a["tasks"]), "fixed": {k: a[k] for k in ("seconds", "peak_running", "peak_memory_gib", "peak_recovery_queue")},
            "density": {k: b[k] for k in ("seconds", "peak_running", "peak_memory_gib", "peak_recovery_queue")},
            "speedup": a["seconds"] / b["seconds"], "correct_completion": len(a["tasks"]),
            "business_retries": 0,
            "timing_boundary": "sum of per-wave warmup and execution; excludes case setup and cleanup"}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fixed", required=True, type=Path)
    p.add_argument("--density", required=True, type=Path)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    report = json.dumps(compare(args.fixed, args.density), indent=2) + "\n"
    if args.output:
        args.output.write_text(report)
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
