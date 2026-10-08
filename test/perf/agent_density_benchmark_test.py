"""No-KVM regression tests, discovered by make test-perf-tools."""
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parent / "agent-density" / "benchmark.py"
spec = importlib.util.spec_from_file_location("agent_density_benchmark", SCRIPT)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def wave(label, n):
    tasks, records = [], []
    for local_id in range(n):
        start_id, nonce = f"{label}:{local_id}", f"test-{label}-{local_id}"
        tasks.append({
            "task": local_id, "status": "success", "peak_mib": 192,
            "http_ok": 2, "http_failed": 0, "submit_http": 202,
            "start_id": start_id, "nonce": nonce,
            "result": {
                "task": local_id, "status": "success", "start_count": 1,
                "start_id": start_id, "nonce": nonce, "round": 4,
                "profile": "held", "peak_mib": 192, "action_count": 4,
                "completed_tools": 12,
                "artifact_sha256": hashlib.sha256(b"VALUE = 4\n").hexdigest(),
            },
        })
        records.extend({"task": local_id, "request_id": f"{label}-{local_id}-{i}",
                        "http": 200, "ok": True} for i in range(2))
    return {
        "label": label, "offered": n, "accepted": n, "ready": n,
        "succeeded": n, "business_accepted": n, "completed": n,
        "correctly_completed": n, "rounds": 4, "profile": "held", "tasks": tasks,
        "cleanup": {"passed": True, "reserved_memory": 0, "q": 0},
        "warmup_seconds": 2, "task_wall_seconds": 9,
        "peak_running": n, "peak_parent_bytes": 1000000, "peak_q": 0,
        "data_requests": len(records), "data_requests_success": len(records),
        "data_request_records": records, "create_failures": [], "response_failures": [],
        "parent_start": {"memory_events": {"oom_kill": 0}},
        "parent_end": {"memory_events": {"oom_kill": 0}},
    }


def run_data(count, limit):
    waves = [wave(f"batch-{i}", min(limit, count-i)) for i in range(0, count, limit)]
    return {"plan": {"business_retries": 0, "waves": [
        {"label": w["label"], "n": w["offered"], "rounds": 4,
         "profile": "held", "peak_override": 192} for w in waves]}, "results": waves}


