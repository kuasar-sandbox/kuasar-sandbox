#!/usr/bin/env python3
"""Validate matched fixed-3 / density-16 coding-Agent completion evidence."""
import argparse
import hashlib
import json
import math
from pathlib import Path


def nonnegative_int(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f"{label}: expected nonnegative integer")
    return value


def seconds(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{label}: invalid timing")
    return value


def check_requests(wave, offered):
    """Keep unsuccessful polls visible; an HTTP 200 identity failure is fatal."""
    label = wave["label"]
    records = wave["data_request_records"]
    total = nonnegative_int(wave["data_requests"], label + ".data_requests")
    succeeded = nonnegative_int(wave["data_requests_success"], label + ".data_requests_success")
    if not total or len(records) != total or not 0 < succeeded <= total:
        raise ValueError(f"{label}: missing or inconsistent data-plane evidence")
    counts = {"requests": total, "succeeded": 0, "http_errors": 0,
              "transport_errors": 0, "late_errors": 0}
    task_counts = [{"http_ok": 0, "http_failed": 0} for _ in range(offered)]
    request_ids, failed_polls = set(), []
    for record in records:
        task = nonnegative_int(record["task"], label + ".request.task")
        status = nonnegative_int(record["http"], label + ".request.http")
        rid = record["request_id"]
        if task >= offered or not isinstance(rid, str) or not rid or rid in request_ids:
            raise ValueError(f"{label}: invalid or duplicate data-plane request identity")
        request_ids.add(rid)
        ok, late = record["ok"], record.get("late", False)
        if type(ok) is not bool or type(late) is not bool:
            raise ValueError(f"{label}: invalid data-plane result flags")
        if status == 200:
            if not ok:
                raise ValueError(f"{label}: HTTP 200 identity validation failed")
            counts["succeeded"] += 1
        else:
            if ok or (status != 0 and not 100 <= status <= 599):
                raise ValueError(f"{label}: inconsistent data-plane HTTP status")
            counts["transport_errors" if status == 0 else "http_errors"] += 1
            if late:
                counts["late_errors"] += 1
            else:
                failed_polls.append((task, status, rid))
        # The fixture records outstanding replies after cleanup separately; these
        # late replies do not contribute to each task's in-loop HTTP counters.
        if not late:
            task_counts[task]["http_ok" if ok else "http_failed"] += 1
    if counts["succeeded"] != succeeded:
        raise ValueError(f"{label}: data-plane success count mismatch")
    failures = [(f["task"], f["http"], f["expected_request_id"])
                for f in wave["response_failures"]]
    if failures != failed_polls:
        raise ValueError(f"{label}: data-plane failure diagnostics mismatch")
    return counts, task_counts


def load_run(path):
    root = Path(path)
    data = json.loads((root / "summary.json").read_text())
    exits = json.loads((root / "process-exit.json").read_text())
    for field in ("driver_exit", "observer_exit"):
        if nonnegative_int(exits[field], field) != 0:
            raise ValueError("driver/observer did not exit successfully")
    plan = data["plan"]
    if nonnegative_int(plan["business_retries"], "business retry policy") != 0:
        raise ValueError("business retry must be disabled in the run plan")
    waves, planned = data["results"], plan["waves"]
    if not waves or len(planned) != len(waves):
        raise ValueError("run and plan wave counts differ")
    labels, tasks, wave_sizes = set(), [], []
    elapsed = 0.0
    peak_running = peak_parent_bytes = peak_q = 0
    data_plane = {k: 0 for k in ("requests", "succeeded", "http_errors", "transport_errors", "late_errors")}
    for spec, wave in zip(planned, waves):
        label = wave["label"]
        if not isinstance(label, str) or not label or label in labels or spec["label"] != label:
            raise ValueError("duplicate or mismatched wave label")
        labels.add(label)
        offered = nonnegative_int(wave["offered"], label + ".offered")
        if (not offered or nonnegative_int(spec["n"], label + ".plan.n") != offered
                or len(wave["tasks"]) != offered):
            raise ValueError(f"{label}: offered count and task records mismatch")
        wave_sizes.append(offered)
        for field in ("accepted", "ready", "succeeded", "business_accepted", "completed", "correctly_completed"):
            if nonnegative_int(wave[field], label + "." + field) != offered:
                raise ValueError(f"{label}: incomplete admission/business result: {field}")
        if wave.get("safety_stop") or wave["create_failures"] or wave["cleanup"]["passed"] is not True:
            raise ValueError(f"{label}: unsafe result, admission failure, or cleanup failure")
        if any(wave["cleanup"].get(k, 0) for k in ("reserved_memory", "q", "cleanup")):
            raise ValueError(f"{label}: resources remain after cleanup")
        start = nonnegative_int(wave["parent_start"]["memory_events"]["oom_kill"], label + ".start.oom_kill")
        end = nonnegative_int(wave["parent_end"]["memory_events"]["oom_kill"], label + ".end.oom_kill")
        if end != start:
            raise ValueError(f"{label}: host cgroup OOM kill or counter reset")
        rounds = nonnegative_int(spec["rounds"], label + ".plan.rounds")
        if not rounds or rounds != wave["rounds"] or spec["profile"] != wave["profile"]:
            raise ValueError(f"{label}: workload differs from the plan")
        local_ids = [nonnegative_int(t["task"], label + ".task") for t in wave["tasks"]]
        if sorted(local_ids) != list(range(offered)):
            raise ValueError(f"{label}: duplicate, skipped or invalid wave-local task IDs")
        counts, task_counts = check_requests(wave, offered)
        for key in data_plane:
            data_plane[key] += counts[key]
        by_id = {t["task"]: t for t in wave["tasks"]}
        expected_hash = hashlib.sha256(f"VALUE = {rounds}\n".encode()).hexdigest()
        for local_id in range(offered):
            t, r = by_id[local_id], by_id[local_id]["result"]
            for field, count in task_counts[local_id].items():
                if nonnegative_int(t[field], label + "." + field) != count:
                    raise ValueError(f"{label}:{local_id}: per-task data-plane count mismatch")
            if (t["status"] != "success" or r["status"] != "success" or t["submit_http"] != 202
                    or r["task"] != local_id or r["start_count"] != 1
                    or not t["start_id"] or r["start_id"] != t["start_id"]
                    or not t["nonce"] or r["nonce"] != t["nonce"]
                    or r["round"] != rounds or r["profile"] != wave["profile"]
                    or r["peak_mib"] != t["peak_mib"] or r["action_count"] != rounds
                    or r["completed_tools"] != rounds * 3 or r["artifact_sha256"] != expected_hash):
                raise ValueError(f"{label}:{local_id}: incorrect or restarted business work")
            if "peak_override" in spec and t["peak_mib"] != spec["peak_override"]:
                raise ValueError(f"{label}:{local_id}: memory peak differs from the plan")
            # IDs reset in each wave. Pair logical work by declared wave order,
            # then local task ID, independently of result-list completion order.
            tasks.append({"ordinal": len(tasks), "peak_mib": t["peak_mib"], "rounds": rounds,
                          "profile": r["profile"], "action_count": r["action_count"],
                          "completed_tools": r["completed_tools"], "artifact_sha256": r["artifact_sha256"]})
        wave_seconds = seconds(wave["warmup_seconds"], label) + seconds(wave["task_wall_seconds"], label)
        if not math.isfinite(wave_seconds) or wave_seconds <= 0:
            raise ValueError(f"{label}: invalid timing")
        elapsed += wave_seconds
        running = nonnegative_int(wave["peak_running"], label + ".peak_running")
        if not 0 < running <= offered:
            raise ValueError(f"{label}: invalid observed running count")
        peak_running = max(peak_running, running)
        peak_parent_bytes = max(peak_parent_bytes, nonnegative_int(wave["peak_parent_bytes"], label + ".peak_parent_bytes"))
        peak_q = max(peak_q, nonnegative_int(wave["peak_q"], label + ".peak_q"))
    if not math.isfinite(elapsed):
        raise ValueError("invalid total timing")
    return {"tasks": tasks, "wave_sizes": wave_sizes, "seconds": elapsed,
            "peak_running": peak_running, "peak_memory_gib": peak_parent_bytes / 2**30,
            "peak_recovery_queue": peak_q, "data_plane": data_plane}


def compare(fixed, density):
    a, b = load_run(fixed), load_run(density)
    count = len(a["tasks"])
    if count != len(b["tasks"]):
        raise ValueError("logical Agent count mismatch")
    if count not in (24, 48):
        raise ValueError("matched experiment requires 24 or 48 logical Agents")
    for run, limit in ((a, 3), (b, 16)):
        expected = [min(limit, count - i) for i in range(0, count, limit)]
        if run["wave_sizes"] != expected:
            raise ValueError(f"planned waves do not implement the {limit}-concurrent experiment")
    for i, (x, y) in enumerate(zip(a["tasks"], b["tasks"])):
        if x != y:
            raise ValueError(f"workload or output mismatch at flattened Agent ordinal {i}")
    metrics = ("seconds", "wave_sizes", "peak_running", "peak_memory_gib", "peak_recovery_queue", "data_plane")
    return {"agents": count, "fixed": {k: a[k] for k in metrics}, "density": {k: b[k] for k in metrics},
            "speedup": a["seconds"] / b["seconds"], "correct_completion": count,
            "planned_business_retries": 0, "observed_business_restarts": 0,
            "retry_boundary": "disabled retry plan and one observed business start per Agent; raw client attempts are not audited",
            "identity_boundary": "Agent ordinal by declared wave order, then wave-local task ID",
            "concurrency_boundary": "sequential planned wave sizes, not a runtime allocator limit inferred from sampled peaks",
            "timing_boundary": "sum of per-wave warmup and execution; excludes case setup and cleanup"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixed", required=True, type=Path)
    parser.add_argument("--density", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = json.dumps(compare(args.fixed, args.density), indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.write_text(report)
        else:
            print(report, end="")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(f"invalid benchmark evidence: {error}")


if __name__ == "__main__":
    main()
