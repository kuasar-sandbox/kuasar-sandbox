#!/usr/bin/env python3
"""Offline behavioral tests with real local HTTP, worker processes and deadlines."""
import contextlib
import hashlib
import http.server
import io
import json
import multiprocessing
import os
from pathlib import Path
import signal
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import zipfile
import download_artifact as d

RUN = '11111111-1111-1111-1111-111111111111'
JOB = '22222222-2222-2222-2222-222222222222'
BASE = ''
DIRECTORY_RECORD = None


def zip_bytes(name='payload.txt', content=b'complete verified content', mode=0):
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w') as z:
        member = zipfile.ZipInfo(name)
        member.external_attr = mode << 16
        z.writestr(member, content)
    return target.getvalue()


def local_operation(spec, frozen, directory, send):
    if DIRECTORY_RECORD:
        with open(DIRECTORY_RECORD, 'a') as log:
            log.write(str(directory) + '\n')
    identity = {'workflow_run_backend_id': RUN, 'workflow_job_run_backend_id': JOB}
    with mock.patch.object(d, 'runtime_identity', return_value=('test-token', BASE, identity)), \
         mock.patch.object(d, 'secure_url', side_effect=lambda value: value):
        d.attempt(spec, frozen, directory, send)


def write_until_stopped(spec, frozen, directory, send):
    marker = Path(spec['marker'])
    marker.with_suffix('.pid').write_text(str(os.getpid()))
    while True:
        with marker.open('a') as log:
            log.write('write\n')
        time.sleep(.01)


def certificate_failure(spec, frozen, directory, send):
    with open(spec['marker'], 'a') as stream:
        stream.write('attempt\n')
    raise urllib.error.URLError(ssl.SSLCertVerificationError('timeout'))