def add_failed_poll(wave, status=502, late=False):
    record = {"task": 0, "http": status, "ok": False, "request_id": "failed-poll", "late": late}
    wave["data_request_records"].append(record)
    wave["data_requests"] += 1
    if not late:
        wave["tasks"][0]["http_failed"] += 1
        wave["response_failures"].append({"task": 0, "http": status, "expected_request_id": record["request_id"]})


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixed, self.density = (Path(self.tmp.name) / x for x in ("fixed", "density"))
        self.fixed.mkdir()
        self.density.mkdir()
        self.f, self.d = run_data(24, 3), run_data(24, 16)
        self.save()

    def save(self):
        for path, data in ((self.fixed, self.f), (self.density, self.d)):
            (path / "summary.json").write_text(json.dumps(data))
            (path / "process-exit.json").write_text(json.dumps({"driver_exit": 0, "observer_exit": 0}))

    def compare(self):
        self.save()
        return benchmark.compare(self.fixed, self.density)

    def rejected(self, text):
        with self.assertRaisesRegex(ValueError, text):
            self.compare()

    def test_wave_local_ids_reset_in_both_24_and_48_agent_runs(self):
        for count in (24, 48):
            with self.subTest(count=count):
                self.f, self.d = run_data(count, 3), run_data(count, 16)
                result = self.compare()
                self.assertEqual(result["correct_completion"], count)
                self.assertEqual(result["fixed"]["wave_sizes"], [3] * (count // 3))
                self.assertEqual(result["planned_business_retries"], 0)
                self.assertEqual(result["observed_business_restarts"], 0)
                self.assertNotIn("business_retries", result)

    def test_task_result_completion_order_does_not_change_pairing(self):
        self.d["results"][0]["tasks"].reverse()
        self.assertEqual(self.compare()["correct_completion"], 24)

    def test_http_poll_failure_is_reported_not_business_retry(self):
        add_failed_poll(self.d["results"][0])
        report = self.compare()
        self.assertEqual(report["correct_completion"], 24)
        self.assertEqual(report["density"]["data_plane"]["http_errors"], 1)
        self.assertEqual(report["density"]["data_plane"]["requests"], 49)
        self.assertEqual(report["density"]["data_plane"]["succeeded"], 48)

    def test_transport_poll_failure_is_reported_separately(self):
        add_failed_poll(self.d["results"][0], status=0)
        data = self.compare()["density"]["data_plane"]
        self.assertEqual(data["transport_errors"], 1)
        self.assertEqual(data["http_errors"], 0)

    def test_late_failure_is_not_in_loop_task_counter(self):
        add_failed_poll(self.d["results"][0], late=True)
        data = self.compare()["density"]["data_plane"]
        self.assertEqual(data["late_errors"], 1)
        self.assertEqual(data["http_errors"], 1)

    def test_http_200_identity_failure_is_fatal_even_when_late(self):
        for late in (False, True):
            with self.subTest(late=late):
                self.d = run_data(24, 16)
                add_failed_poll(self.d["results"][0], status=200, late=late)
                self.rejected("identity validation")

    def test_missing_failure_diagnostics_are_rejected(self):
        add_failed_poll(self.d["results"][0])
        self.d["results"][0]["response_failures"] = []
        self.rejected("diagnostics")

    def test_summary_request_count_mismatch_is_rejected(self):
        self.d["results"][0]["data_requests_success"] -= 1
        self.rejected("success count")

    def test_task_request_count_mismatch_is_rejected(self):
        self.d["results"][0]["tasks"][0]["http_failed"] = 1
        self.rejected("per-task data-plane")

    def test_duplicate_request_identity_is_rejected(self):
        rows = self.d["results"][0]["data_request_records"]
        rows[1]["request_id"] = rows[0]["request_id"]
        self.rejected("duplicate data-plane")

    def test_business_retry_plan_and_observed_restart_are_rejected(self):
        self.f["plan"]["business_retries"] = 1
        self.rejected("retry")
        self.f["plan"]["business_retries"] = 0
        self.f["results"][0]["tasks"][0]["result"]["start_count"] = 2
        self.rejected("restarted")

    def test_wrong_task_start_or_nonce_is_rejected(self):
        for field in ("task", "start_id", "nonce"):
            with self.subTest(field=field):
                self.d = run_data(24, 16)
                self.d["results"][0]["tasks"][0]["result"][field] = "wrong"
                self.rejected("incorrect")

    def test_same_but_incorrect_output_in_both_runs_is_rejected(self):
        for data in (self.f, self.d):
            data["results"][0]["tasks"][0]["result"]["artifact_sha256"] = "0" * 64
        self.rejected("incorrect")

    def test_work_counts_must_match_rounds_not_merely_each_other(self):
        for field in ("action_count", "completed_tools"):
            with self.subTest(field=field):
                self.d = run_data(24, 16)
                self.d["results"][0]["tasks"][0]["result"][field] = 1
                self.rejected("incorrect")

    def test_plan_workload_mismatch_is_rejected(self):
        self.d["plan"]["waves"][0]["rounds"] = 5
        self.rejected("workload differs")

    def test_cross_run_workload_mismatch_is_rejected(self):
        for spec in self.d["plan"]["waves"]:
            spec["peak_override"] = 256
        for w in self.d["results"]:
            for t in w["tasks"]:
                t["peak_mib"] = t["result"]["peak_mib"] = 256
        self.rejected("workload or output mismatch")

    def test_duplicate_and_noninteger_local_task_ids_are_rejected(self):
        self.f["results"][0]["tasks"][1]["task"] = 0
        self.rejected("duplicate")
        self.f["results"][0]["tasks"][1]["task"] = True
        self.rejected("integer")

    def test_wrong_agent_count_and_configured_batch_sizes_are_rejected(self):
        self.f, self.d = run_data(3, 3), run_data(3, 16)
        self.rejected("24 or 48")
        self.f, self.d = run_data(24, 4), run_data(24, 16)
        self.rejected("3-concurrent")
        self.f, self.d = run_data(24, 3), run_data(24, 8)
        self.rejected("16-concurrent")

    def test_sampled_peak_is_not_treated_as_configured_concurrency(self):
        for w in self.d["results"]:
            w["peak_running"] = 2
        self.assertEqual(self.compare()["density"]["peak_running"], 2)

    def test_wave_order_and_count_must_match_plan(self):
        self.d["results"].reverse()
        self.rejected("mismatched wave")
        self.d["results"].pop()
        self.rejected("wave counts")

    def test_admission_completion_cleanup_and_oom_failures_are_rejected(self):
        original = copy.deepcopy(self.f)
        for field in ("accepted", "ready", "succeeded", "business_accepted", "completed", "correctly_completed"):
            with self.subTest(field=field):
                self.f = copy.deepcopy(original)
                self.f["results"][0][field] -= 1
                self.rejected("incomplete")
        self.f = copy.deepcopy(original)
        self.f["results"][0]["cleanup"]["reserved_memory"] = 1
        self.rejected("resources remain")
        self.f = copy.deepcopy(original)
        self.f["results"][0]["parent_end"]["memory_events"]["oom_kill"] = 1
        self.rejected("OOM")

    def test_missing_request_evidence_is_rejected(self):
        self.f["results"][0]["data_request_records"] = []
        self.rejected("data-plane evidence")

    def test_negative_nan_and_infinite_timing_are_rejected(self):
        for value in (-1, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.f["results"][0]["warmup_seconds"] = value
                self.rejected("timing")

    def test_nonzero_process_exit_is_rejected(self):
        (self.density / "process-exit.json").write_text('{"driver_exit": 1, "observer_exit": 0}')
        with self.assertRaisesRegex(ValueError, "exit"):
            benchmark.compare(self.fixed, self.density)

    def test_cli_fails_without_writing_a_success_report_on_missing_fields(self):
        del self.f["results"][0]["data_request_records"]
        self.save()
        output = Path(self.tmp.name) / "report.json"
        result = subprocess.run([sys.executable, str(SCRIPT), "--fixed", str(self.fixed),
                                 "--density", str(self.density), "--output", str(output)],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid benchmark evidence", result.stderr)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
