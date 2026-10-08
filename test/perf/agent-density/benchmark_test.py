import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("agent_density_benchmark", Path(__file__).with_name("benchmark.py"))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

class BenchmarkTests(unittest.TestCase):
    def test_matched_and_mismatched(self):
        with tempfile.TemporaryDirectory() as td:
            dirs = [Path(td)/n for n in ("fixed", "density")]
            for d, peak in zip(dirs, (3, 16)):
                d.mkdir()
                tasks = [{"task": i, "status": "success", "peak_mib": 192,
                          "result": {"action_count": 4, "completed_tools": 12, "artifact_sha256": str(i)}} for i in range(3)]
                w = {"offered": 3, "accepted": 3, "succeeded": 3, "tasks": tasks,
                     "cleanup": {"passed": True}, "warmup_seconds": 2, "task_wall_seconds": 9,
                     "peak_running": peak, "peak_parent_bytes": 1000000, "peak_q": 0,
                     "data_requests": 4, "data_requests_success": 4,
                     "parent_start": {"memory_events": {"oom_kill": 0}},
                     "parent_end": {"memory_events": {"oom_kill": 0}}}
                (d/"summary.json").write_text(json.dumps({"results": [w]}))
                (d/"process-exit.json").write_text(json.dumps({"driver_exit": 0, "observer_exit": 0}))
            self.assertEqual(b.compare(*dirs)["correct_completion"], 3)
            s = json.loads((dirs[1]/"summary.json").read_text())
            s["results"][0]["tasks"][0]["result"]["artifact_sha256"] = "bad"
            (dirs[1]/"summary.json").write_text(json.dumps(s))
            with self.assertRaisesRegex(ValueError, "mismatch"):
                b.compare(*dirs)

if __name__ == "__main__":
    unittest.main()