def cancel_supervisor(spec, destination):
    signal.signal(signal.SIGTERM, lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        d.download(spec, destination, seconds=5, delays=(0, 0), operation=write_until_stopped)
    except KeyboardInterrupt:
        return


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def handle_error(self, request, address):
        pass  # Expected reset/broken pipe after supervised cancellation.


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_body(self, body, status=200):
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        state = self.server.state
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path.endswith('/ListArtifacts'):
            state['lists'] += 1
            if state['mode'] == 'metadata_stall' and state['lists'] == 1:
                time.sleep(.6)
            body = self.server.archive
            artifact_id = '2' if state['mode'] == 'replacement' and state['lists'] > 1 else '1'
            digest = '0' * 64 if state['mode'] == 'digest' else hashlib.sha256(body).hexdigest()
            row = {'workflow_run_backend_id': RUN, 'workflow_job_run_backend_id': JOB,
                   'database_id': artifact_id, 'name': 'wanted', 'size': str(len(body)),
                   'digest': 'sha256:' + digest}
            self.send_body(json.dumps({'artifacts': [row]}).encode())
        elif self.path.endswith('/GetSignedArtifactURL'):
            state['urls'] += 1
            self.send_body(json.dumps({'signed_url': BASE + '/blob/' + str(state['urls']) +
                                      '?sig=never-log-this'}).encode())
        else:
            self.send_body(b'unknown', 404)

    def do_GET(self):
        state = self.server.state
        state['requests'].append(self.path)
        state['blob_authorizations'].append(self.headers.get('Authorization'))
        mode, number = state['mode'], len(state['requests'])
        if mode == 'auth':
            self.send_body(b'not authorized: timeout is only text', 403)
            return
        if (mode in ('reset', 'replacement') and number == 1) or mode == 'reset_always':
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if mode == 'headers_stall' and number == 1:
            time.sleep(.6)
        if mode == 'headers_always':
            time.sleep(.6)
        body = self.server.archive
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if mode == 'truncate' and number == 1:
            self.wfile.write(body[:len(body) // 2])
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
        elif mode == 'drip' and number == 1:
            for value in body:
                self.wfile.write(bytes([value]))
                self.wfile.flush()
                time.sleep(.02)
        else:
            self.wfile.write(body)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        global BASE, DIRECTORY_RECORD
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        DIRECTORY_RECORD = self.root / 'directories'
        self.server = Server(('127.0.0.1', 0), Handler)
        self.server.archive = zip_bytes()
        self.server.state = {'mode': 'ok', 'lists': 0, 'urls': 0, 'requests': [],
                             'blob_authorizations': []}
        BASE = 'http://127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.delays = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def run_download(self, mode, seconds=.18):
        self.server.state['mode'] = mode
        with contextlib.redirect_stdout(io.StringIO()) as output:
            d.download({'name': 'wanted', 'pattern': ''}, self.root / 'out',
                       seconds=seconds, operation=local_operation,
                       sleep=self.delays.append)
        self.assertNotIn('never-log-this', output.getvalue())
        self.assertNotIn('test-token', output.getvalue())
        self.assertFalse(list(self.root.glob('.kuasar-artifact-*')))

    def test_success_is_verified_and_published_once(self):
        self.run_download('ok')
        self.assertEqual((self.root / 'out/payload.txt').read_bytes(), b'complete verified content')
        self.assertEqual(self.server.state['blob_authorizations'], [None])
        self.assertEqual(self.delays, [])

    def test_headers_and_metadata_stall_and_drip_have_hard_deadline(self):
        for mode in ('metadata_stall', 'headers_stall', 'drip'):
            with self.subTest(mode=mode):
                if (self.root / 'out').exists():
                    import shutil
                    shutil.rmtree(self.root / 'out')
                self.server.state.update(mode=mode, lists=0, urls=0, requests=[])
                self.delays.clear()
                started = time.monotonic()
                self.run_download(mode)
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(self.delays, [15])

    def test_reset_and_truncation_retry_new_url_and_directory(self):
        for mode in ('reset', 'truncate'):
            with self.subTest(mode=mode):
                if (self.root / 'out').exists():
                    import shutil
                    shutil.rmtree(self.root / 'out')
                self.server.state.update(lists=0, urls=0, requests=[])
                self.delays.clear()
                self.run_download(mode)
                self.assertEqual(self.delays, [15])
                self.assertEqual(len(set(self.server.state['requests'])), 2)
                directories = (self.root / 'directories').read_text().splitlines()
                self.assertEqual(len(directories), len(set(directories)))
                self.assertTrue(all(not Path(p).exists() for p in directories))

    def test_three_attempts_then_block_without_destination(self):
        with self.assertRaisesRegex(d.Failure, 'attempt_deadline'):
            self.run_download('headers_always')
        self.assertEqual(self.delays, [15, 30])
        self.assertEqual(self.server.state['urls'], 3)
        self.assertFalse((self.root / 'out').exists())
        self.assertFalse(list(self.root.glob('.kuasar-artifact-*')))

    def test_auth_and_digest_are_fatal_once(self):
        for mode, reason in [('auth', 'http_403'), ('digest', 'digest_mismatch')]:
            with self.subTest(mode=mode):
                self.server.state.update(lists=0, urls=0, requests=[])
                with self.assertRaisesRegex(d.Failure, reason):
                    self.run_download(mode)
                self.assertEqual(self.server.state['urls'], 1)
                self.assertEqual(self.delays, [])
                self.assertFalse((self.root / 'out').exists())

    def test_same_name_replacement_is_not_selected_on_retry(self):
        with self.assertRaisesRegex(d.Failure, 'artifact_identity_changed'):
            self.run_download('replacement')
        self.assertEqual(self.server.state['urls'], 1)
        self.assertFalse((self.root / 'out').exists())

    def test_verified_invalid_zip_is_fatal(self):
        self.server.archive = b'not a zip but has a correct digest'
        with self.assertRaisesRegex(d.Failure, 'invalid_archive'):
            self.run_download('ok')
        self.assertEqual(self.server.state['urls'], 1)
        self.assertFalse((self.root / 'out').exists())

    def test_verified_zip_cannot_escape_or_create_symlinks(self):
        import stat
        for name, mode in [('../escape', 0), ('/escape', 0), ('a\\b', 0),
                           ('a//b', 0), ('a/../b', 0), ('a:b', 0),
                           ('link', stat.S_IFLNK | 0o777),
                           ('device', stat.S_IFCHR | 0o600)]:
            with self.subTest(name=name):
                self.server.archive = zip_bytes(name, b'outside', mode)
                with self.assertRaisesRegex(d.Failure, 'unsafe_archive'):
                    self.run_download('ok')
                self.assertFalse((self.root / 'out').exists())
        self.assertEqual(self.delays, [])

    def test_cancel_reaps_worker_before_cleanup_without_retry(self):
        marker = self.root / 'writes'
        spec = {'name': 'wanted', 'pattern': '', 'marker': str(marker)}
        proc = multiprocessing.get_context('fork').Process(
            target=cancel_supervisor, args=(spec, self.root / 'out'))
        proc.start()
        try:
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists())
            os.kill(proc.pid, signal.SIGTERM)
            proc.join(3)
            self.assertFalse(proc.is_alive())
            size = marker.stat().st_size
            time.sleep(.1)
            self.assertEqual(marker.stat().st_size, size)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(marker.with_suffix('.pid').read_text()), 0)
            self.assertFalse((self.root / 'out').exists())
            self.assertFalse(list(self.root.glob('.kuasar-artifact-*')))
        finally:
            if proc.is_alive():
                proc.kill()
                proc.join()

    def test_certificate_and_unknown_errors_are_not_transient(self):
        for exc in [ssl.SSLCertVerificationError('timeout'),
                    urllib.error.URLError(ssl.SSLCertVerificationError('timeout')),
                    RuntimeError('connection reset timeout'), OSError('timeout')]:
            self.assertFalse(d.classify(exc).retryable)
        for status in (400, 401, 403, 404, 422):
            error = urllib.error.HTTPError('https://invalid', status, 'timeout', {}, None)
            self.assertFalse(d.classify(error).retryable)

    def test_metadata_missing_digest_and_cross_run_fail_closed(self):
        row = {'workflow_run_backend_id': RUN, 'workflow_job_run_backend_id': JOB,
               'database_id': '1', 'name': 'wanted', 'size': '100'}
        with self.assertRaisesRegex(d.Failure, 'missing_digest'):
            d.normalize(row, RUN)
        row['digest'] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(d.Failure, 'artifact_run_mismatch'):
            d.normalize(row, JOB)
        row['workflow_run_backend_id'] = RUN
        selected = d.select_artifacts({'artifacts': [row]},
                                     {'name': '', 'pattern': 'want*'}, RUN)
        self.assertEqual(selected[0]['database_id'], '1')
        with self.assertRaisesRegex(d.Failure, 'ambiguous_artifact_identity'):
            d.select_artifacts({'artifacts': [row, row]},
                               {'name': 'wanted', 'pattern': ''}, RUN)

    def test_duplicate_and_cross_archive_paths_are_rejected(self):
        archive = self.root / 'duplicate.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('parent', 'file')
            z.writestr('parent/child', 'must not replace parent')
        output = self.root / 'staging'
        output.mkdir()
        with self.assertRaises((OSError, d.Failure)):
            d.extract_archive(archive, output)
        archive.write_bytes(zip_bytes('existing', b'new'))
        (output / 'existing').write_bytes(b'old')
        with self.assertRaisesRegex(d.Failure, 'artifact_path_collision'):
            d.extract_archive(archive, output)
        self.assertEqual((output / 'existing').read_bytes(), b'old')

    def test_workflow_timeout_sparse_checkout_and_runtime_boundary(self):
        root = Path(__file__).resolve().parents[2]
        workflow = (root / '.github/workflows/integration-architecture.yml').read_text()
        # Only the small, hash-bound executor bootstrap uses the pinned official
        # downloader before the local bounded action is available.
        self.assertEqual(workflow.count('uses: actions/download-artifact@'), 1)
        e2e = workflow.split('\n  e2e:\n', 1)[1].split('\n  performance:\n', 1)[0]
        bootstrap = e2e.split('      - name: Download the exact executor capsule\n', 1)[1]
        bootstrap = bootstrap.split('\n      - ', 1)[0]
        self.assertIn("if: runner.environment == 'self-hosted'", bootstrap)
        self.assertIn('uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c', bootstrap)
        self.assertIn('timeout-minutes: 5', bootstrap)
        self.assertIn('name: ${{ inputs.executor_artifact }}', bootstrap)
        self.assertIn('path: ${{ env.E2E_WORKSPACE }}/executor', bootstrap)
        restore = e2e.split('      - name: Verify and restore exact executor Git objects\n', 1)[1]
        restore = restore.split('\n      - ', 1)[0]
        self.assertIn('EXECUTOR_DIGEST: ${{ inputs.executor_digest }}', restore)
        self.assertIn('[ "$FRAMEWORK_SHA" = "$TRUSTED_WORKFLOW_SHA" ]', restore)
        self.assertLess(restore.index('hashlib.sha256'), restore.index('tarfile.open'))
        self.assertEqual(workflow.count('uses: ./framework/.github/actions/download-artifact'), 13)
        self.assertEqual(workflow.count('uses: ./framework/.github/actions/download-artifact'),
                         workflow.count('timeout-minutes: 35'))
        self.assertEqual(workflow.count('sparse-checkout: |'),
                         workflow.count('/.github/actions/download-artifact/'))
        action = (root / '.github/actions/download-artifact/action.yml').read_text()
        self.assertIn('using: node24', action)
        self.assertNotIn('github-token', action)
        entry = (root / '.github/actions/download-artifact/index.js').read_text()
        self.assertNotIn('_actions/', entry)
        self.assertNotIn('npm', entry)
        self.assertIn("child.kill(signal)", entry)
        self.assertEqual(d.ATTEMPT_SECONDS, 600)
        self.assertEqual(d.BACKOFF_SECONDS, (15, 30))

    def test_certificate_failure_runs_once_and_leaves_no_target(self):
        marker = self.root / 'tls-attempts'
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(d.Failure, 'tls_error'):
                d.download({'name': 'wanted', 'pattern': '', 'marker': str(marker)},
                           self.root / 'out', seconds=.18, delays=(15, 30),
                           operation=certificate_failure, sleep=self.delays.append)
        self.assertEqual(marker.read_text().splitlines(), ['attempt'])
        self.assertEqual(self.delays, [])
        self.assertFalse((self.root / 'out').exists())
        self.assertFalse(list(self.root.glob('.kuasar-artifact-*')))

    def test_deadline_stops_writes_before_removing_staging(self):
        marker = self.root / 'writes'
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(d.Failure, 'attempt_deadline'):
                d.download({'name': 'wanted', 'pattern': '', 'marker': str(marker)},
                           self.root / 'out', seconds=.08, delays=(),
                           operation=write_until_stopped)
        size = marker.stat().st_size
        time.sleep(.1)
        self.assertEqual(marker.stat().st_size, size)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.with_suffix('.pid').read_text()), 0)
        self.assertFalse((self.root / 'out').exists())
        self.assertFalse(list(self.root.glob('.kuasar-artifact-*')))

    def test_insufficient_space_fails_before_download(self):
        row = {'size': 100, 'digest': '0' * 64}
        with mock.patch.object(d.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 0})()), \
             mock.patch.object(d, 'open_url') as network:
            with self.assertRaisesRegex(d.Failure, 'insufficient_disk_space'):
                d.transfer(row, 'https://invalid', self.root / 'unused.zip')
        network.assert_not_called()
        self.assertFalse((self.root / 'unused.zip').exists())

    def test_worker_does_not_publish_group_writable_files(self):
        import stat
        previous = os.umask(0o002)
        try:
            self.run_download('ok')
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE((self.root / 'out/payload.txt').stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((self.root / 'out').stat().st_mode), 0o755)

    def test_existing_destination_is_untouched(self):
        (self.root / 'out').mkdir()
        (self.root / 'out/keep').write_text('original')
        with self.assertRaisesRegex(d.Failure, 'destination_must_be_new'):
            self.run_download('ok')
        self.assertEqual((self.root / 'out/keep').read_text(), 'original')
        self.assertEqual(self.server.state['lists'], 0)


if __name__ == '__main__':
    unittest.main()
