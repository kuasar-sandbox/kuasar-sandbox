"""Exercise real delayed exec transitions using only this test's child processes."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


def function(script, name):
    return re.search(r"(?ms)^" + name + r"\(\) \{.*?^\}", script).group()


class ProcessStartup(unittest.TestCase):
    def run_start(self, kind, exit_before_exec=False):
        with tempfile.TemporaryDirectory(prefix="kuasar-demo-exec-") as temporary:
            root = Path(temporary)
            wrapper = root / "setsid"
            transition = "sys.exit(91)\n" if exit_before_exec else (
                "os.execv(" + repr(shutil.which("setsid")) + ", ['setsid', *sys.argv[1:]])\n")
            wrapper.write_text("#!/usr/bin/python3\nimport os, sys, time\ntime.sleep(0.8)\n" + transition)
            wrapper.chmod(0o755)
            script = (ROOT / ("demo_prep.sh" if kind == "prep" else "demo_e2b.sh")).read_text()
            names = ("proc_start_time", "record_state", "start_owned") if kind == "prep" else (
                "proc_start_time", "start_process")
            commands = '\n'.join(function(script, name) for name in names)
            command = '''set -euo pipefail
PID_DIR=$1
LOG_DIR=$1
declare -a STARTED_NOW=() PROCESS_NAMES=() PROCESS_PIDS=() PROCESS_STARTS=() PROCESS_EXES=()
say() { :; }
ok() { :; }
demo_die() { echo "$*" >&2; exit 1; }
die() { demo_die "$@"; }
capture_unit_files() { :; }
cleanup() {
  for child in $(jobs -pr); do kill "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; done
}
trap cleanup EXIT
''' + commands
            if kind == "prep":
                command += '\nstart_owned fixture "$2" 20\nrecord_state fixture\n'
                command += '[ "$REC_EXE" = "$(readlink -f "$2")" ]\n'
                command += '[ "$(readlink -f /proc/$REC_PID/exe)" = "$REC_EXE" ]\n'
            else:
                command += '\nstart_process fixture "$1/service.log" "$2" 20\n'
                command += '[ "$(readlink -f /proc/$STARTED_PID/exe)" = "${PROCESS_EXES[0]}" ]\n'
            environment = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"])
            return subprocess.run(["bash", "-c", command, "_", str(root), shutil.which("sleep")],
                                  env=environment, text=True, capture_output=True, timeout=15)

    def test_persistent_service_waits_for_its_actual_executable(self):
        result = self.run_start("prep")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_per_run_service_waits_for_its_actual_executable(self):
        result = self.run_start("run")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_partial_launch_failure_is_not_declared_started(self):
        for kind in ("prep", "run"):
            result = self.run_start(kind, exit_before_exec=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exited", result.stderr)

    def test_preexisting_foreign_pid_record_is_still_refused_without_signalling(self):
        with tempfile.TemporaryDirectory(prefix="kuasar-demo-foreign-pid-") as temporary:
            root = Path(temporary)
            child = subprocess.Popen([shutil.which("sleep"), "20"])
            try:
                start = Path("/proc/" + str(child.pid) + "/stat").read_text().rsplit(") ", 1)[1].split()[19]
                record = str(child.pid) + "\t" + start + "\t" + str(Path(shutil.which("false")).resolve()) + "\n"
                (root / "fixture.pid").write_text(record)
                source = (ROOT / "demo_prep.sh").read_text()
                functions = "\n".join(function(source, name) for name in ("proc_start_time", "record_state", "start_owned"))
                script = 'set -euo pipefail\nPID_DIR=$1\nLOG_DIR=$1\ndemo_die() { echo "$*" >&2; exit 1; }\n'
                script += functions + '\nstart_owned fixture "$2" 20\n'
                result = subprocess.run(["bash", "-c", script, "_", str(root), shutil.which("sleep")],
                                        text=True, capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("refusing to replace or kill", result.stderr)
                self.assertIsNone(child.poll())
                self.assertEqual((root / "fixture.pid").read_text(), record)
            finally:
                child.terminate()
                child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
