"""Exercise the actual Demo Python assertions, not a MicroVM or SDK E2E."""
import contextlib
import io
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import re
import types
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("demo_e2b.sh").read_text()


class DemoStateAssertions(unittest.TestCase):
    def exercise(self, phase, *, listed=True, status=0, state=None, data=None, list_after_ready=False):
        label = "forked child exec failed" if phase == "fanout" else "connect-with-migration-token failed"
        match = re.search(r'(?m)^py <<PY \|\| die "' + label + r'"\n(.*?)^PY$', SCRIPT, re.S)
        self.assertIsNotNone(match)
        code = match.group(1).replace("$CHILD", "fixture-child").replace("$SID", "fixture-source")
        code = code.replace("$MIG_TOKEN", "fixture-migration")
        values = types.SimpleNamespace(killed=False, migration=False, ready=False)

        class Sandbox:
            @staticmethod
            def list():
                visible = listed and (values.ready or not list_after_ready)
                return types.SimpleNamespace(next_items=lambda: [types.SimpleNamespace(sandbox_id="fixture-child")] if visible else [])

            @staticmethod
            def connect(identifier, **options):
                expected = "fixture-child" if phase == "fanout" else "fixture-source"
                if identifier != expected:
                    raise AssertionError("wrong sandbox identity")
                if phase == "migration":
                    if options != {"headers": {"X-Kuasar-Migration-Token": "fixture-migration"}}:
                        raise AssertionError("migration token was not supplied")
                    values.migration = True
                def run(_):
                    values.ready = status == 0
                    return types.SimpleNamespace(exit_code=status, stderr="fixture",
                        stdout="hello from before the snapshot" if state is None else state)
                command = types.SimpleNamespace(run=run)
                files = types.SimpleNamespace(read=lambda _: "written through the E2B Files API" if data is None else data)
                return types.SimpleNamespace(commands=command, files=files, kill=lambda: setattr(values, "killed", True))

        with patch.dict("sys.modules", {"e2b": types.SimpleNamespace(Sandbox=Sandbox)}), contextlib.redirect_stdout(io.StringIO()):
            exec(compile(code, "demo-" + phase, "exec"), {})
        return values

    def test_complete_state_succeeds(self):
        self.assertTrue(self.exercise("fanout").killed)
        self.assertTrue(self.exercise("migration").migration)

    def test_missing_child_is_rejected(self):
        with self.assertRaises(AssertionError):
            self.exercise("fanout", listed=False)

    def test_starting_child_becomes_listed_only_after_guest_readiness(self):
        self.assertTrue(self.exercise("fanout", list_after_ready=True).killed)

    def test_both_phases_reject_failed_or_changed_data(self):
        for phase in ("fanout", "migration"):
            for change in ({"status": 1}, {"state": "lost state"}, {"data": "lost Files data"}):
                with self.subTest(phase=phase, change=change), self.assertRaises(AssertionError):
                    self.exercise(phase, **change)


