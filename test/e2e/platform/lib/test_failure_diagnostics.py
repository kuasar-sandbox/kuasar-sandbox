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

    def run_case(self, body, *, case="read-recovery", cleanup="", blocked_output=False):
        name = {"read-recovery": "e2e_sandbox_read_recovery.sh", "registry-n3": "e2e_cluster_stub.sh",
                "unrelated": "e2e_unrelated.sh"}[case]
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
        self.assertEqual((report["case"], report["phase"], report["exit_code"]), ("read-recovery", "seed-snapshot", 17))
        self.assertEqual(report["source"], "e2e_sandbox_read_recovery.sh")
        self.assertEqual(report["line"], lines.index("SNAP=$(exit 17)") + 1)
        self.assertEqual(report["logs"]["seed.snapshot.log"]["excerpts"][0]["error_terms"], ["context deadline exceeded"])
        self.assertNotIn(secret, json.dumps(reports) + result.stdout + result.stderr)

    def test_explicit_cluster_fail_retains_call_line_phase_and_http_status(self):
        (self.work / "build-status.body").write_text('{"error":"Bad Gateway","api_secret":"raw-secret"}')
        body = 'step "checking build follow-up forwarding through the node API endpoint"\ncode=502\n[ "$code" = 200 ] || fail "private argument"\n'
        result, reports, lines = self.run_case(body, case="registry-n3")
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertEqual(self.cleanup.read_text(), "19\n")
        report = reports[0]
        self.assertEqual((report["phase"], report["http_status"]), ("build-status", 502))
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

    def test_existing_privileged_reexec_keeps_hook_when_sudo_filters_bash_env(self):
        sudo = self.root / "sudo"
        sudo.write_text('#!/bin/bash\nset -eu\n[ "$1" = -nE ]; shift\nunset BASH_ENV\nexec "$@"\n')
        sudo.chmod(0o755)
        body = '''if [ -z "${DIAGNOSTIC_REEXECED:-}" ]; then
    export DIAGNOSTIC_REEXECED=1
    exec sudo -nE bash "$0" "$@"
fi
SNAP=$(exit 17)
'''
        result, reports, lines = self.run_case(body)
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(self.cleanup.read_text(), "17\n")
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["line"], lines.index("SNAP=$(exit 17)") + 1)

    def test_exec_still_closes_readiness_descriptors(self):
        result, reports, _ = self.run_case('''exec {fd}>"$WORK/readiness"
saved_fd=$fd
exec {fd}>&-
[ ! -e "/proc/$$/fd/$saved_fd" ]
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(reports, [])

    def test_missing_work_still_records_failure_identity(self):
        path = diagnostics.collect("read-recovery", "setup", "case.sh", "12", "17",
                                   str(self.work / "missing"), str(self.metrics), "")
        report = json.loads(path.read_text())
        self.assertEqual(report["exit_code"], 17)
        self.assertTrue(report["logs_unavailable"])

    def test_trusted_source_entry_enables_hook_only_in_source_ci_layout(self):
        # The current trusted workflow lacks BASH_ENV. Its assembled source
        # entry must enable the hook without extending release/artifact E2E.
        for layout, ci, active in (("build/e2e-suite", True, True),
                                   ("release", True, False),
                                   ("prepared/x86_64", True, False),
                                   ("developer/build/e2e-suite", False, False)):
            with self.subTest(layout=layout, ci=ci):
                suite = self.root / layout / "test/e2e"
                metrics = self.root / layout / "metrics"
                (suite / "sandboxer").mkdir(parents=True)
                (suite / "platform/lib").mkdir(parents=True)
                shutil.copyfile(ROOT.parents[1] / "run_all.sh", suite / "run_all.sh")
                for name in ("failure-diagnostics.sh", "failure_diagnostics.py"):
                    shutil.copyfile(ROOT / name, suite / "platform/lib" / name)
                runner = suite / "sandboxer/run_all.sh"
                runner.write_text('#!/bin/bash\nbash "$(dirname "$0")/e2e_sandbox_read_recovery.sh"\n')
                runner.chmod(0o755)
                case = suite / "sandboxer/e2e_sandbox_read_recovery.sh"
                case.write_text('set -eu\ncleanup() { rm -rf "$WORK"; }\ntrap cleanup EXIT\nSNAP=$(exit 17)\n')
                environment = {**os.environ, "KUASAR_E2E_SHARD": "sandboxer",
                               "WORK": str(self.work), "ZOT_BIN": "/unused/zot", "VGW_BIN": "/unused/gateway"}
                environment.pop("BASH_ENV", None)
                environment.pop("KUASAR_CI_DIR", None)
                if ci:
                    environment["KUASAR_CI_DIR"] = str(metrics)
                result = subprocess.run(["bash", str(suite / "run_all.sh")], env=environment,
                                        text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 17, result.stderr)
                reports = [json.loads(p.read_text()) for p in metrics.glob("e2e-failures/*.json")]
                self.assertEqual(len(reports), int(active))
                if active:
                    self.assertEqual((reports[0]["line"], reports[0]["exit_code"]), (4, 17))
                else:
                    self.assertNotIn("E2E failure:", result.stderr)

    def test_collector_is_bounded_and_never_copies_raw_text_or_symlinks(self):
        secret = "unknown-credential-format-should-also-be-withheld"
        (self.work / "cache.log").write_text(secret * 3000 + "\ncontext canceled secret=" + secret)
        (self.root / "outside").write_text("permission denied " + secret)
        (self.work / "seed.log").symlink_to(self.root / "outside")
        os.mkfifo(self.work / "proxy.log")
        path = diagnostics.collect("read-recovery", "setup", "case.sh", "12", "17", str(self.work), str(self.metrics), "")
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
