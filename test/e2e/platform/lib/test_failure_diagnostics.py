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
from unittest.mock import patch
from io import StringIO

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
        # Never query a real host daemon from synthetic case tests.
        for name in ("systemctl", "journalctl"):
            tool = self.root / name
            tool.write_text("#!/bin/sh\nexit 0\n")
            tool.chmod(0o700)

    def run_case(self, body, *, case="snapshot.read-recovery.sh", cleanup="", blocked_output=False):
        name = "basic.unrelated.sh" if case == "unrelated" else case
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
        for case in diagnostics.FILES:
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

    def test_cgroup_bare_exec_preserves_dynamic_locals_and_exact_failure_line(self):
        tool = self.root / "sandbox-ctl"
        tool.write_text('#!/bin/sh\nprintf "connection reset by peer private-credential\\n"\nexit 37\n')
        tool.chmod(0o700)
        body = '''OUT=$WORK
BIN=${BASH_SOURCE[0]%/*}
run_case() {
    local run_pid=$$ name=false-shared
    "$BIN/sandbox-ctl" exec --token private-credential >"$OUT/$name-exec.log" 2>&1
}
run_case false shared
'''
        result, reports, lines = self.run_case(body, case="sandbox.cgroup.sh")
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertEqual(self.cleanup.read_text(), "37\n")
        report = reports[0]
        self.assertEqual(report["command"], "sandbox-ctl-exec")
        self.assertEqual(report["phase"], "cgroup-probe")
        self.assertEqual(report["line"], next(i + 1 for i, line in enumerate(lines) if '--token' in line))
        self.assertGreater(report["process"]["pid"], 1)
        self.assertEqual(report["logs"]["false-shared-exec.log"]["excerpts"][0]["error_terms"], ["connection reset by peer"])
        self.assertNotIn("private-credential", json.dumps(reports) + result.stderr)

    def test_resource_explicit_fail_retains_second_iteration_unit_before_cleanup(self):
        body = '''run_low_allocatable_case() {
    local LOW_UNIT=sandbox-runner-test@second.service LOW_CTL_PID=$$
    local LOW_CG=/system.slice/sandbox-runner-test@second.service
    printf '[sandbox-ctl] received terminated\\n' >"$WORK/low-allocatable-2.journal"
    fail 'private secret'
}
run_low_allocatable_case 2
'''
        result, reports, _ = self.run_case(body, case="orchestrator.resource-startup.sh")
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertEqual(self.cleanup.read_text(), "19\n")
        report = reports[0]
        self.assertEqual(report["phase"], "low-allocatable")
        self.assertEqual(report["service"]["unit"], "sandbox-runner-test@second.service")
        self.assertGreater(report["process"]["pid"], 1)
        self.assertIn("low-allocatable-2.journal", report["logs"])

    def test_proxy_custom_exit_handler_keeps_original_failure_and_cleanup(self):
        body = '''lifecycle_cleanup() {
    local status=$?
    trap - EXIT
    cleanup || status=1
    exit "$status"
}
trap lifecycle_cleanup EXIT
service=kuasar-proxy-fixture.service
printf '{"SERVICE_RESULT":"success","EXIT_CODE":"killed","EXIT_STATUS":"TERM","INVOCATION_ID":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}' >"$WORK/service-exit-builtin-bothfd.log"
exit 23
'''
        result, reports, _ = self.run_case(body, case="orchestrator.proxy-inplace-restart.sh")
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertFalse(self.work.exists())
        self.assertEqual(reports[0]["service"]["unit"], "kuasar-proxy-fixture.service")
        stop = reports[0]["logs"]["service-exit-builtin-bothfd.log"]["excerpts"][0]
        self.assertEqual(stop["EXIT_STATUS"], "TERM")
        self.metrics = self.root / "success-metrics"
        result, reports, _ = self.run_case('''lifecycle_cleanup() { local status=$?; trap - EXIT; cleanup || status=1; exit "$status"; }
trap lifecycle_cleanup EXIT
exit 0
''', case="orchestrator.proxy-inplace-restart.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(reports, [])

    def test_unit_terminal_query_is_bounded_read_only_and_redacts_journal(self):
        def execute(command, **kwargs):
            self.assertEqual(kwargs["timeout"], 5)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            if command[0] == "systemctl":
                self.assertEqual(command[:3], ["systemctl", "show", "sandbox-runner-test@second.service"])
                text = 'ActiveState=deactivating\nMainPID=0\nEnvironment=secret\nExecMainStatus=9\n'
            else:
                self.assertIn('--unit=sandbox-runner-test@second.service', command)
                self.assertIn('--lines=200', command)
                self.assertNotIn('--all', command)
                text = json.dumps({"MESSAGE": "[sandbox-ctl] CH exited code=-1 private-secret SIGKILL",
                                   "_PID": "123", "__REALTIME_TIMESTAMP": "123456789", "_ENVIRON": "secret"}) + '\n'
            kwargs["stdout"].write(text.encode())
            return subprocess.CompletedProcess(command, 0)
        with patch.object(diagnostics.subprocess, "run", side_effect=execute):
            result = diagnostics.unit_state("sandbox-runner-test@second.service")
        self.assertEqual(result["properties"], {"ActiveState": "deactivating", "MainPID": "0", "ExecMainStatus": "9"})
        event = result["journal"]["events"][0]
        self.assertEqual(event["excerpts"][0]["ch_exit_code"], -1)
        self.assertIn("SIGKILL", event["excerpts"][0]["error_terms"])
        self.assertEqual(event["__REALTIME_TIMESTAMP"], "123456789")
        self.assertNotIn("secret", json.dumps(result))

    def test_failed_reads_are_reported_without_retry_or_exception_contents(self):
        with patch.object(diagnostics.subprocess, "run", side_effect=subprocess.TimeoutExpired("secret", 5)) as run:
            result = diagnostics.unit_state("kuasar-proxy-fixture.service")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["show"], {"error": "TimeoutExpired"})
        self.assertEqual(result["journal"]["error"], "TimeoutExpired")
        self.assertNotIn("secret", json.dumps(result))
        with patch.object(diagnostics.subprocess, "run") as run:
            self.assertEqual(diagnostics.unit_state("--all"), {"unavailable": True})
            self.assertEqual(diagnostics.unit_state("unrelated.service"), {"unavailable": True})
            self.assertEqual(diagnostics.cgroup_state("/../../etc"), {"unavailable": True})
            self.assertEqual(diagnostics.process_state("/environ"), {})
        run.assert_not_called()

    def test_terminal_parser_never_copies_unknown_structured_values(self):
        text = '\n'.join([
            json.dumps({"kind": "exec_stop_term_pending", "forced": True, "main_pid": 23, "candidate": {"secret": "private"}}),
            json.dumps({"SERVICE_RESULT": ["bad"], "INVOCATION_ID": "private", "EXIT_STATUS": "secret"}),
            json.dumps({"kind": ["bad"], "SERVICE_RESULT": {"secret": "private"}}),
            'CG-exec-GEN-1-FORK-OK 128 private',
        ])
        result = diagnostics.excerpts(text)
        self.assertEqual(result[0]["kind"], "exec_stop_term_pending")
        self.assertEqual(result[0]["main_pid"], 23)
        self.assertEqual(result[1]["cgroup_markers"], ["CG-exec-GEN-1-FORK-OK 128"])
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))

    def test_cgroup_template_slice_is_read_only_and_bounded(self):
        group = r'/system.slice/system-sandbox\x2drunner\x2dtest.slice/sandbox-runner-test@second.service'
        paths = []
        def opened(path):
            paths.append(str(path))
            return StringIO(' '.join(str(pid) for pid in range(100, 120)))
        with patch.object(Path, 'open', opened):
            result = diagnostics.cgroup_state(group)
        self.assertEqual(paths, ['/sys/fs/cgroup' + group + child + '/cgroup.procs' for child in ('', '/ctl', '/vmm')])
        self.assertEqual(result['/vmm']['pids'], list(range(100, 116)))
        self.assertTrue(result['/vmm']['pids_truncated'])


if __name__ == "__main__":
    unittest.main()