class DemoMigrationCleanup(unittest.TestCase):
    sid = "fixture-source"
    api_key = "fixture-only-api-key"
    secret = "UNRELATED_PRIVATE_SENTINEL"

    def helper(self):
        match = re.search(r'(?m)^migration_source\(\) \{\n.*?<<\'PY\'\n(.*?)^PY\n\}', SCRIPT, re.S)
        self.assertIsNotNone(match)
        return compile(match[1], "demo-migration-source", "exec")

    def reply(self, state="deleting", status=200, sid=None, body=None, rc=0):
        if body is None:
            body = json.dumps({"sandboxID": self.sid if sid is None else sid, "state": state}).encode()
        return types.SimpleNamespace(returncode=rc, stdout=body + b"\n" + str(status).encode())

    def exercise(self, phase, replies, *, repeat=False, failure=None):
        elapsed = [0.0]
        calls = []
        def run(command, **kwargs):
            self.assertEqual(command[command.index("--unix-socket") + 1], "/fixture/control.sock")
            self.assertEqual(command[command.index("--header") + 1], "@-")
            self.assertEqual(command[-1], "http://localhost/sandboxes/" + self.sid)
            self.assertEqual(kwargs["input"], ("X-API-KEY: " + self.api_key + "\n").encode())
            self.assertNotIn(self.api_key, " ".join(command))
            self.assertGreater(kwargs["timeout"], 0)
            self.assertLessEqual(kwargs["timeout"], 2)
            self.assertEqual(float(command[command.index("--max-time") + 1]), kwargs["timeout"])
            calls.append(command)
            if failure is not None:
                raise failure
            index = min(len(calls) - 1, len(replies) - 1) if repeat else len(calls) - 1
            return replies[index]
        def sleep(seconds):
            self.assertGreaterEqual(seconds, 0)
            self.assertLessEqual(seconds, 0.1)
            elapsed[0] += seconds
        with patch.object(sys, "argv", ["-", "/fixture/control.sock", self.sid, phase]), \
             patch.dict(os.environ, {"E2B_API_KEY": self.api_key}), \
             patch("subprocess.run", side_effect=run), \
             patch("time.monotonic", side_effect=lambda: elapsed[0]), \
             patch("time.sleep", side_effect=sleep):
            try:
                exec(self.helper(), {})
            except SystemExit as error:
                self.assertNotIn(self.secret, str(error))
                return str(error), calls, elapsed[0]
        return None, calls, elapsed[0]

    def test_pre_export_requires_paused_source(self):
        error, calls, elapsed = self.exercise("paused", [self.reply("paused")])
        self.assertIsNone(error)
        self.assertEqual(len(calls), 1)
        self.assertEqual(elapsed, 0)

    def test_wait_only_for_exact_deleting_source_then_404(self):
        for replies in ([self.reply(status=404)], [self.reply(), self.reply(), self.reply(status=404)]):
            with self.subTest(requests=len(replies)):
                error, calls, _ = self.exercise("absent", replies)
                self.assertIsNone(error)
                self.assertEqual(len(calls), len(replies))

    def test_unexpected_status_identity_json_or_state_fails_immediately(self):
        for phase in ("paused", "absent"):
            cases = [self.reply(status=s) for s in (301, 401, 403, 409, 429, 500)]
            cases += [self.reply(sid="wrong"), self.reply(body=b"[]"), self.reply(body=b"{}"),
                      self.reply(body=self.secret.encode()), self.reply("running"),
                      self.reply("deleting" if phase == "paused" else "paused")]
            if phase == "paused":
                cases.append(self.reply(status=404))
            for reply in cases:
                with self.subTest(phase=phase, response=reply.stdout[-3:]):
                    error, calls, elapsed = self.exercise(phase, [reply])
                    self.assertIsNotNone(error)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(elapsed, 0)

    def test_transport_errors_and_bad_framing_fail_without_retry(self):
        for failure in (OSError(self.secret), subprocess.TimeoutExpired("curl", 2)):
            error, calls, _ = self.exercise("absent", [], failure=failure)
            self.assertEqual(error, "migration source transport failed")
            self.assertEqual(len(calls), 1)
        for reply in (self.reply(rc=7), types.SimpleNamespace(returncode=0, stdout=b"missing-status")):
            error, calls, _ = self.exercise("absent", [reply])
            self.assertIsNotNone(error)
            self.assertEqual(len(calls), 1)

    def test_deleting_has_a_single_fifteen_second_deadline(self):
        error, calls, elapsed = self.exercise("absent", [self.reply()], repeat=True)
        self.assertEqual(error, "migration source cleanup deadline exceeded")
        self.assertLessEqual(len(calls), 152)
        self.assertAlmostEqual(elapsed, 15.0)

    def test_real_curl_uses_authenticated_same_node_unix_socket(self):
        replies = [self.reply("paused"), self.reply(), self.reply(status=404)]
        observed = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                observed.append((self.path, self.headers.get("X-API-KEY")))
                payload, _, status = replies.pop(0).stdout.rpartition(b"\n")
                self.send_response(int(status))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        with tempfile.TemporaryDirectory(prefix="demo-get-") as directory:
            path = str(Path(directory) / "control.sock")
            with socketserver.UnixStreamServer(path, Handler) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    for phase in ("paused", "absent"):
                        with patch.object(sys, "argv", ["-", path, self.sid, phase]), \
                             patch.dict(os.environ, {"E2B_API_KEY": self.api_key}):
                            exec(self.helper(), {})
                finally:
                    server.shutdown()
                    thread.join(timeout=2)
        self.assertEqual(observed, [("/sandboxes/" + self.sid, self.api_key)] * 3)
        self.assertFalse(replies)

    def test_shell_observation_failure_stops_before_the_next_action(self):
        start = SCRIPT.index("migration_source() {")
        end = SCRIPT.index("\n}\n", start) + 3
        function = SCRIPT[start:end]
        for phase in ("paused", "absent"):
            shell = 'set -euo pipefail\npy() { return 23; }\n' + function
            shell += '\nmigration_source ' + phase + '\nprintf SHOULD_NOT_RUN\n'
            result = subprocess.run(["bash", "-c", shell],
                env=dict(os.environ, WORK="/fixture", SID=self.sid), capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 23)
            self.assertNotIn(b"SHOULD_NOT_RUN", result.stdout + result.stderr)

    def test_precondition_surrounds_export_and_sdk_errors_propagate_once(self):
        start = SCRIPT.index("migration_source paused")
        export = SCRIPT.index('MIG_TOKEN="$(', start)
        cleared = SCRIPT.index("migration_source absent", export)
        match = re.search(r'(?m)^py <<PY \|\| die "connect-with-migration-token failed"\n(.*?)^PY$', SCRIPT, re.S)
        self.assertIsNotNone(match)
        self.assertLess(cleared, match.start())
        code = match[1].replace("$SID", self.sid).replace("$MIG_TOKEN", "fixture-migration")
        for failure in (ValueError("invalid fixture token"), KeyError("code"), AssertionError("identity mismatch")):
            calls = []
            def connect(*args, **kwargs):
                calls.append((args, kwargs))
                raise failure
            with patch.dict("sys.modules", {"e2b": types.SimpleNamespace(Sandbox=types.SimpleNamespace(connect=connect))}):
                with self.assertRaises(type(failure)) as raised:
                    exec(compile(code, "demo-migration-connect", "exec"), {})
            self.assertIs(raised.exception, failure)
            self.assertEqual(calls, [((self.sid,), {"headers": {"X-Kuasar-Migration-Token": "fixture-migration"}})])


if __name__ == "__main__":
    unittest.main()
