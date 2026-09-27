#!/usr/bin/env python3
"""Exercise the real Bash hook/collector with bounded synthetic case failures."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("diagnostics", ROOT / "failure_diagnostics.py")
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


class FailureDiagnostics(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "private"
        self.work.mkdir()
        self.metrics = self.root / "metrics"
        self.cleanup = self.root / "cleanup-status"

    def run_case(self, body, *, case="snapshot.read-recovery.sh", cleanup="", blocked_output=False):
        name = {"snapshot.read-recovery.sh": "snapshot.read-recovery.sh", "orchestrator.cluster-recovery.sh": "orchestrator.cluster-recovery.sh",
                "unrelated": "basic.unrelated.sh"}[case]
        path = self.root / name
        text = '''#!/usr/bin/env bash
set -euo pipefail
cleanup() {
    local result=$?
    printf '%s\\n' "$result" >> "$CLEANUP_STATUS"
    rm -rf "$WORK"
''' + cleanup + '''
}
trap cleanup EXIT
launch() { :; }
step() { :; }
run_redirect_flow() { :; }
fail() { exit 19; }
''' + body
        path.write_text(text)
        environment = {**os.environ, "BASH_ENV": str(ROOT / "failure-diagnostics.sh"),
                       "KUASAR_CI_DIR": str(self.metrics), "WORK": str(self.work),
                       "CLEANUP_STATUS": str(self.cleanup), "CLUSTER_STUB_CASE": case,
                       "PATH": str(self.root) + os.pathsep + os.environ["PATH"]}
        if blocked_output:
            self.metrics.write_text("not a directory")
        result = subprocess.run(["bash", str(path)], env=environment, text=True, capture_output=True, timeout=10)
        reports = [json.loads(p.read_text()) for p in self.metrics.glob("e2e-failures/*.json")]
        return result, reports, text.splitlines()

    def test_silent_snapshot_failure_is_collected_before_cleanup(self):
        secret = "never-publish-this-capability"
        (self.work / "seed.snapshot.log").write_text("rpc: context deadline exceeded token=" + secret + "\n")
        result, reports, lines = self.run_case('launch seed --config ignored\nSNAP=$(exit 17)\n')
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(self.cleanup.read_text(), "17\n")
        self.assertFalse(self.work.exists())
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual((report["case"], report["phase"], report["exit_code"]), ("snapshot.read-recovery.sh", "seed-snapshot", 17))
        self.assertEqual(report["source"], "snapshot.read-recovery.sh")
        self.assertEqual(report["line"], lines.index("SNAP=$(exit 17)") + 1)
        self.assertEqual(report["logs"]["seed.snapshot.log"]["excerpts"][0]["error_terms"], ["context deadline exceeded"])
        self.assertIn('"error_terms": ["context deadline exceeded"]', result.stderr)
        self.assertNotIn(secret, json.dumps(reports) + result.stdout + result.stderr)

    def test_explicit_cluster_fail_retains_call_line_phase_and_http_status(self):
        (self.work / "create.response").write_text('{"error":"Bad Gateway","api_secret":"raw-secret"}')
        body = 'run_redirect_flow\ncode=502\n[ "$code" = 200 ] || fail "private argument"\n'
        result, reports, lines = self.run_case(body, case="orchestrator.cluster-recovery.sh")
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertEqual(self.cleanup.read_text(), "19\n")
        report = reports[0]
        self.assertEqual((report["phase"], report["http_status"]), ("registry-recovery", 502))
        self.assertEqual(report["line"], lines.index('[ "$code" = 200 ] || fail "private argument"') + 1)
        self.assertNotIn("raw-secret", json.dumps(reports))
        self.assertNotIn("private argument", result.stderr)

    def test_cleanup_failure_cannot_replace_original_failure(self):
        result, reports, _ = self.run_case("exit 17\n", cleanup="    return 23\n")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(self.cleanup.read_text(), "17\n")
        self.assertEqual(reports[0]["exit_code"], 17)

    def test_success_and_failed_success_cleanup_keep_original_semantics(self):
        result, reports, _ = self.run_case(":\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(reports, [])
        self.assertEqual(self.cleanup.read_text(), "0\n")
        self.work.mkdir()
        result, reports, _ = self.run_case(":\n", cleanup="    return 23\n")
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(reports, [])

    def test_collector_failure_cannot_mask_test_failure_or_prevent_cleanup(self):
        result, reports, _ = self.run_case("exit 17\n", blocked_output=True)
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(self.cleanup.read_text(), "17\n")
        self.assertFalse(self.work.exists())
        self.assertEqual(reports, [])

    def test_unrelated_case_is_not_instrumented(self):
        result, reports, _ = self.run_case("exit 17\n", case="unrelated")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(reports, [])
        self.assertNotIn("E2E failure:", result.stderr)

    def test_exec_still_closes_readiness_descriptors(self):
        result, reports, _ = self.run_case('''exec {fd}>"$WORK/readiness"
saved_fd=$fd
exec {fd}>&-
[ ! -e "/proc/$$/fd/$saved_fd" ]
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(reports, [])

    def test_missing_work_still_records_failure_identity(self):
        path = diagnostics.collect("snapshot.read-recovery.sh", "setup", "case.sh", "12", "17",
                                   str(self.work / "missing"), str(self.metrics), "")
        report = json.loads(path.read_text())
        self.assertEqual(report["exit_code"], 17)
        self.assertTrue(report["logs_unavailable"])

    def test_public_runner_enables_only_prepared_recovery_hook(self):
        import importlib.machinery
        entry = ROOT.parents[1] / "e2e"
        spec = importlib.util.spec_from_loader("e2e_diagnostics", importlib.machinery.SourceFileLoader("e2e_diagnostics", str(entry)))
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        helper = self.root / "test/e2e/lib/platform/failure-diagnostics.sh"
        helper.parent.mkdir(parents=True)
        helper.write_bytes((ROOT / "failure-diagnostics.sh").read_bytes())
        for case in ("snapshot.read-recovery.sh", "orchestrator.cluster-recovery.sh"):
            env = runner.case_diagnostics(self.root, case, self.metrics)
            self.assertEqual(env["BASH_ENV"], str(helper))
            self.assertEqual(env["KUASAR_CI_DIR"], str(self.metrics))
        self.assertEqual(runner.case_diagnostics(self.root, "basic.other.sh", self.metrics), {})
        helper.unlink()
        with self.assertRaisesRegex(ValueError, "missing prepared platform"):
            runner.case_diagnostics(self.root, "snapshot.read-recovery.sh", self.metrics)

    def test_collector_is_bounded_and_never_copies_raw_text_or_symlinks(self):
        secret = "unknown-credential-format-should-also-be-withheld"
        (self.work / "cache.log").write_text(secret * 3000 + "\ncontext canceled secret=" + secret)
        (self.root / "outside").write_text("permission denied " + secret)
        (self.work / "seed.log").symlink_to(self.root / "outside")
        os.mkfifo(self.work / "proxy.log")
        path = diagnostics.collect("snapshot.read-recovery.sh", "setup", "case.sh", "12", "17", str(self.work), str(self.metrics), "")
        report = json.loads(path.read_text())
        self.assertNotIn(secret, path.read_text())
        self.assertNotIn("seed.log", report["logs"])
        self.assertNotIn("proxy.log", report["logs"])
        self.assertTrue(report["logs"]["cache.log"]["tail_truncated"])
        self.assertLess(path.stat().st_size, 4096)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(diagnostics.excerpts("Kernel command line: panic=1"), [])
        self.assertEqual(diagnostics.excerpts("panic: private-value")[0]["error_terms"], ["panic:"])


if __name__ == "__main__":
    unittest.main()
