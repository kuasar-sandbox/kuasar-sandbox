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
PRIOR_RUN = '33333333-3333-3333-3333-333333333333'
PRIOR_JOB = '44444444-4444-4444-4444-444444444444'
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


def cancel_http_supervisor(destination):
    signal.signal(signal.SIGTERM, lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        d.download({'name': 'wanted', 'pattern': ''}, destination,
                   seconds=5, delays=(0, 0), operation=local_operation)
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
        state['rpc_requests'].append((self.path.rsplit('/', 1)[-1], data,
                                      self.headers.get('Authorization')))
        if self.path.endswith('/ListArtifacts'):
            state['lists'] += 1
            if state['mode'] == 'list_auth':
                self.send_body(b'not authorized: timeout is only text', 403)
                return
            if state['mode'] == 'metadata_stall' and state['lists'] == 1:
                time.sleep(.6)
            body = self.server.archive
            artifact_id = '2' if state['mode'] == 'replacement' and state['lists'] > 1 else '1'
            digest = '0' * 64 if state['mode'] == 'digest' else hashlib.sha256(body).hexdigest()
            row = {'workflow_run_backend_id': RUN, 'workflow_job_run_backend_id': JOB,
                   'database_id': artifact_id, 'name': 'wanted', 'size': str(len(body)),
                   'digest': 'sha256:' + digest}
            row.update(state.get('artifact_overrides', {}))
            if state['mode'] in ('metadata_change', 'metadata_partial') and state['lists'] > 1:
                row.update(state['replacement_fields'])
            rows = [dict(row, **values) for values in state.get('extra_artifacts', [])] + [row]
            for item in rows:
                if item['name'] in state.get('archives', {}):
                    archive = state['archives'][item['name']]
                    item.update(size=str(len(archive)), digest=hashlib.sha256(archive).hexdigest())
            self.send_body(json.dumps({'artifacts': rows}).encode())
        elif self.path.endswith('/GetSignedArtifactURL'):
            state['urls'] += 1
            state['signed_names'][state['urls']] = data['name']
            self.send_body(json.dumps({'signed_url': BASE + '/blob/' + str(state['urls']) +
                                      '?sig=never-log-this'}).encode())
        else:
            self.send_body(b'unknown', 404)

    def do_GET(self):
        state = self.server.state
        state['requests'].append(self.path)
        state['blob_authorizations'].append(self.headers.get('Authorization'))
        state['ranges'].append(self.headers.get('Range'))
        mode, number = state['mode'], len(state['requests'])
        if (mode == 'throttle' and number == 1) or (mode == 'resume_429' and number == 2):
            self.send_response(429)
            self.send_header('Retry-After', state.get('retry_after', '41'))
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if mode == 'auth':
            self.send_body(b'not authorized: timeout is only text', 403)
            return
        if (mode in ('reset', 'replacement', 'metadata_change') and number == 1) or mode == 'reset_always':
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if mode == 'headers_stall' and number == 1:
            time.sleep(.6)
        if mode == 'headers_always':
            time.sleep(.6)
        name = state['signed_names'][int(self.path.split('/blob/')[1].split('?')[0])]
        body = state.get('archives', {}).get(name, self.server.archive)
        state.setdefault('download_names', []).append(name)
        range_modes = ('resume', 'resume_deadline', 'ignore_range', 'range_wrong_start',
                       'range_wrong_end', 'range_wrong_total', 'range_missing', 'length_wrong',
                       'range_416', 'resume_auth', 'resume_digest', 'resume_429', 'multi_resume',
                       'metadata_partial')
        first_partial = (mode in range_modes and number == 1 and mode != 'multi_resume' or
                         mode == 'multi_resume' and number == 2)
        if first_partial:
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            prefix = body[:len(body) // 2]
            if mode == 'resume_digest':
                prefix = bytes([prefix[0] ^ 1]) + prefix[1:]
            self.wfile.write(prefix)
            self.wfile.flush()
            if mode == 'resume_deadline':
                time.sleep(.6)
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        offset = int(self.headers['Range'].removeprefix('bytes=').removesuffix('-')) if self.headers.get('Range') else 0
        if mode in ('range_416', 'resume_auth') and offset:
            self.send_body(b'unusable signed URL', 416 if mode == 'range_416' else 403)
            return
        ranged = offset and mode in range_modes and mode != 'ignore_range'
        self.send_response(206 if ranged else 200)
        if ranged and mode != 'range_missing':
            start, end, total = offset, len(body) - 1, len(body)
            start += mode == 'range_wrong_start'
            end -= mode == 'range_wrong_end'
            total += mode == 'range_wrong_total'
            self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
        if ranged:
            body = body[offset:]
        self.send_header('Content-Length', str(len(body) + (mode == 'length_wrong')))
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
                             'blob_authorizations': [], 'rpc_requests': [],
                             'ranges': [], 'signed_names': {}}
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

    def test_partial_network_body_and_killed_worker_resume_exact_prefix(self):
        for mode in ('resume', 'resume_deadline', 'ignore_range'):
            with self.subTest(mode=mode):
                if (self.root / 'out').exists():
                    import shutil
                    shutil.rmtree(self.root / 'out')
                self.server.state.update(lists=0, urls=0, requests=[], ranges=[])
                self.delays.clear()
                self.run_download(mode)
                half = len(self.server.archive) // 2
                self.assertEqual(self.server.state['ranges'], [None, f'bytes={half}-'])
                self.assertEqual(self.server.state['urls'], 2)
                self.assertEqual(len(set(self.server.state['requests'])), 2)
                self.assertEqual((self.root / 'out/payload.txt').read_bytes(), b'complete verified content')
                self.assertEqual(self.delays, [15])

    def test_bad_resume_responses_and_changed_bytes_fail_closed(self):
        for mode, reason in [('range_wrong_start', 'archive_range_mismatch'),
                             ('range_wrong_end', 'archive_range_mismatch'),
                             ('range_wrong_total', 'archive_range_mismatch'),
                             ('range_missing', 'archive_range_mismatch'),
                             ('length_wrong', 'archive_length_mismatch'),
                             ('range_416', 'http_416'), ('resume_auth', 'http_403'),
                             ('resume_digest', 'digest_mismatch')]:
            with self.subTest(mode=mode):
                self.server.state.update(lists=0, urls=0, requests=[], ranges=[])
                self.delays.clear()
                with self.assertRaisesRegex(d.Failure, reason):
                    self.run_download(mode)
                self.assertEqual(self.server.state['urls'], 2)
                self.assertEqual(self.delays, [15])
                self.assertFalse((self.root / 'out').exists())
                self.assertFalse(list(self.root.glob('.kuasar-artifact-*')))

    def test_partial_cannot_survive_any_frozen_identity_change(self):
        for fields in ({'workflow_run_backend_id': PRIOR_RUN},
                       {'workflow_job_run_backend_id': PRIOR_JOB}, {'database_id': '2'},
                       {'size': str(len(self.server.archive) + 1)}, {'digest': '0' * 64},
                       {'name': 'wanted-replaced'}):
            with self.subTest(fields=fields):
                self.server.state.update(lists=0, urls=0, requests=[], ranges=[], replacement_fields=fields)
                self.delays.clear()
                with self.assertRaisesRegex(d.Failure, 'artifact_identity_changed|artifact_selection_empty'):
                    self.run_download('metadata_partial')
                self.assertEqual(self.server.state['urls'], 1)
                self.assertFalse((self.root / 'out').exists())
                self.assertFalse(list(self.root.glob('.kuasar-artifact-*')))

    def test_unfrozen_partial_is_rejected_without_signing_or_downloading(self):
        staging = self.root / 'private'
        archives = staging / 'archives'
        archives.mkdir(parents=True)
        (archives / '1.zip').write_bytes(self.server.archive[:10])
        attempt = staging / 'attempt'
        attempt.mkdir()
        with self.assertRaisesRegex(d.Failure, 'missing_frozen_identity'):
            local_operation({'name': 'wanted', 'pattern': ''}, None, attempt, lambda message: None)
        self.assertEqual(self.server.state['urls'], 0)

    def test_multi_artifact_retry_reverifies_completed_zip_and_rebuilds_payload(self):
        self.server.state['archives'] = {'wanted': zip_bytes('first', b'one'),
                                         'wanted-2': zip_bytes('second', b'two')}
        self.server.state['extra_artifacts'] = [{'database_id': '2', 'name': 'wanted-2'}]
        self.server.state['mode'] = 'multi_resume'
        with contextlib.redirect_stdout(io.StringIO()):
            d.download({'name': '', 'pattern': 'wanted*'}, self.root / 'out',
                       seconds=.2, operation=local_operation, sleep=self.delays.append)
        self.assertEqual(self.server.state['download_names'], ['wanted', 'wanted-2', 'wanted-2'])
        self.assertEqual(self.server.state['ranges'],
                         [None, None, f"bytes={len(self.server.state['archives']['wanted-2']) // 2}-"])
        self.assertEqual((self.root / 'out/first').read_bytes(), b'one')
        self.assertEqual((self.root / 'out/second').read_bytes(), b'two')
        self.assertFalse(list(self.root.glob('.kuasar-artifact-*')))

    def test_retry_after_is_respected_without_extending_total_budget(self):
        self.run_download('throttle')
        self.assertEqual(self.delays, [41])
        import shutil
        shutil.rmtree(self.root / 'out')
        self.server.state.update(lists=0, urls=0, requests=[], retry_after='86400')
        self.delays.clear()
        with self.assertRaisesRegex(d.Failure, 'total_deadline'):
            self.run_download('throttle')
        self.assertEqual(self.server.state['urls'], 1)
        self.assertEqual(self.delays, [])
        self.assertFalse(list(self.root.glob('.kuasar-artifact-*')))

    def test_throttle_keeps_prefix_and_uses_new_signed_url(self):
        self.server.state['retry_after'] = '20'
        self.run_download('resume_429')
        half = len(self.server.archive) // 2
        self.assertEqual(self.server.state['ranges'], [None, f'bytes={half}-', f'bytes={half}-'])
        self.assertEqual(self.delays, [15, 30])
        self.assertEqual(self.server.state['urls'], 3)

    def test_retry_after_date_and_invalid_value(self):
        headers = {'Retry-After': 'Wed, 21 Oct 2015 07:28:00 GMT'}
        exc = urllib.error.HTTPError('https://invalid', 429, '', headers, None)
        with mock.patch.object(d.time, 'time', return_value=1445412470):
            self.assertEqual(d.classify(exc).retry_after, 10)
        for value in ('-1', '1.5', 'not a date'):
            exc = urllib.error.HTTPError('https://invalid', 429, '', {'Retry-After': value}, None)
            self.assertFalse(d.classify(exc).retryable)

    def test_cancel_removes_real_download_prefix_without_retry(self):
        self.server.state['mode'] = 'resume_deadline'
        proc = multiprocessing.get_context('fork').Process(
            target=cancel_http_supervisor, args=(self.root / 'out',))
        proc.start()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                partials = list(self.root.glob('.kuasar-artifact-*/archives/1.zip'))
                if partials and partials[0].stat().st_size:
                    break
                time.sleep(.01)
            self.assertTrue(partials and partials[0].stat().st_size)
            os.kill(proc.pid, signal.SIGTERM)
            proc.join(3)
            self.assertFalse(proc.is_alive())
            self.assertEqual(len(self.server.state['requests']), 1)
            self.assertFalse((self.root / 'out').exists())
            self.assertFalse(list(self.root.glob('.kuasar-artifact-*')))
        finally:
            if proc.is_alive():
                proc.kill()
                proc.join()

    def test_completed_archive_is_rehashed_before_reuse(self):
        self.server.state.update(mode='multi_resume',
            archives={'wanted': zip_bytes('first', b'one'), 'wanted-2': zip_bytes('second', b'two')},
            extra_artifacts=[{'database_id': '2', 'name': 'wanted-2'}])
        def corrupt_verified_archive(delay):
            archive, = self.root.glob('.kuasar-artifact-*/archives/1.zip')
            body = archive.read_bytes()
            archive.write_bytes(bytes([body[0] ^ 1]) + body[1:])
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(d.Failure, 'digest_mismatch'):
                d.download({'name': '', 'pattern': 'wanted*'}, self.root / 'out',
                           seconds=.2, operation=local_operation, sleep=corrupt_verified_archive)
        self.assertEqual(self.server.state['urls'], 2)
        self.assertFalse((self.root / 'out').exists())
        self.assertFalse(list(self.root.glob('.kuasar-artifact-*')))

    def test_killed_extraction_rebuilds_payload_from_verified_archive(self):
        marker = self.root / 'first-extraction'
        extract = d.extract_archive
        def interrupted_extract(archive, destination):
            extract(archive, destination)
            if not marker.exists():
                marker.touch()
                time.sleep(.6)
        with mock.patch.object(d, 'extract_archive', side_effect=interrupted_extract):
            self.run_download('ok')
        self.assertEqual(self.server.state['urls'], 1)
        self.assertEqual(self.server.state['lists'], 2)
        self.assertEqual((self.root / 'out/payload.txt').read_bytes(), b'complete verified content')

    def test_total_budget_includes_backoff_and_does_not_leave_workers(self):
        marker = self.root / 'writes'
        started = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(d.Failure, 'attempt_deadline|total_deadline'):
                d.download({'name': 'wanted', 'pattern': '', 'marker': str(marker)},
                           self.root / 'out', seconds=.1, delays=(.05, .05),
                           operation=write_until_stopped)
        self.assertLess(time.monotonic() - started, .8)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.with_suffix('.pid').read_text()), 0)
        self.assertFalse((self.root / 'out').exists())
        self.assertFalse(list(self.root.glob('.kuasar-artifact-*')))

    def test_prior_attempt_artifact_uses_current_scope_and_producer_signing_ids(self):
        self.server.state['artifact_overrides'] = {
            'workflow_run_backend_id': PRIOR_RUN, 'workflow_job_run_backend_id': PRIOR_JOB}
        self.run_download('reset')
        self.assertEqual((self.root / 'out/payload.txt').read_bytes(), b'complete verified content')
        self.assertEqual(self.delays, [15])
        calls = self.server.state['rpc_requests']
        self.assertEqual([method for method, _, _ in calls],
                         ['ListArtifacts', 'GetSignedArtifactURL'] * 2)
        for method, data, authorization in calls:
            self.assertEqual(authorization, 'Bearer test-token')
            self.assertEqual(data, {'workflow_run_backend_id': RUN,
                                    'workflow_job_run_backend_id': JOB}
                             if method == 'ListArtifacts' else
                             {'workflow_run_backend_id': PRIOR_RUN,
                              'workflow_job_run_backend_id': PRIOR_JOB, 'name': 'wanted'})
        self.assertEqual(self.server.state['blob_authorizations'], [None, None])

    def test_latest_name_version_is_downloaded_without_stale_producer_fallback(self):
        self.server.state['artifact_overrides'] = {'database_id': '10'}
        self.server.state['extra_artifacts'] = [{
            'database_id': '9', 'workflow_run_backend_id': PRIOR_RUN,
            'workflow_job_run_backend_id': PRIOR_JOB, 'digest': 'sha256:' + '0' * 64}]
        self.run_download('reset')
        self.assertEqual((self.root / 'out/payload.txt').read_bytes(), b'complete verified content')
        self.assertEqual(self.delays, [15])
        for method, data, _ in self.server.state['rpc_requests']:
            self.assertEqual(data['workflow_run_backend_id'], RUN)
            self.assertEqual(data['workflow_job_run_backend_id'], JOB)

    def test_newest_invalid_metadata_cannot_fall_back_to_older_valid_artifact(self):
        self.server.state['artifact_overrides'] = {'database_id': '10', 'digest': None}
        self.server.state['extra_artifacts'] = [{
            'database_id': '9', 'digest': 'sha256:' + hashlib.sha256(self.server.archive).hexdigest()}]
        with self.assertRaisesRegex(d.Failure, 'missing_digest'):
            self.run_download('ok')
        self.assertEqual(self.server.state['urls'], 0)
        self.assertEqual(self.delays, [])
        self.assertFalse((self.root / 'out').exists())

    def test_latest_download_failure_never_tries_an_older_version(self):
        for mode, reason, attempts in [('auth', 'http_403', 1),
                                        ('digest', 'digest_mismatch', 1),
                                        ('reset_always', 'connection_interrupted', 3)]:
            with self.subTest(mode=mode):
                self.server.state.update(lists=0, urls=0, requests=[], rpc_requests=[],
                    artifact_overrides={'database_id': '10'},
                    extra_artifacts=[{'database_id': '9', 'workflow_run_backend_id': PRIOR_RUN,
                        'workflow_job_run_backend_id': PRIOR_JOB,
                        'digest': 'sha256:' + hashlib.sha256(self.server.archive).hexdigest()}])
                self.delays.clear()
                with self.assertRaisesRegex(d.Failure, reason):
                    self.run_download(mode)
                signed = [data for method, data, _ in self.server.state['rpc_requests']
                          if method == 'GetSignedArtifactURL']
                self.assertEqual(len(signed), attempts)
                self.assertTrue(all(data == {'workflow_run_backend_id': RUN,
                    'workflow_job_run_backend_id': JOB, 'name': 'wanted'} for data in signed))
                self.assertFalse((self.root / 'out').exists())

    def test_retry_cannot_upgrade_or_roll_back_frozen_name_version(self):
        for replacement in ('11', '8'):
            with self.subTest(replacement=replacement):
                self.server.state.update(lists=0, urls=0, requests=[],
                    artifact_overrides={'database_id': '10'},
                    extra_artifacts=[{'database_id': '9', 'workflow_run_backend_id': PRIOR_RUN,
                                      'workflow_job_run_backend_id': PRIOR_JOB}],
                    replacement_fields={'database_id': replacement})
                self.delays.clear()
                with self.assertRaisesRegex(d.Failure, 'artifact_identity_changed'):
                    self.run_download('metadata_change')
                self.assertEqual(self.server.state['lists'], 2)
                self.assertEqual(self.server.state['urls'], 1)
                self.assertEqual(self.delays, [15])
                self.assertFalse((self.root / 'out').exists())

    def test_latest_selection_is_numeric_order_independent_and_per_exact_name(self):
        row = {'workflow_run_backend_id': RUN, 'workflow_job_run_backend_id': JOB,
               'database_id': '10', 'name': 'wanted', 'size': '100', 'digest': '0' * 64}
        older = {**row, 'database_id': '9', 'workflow_run_backend_id': PRIOR_RUN,
                 'workflow_job_run_backend_id': PRIOR_JOB}
        other = {**row, 'database_id': '11', 'name': 'wanted-other'}
        for rows in ([older, row, other], [other, row, older], [row, older, other]):
            with self.subTest(rows=rows):
                exact = d.select_artifacts({'artifacts': rows}, {'name': 'wanted', 'pattern': ''})
                pattern = d.select_artifacts({'artifacts': rows}, {'name': '', 'pattern': 'want*'})
                self.assertEqual(exact, [d.normalize(row)])
                self.assertEqual(pattern, [d.normalize(row), d.normalize(other)])
        with self.assertRaisesRegex(d.Failure, 'ambiguous_artifact_identity'):
            d.select_artifacts({'artifacts': [row, {**other, 'database_id': '10'}]},
                               {'name': '', 'pattern': 'want*'})

    def test_metadata_authorization_denial_cannot_fall_back_to_another_run(self):
        with self.assertRaisesRegex(d.Failure, 'http_403'):
            self.run_download('list_auth')
        self.assertEqual(self.server.state['lists'], 1)
        self.assertEqual(self.server.state['urls'], 0)
        self.assertEqual(self.server.state['requests'], [])
        self.assertEqual(self.delays, [])
        self.assertFalse((self.root / 'out').exists())

    def test_producer_ids_size_and_digest_are_frozen_across_retries(self):
        for fields in ({'workflow_run_backend_id': PRIOR_RUN},
                       {'workflow_job_run_backend_id': PRIOR_JOB},
                       {'size': str(len(self.server.archive) + 1)},
                       {'digest': 'sha256:' + '0' * 64}):
            with self.subTest(fields=fields):
                self.server.state.update(lists=0, urls=0, requests=[], replacement_fields=fields)
                self.delays.clear()
                with self.assertRaisesRegex(d.Failure, 'artifact_identity_changed'):
                    self.run_download('metadata_change')
                self.assertEqual(self.server.state['lists'], 2)
                self.assertEqual(self.server.state['urls'], 1)
                self.assertEqual(self.delays, [15])
                self.assertFalse((self.root / 'out').exists())

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

    def test_metadata_missing_digest_and_invalid_producer_ids_fail_closed(self):
        row = {'workflow_run_backend_id': RUN, 'workflow_job_run_backend_id': JOB,
               'database_id': '1', 'name': 'wanted', 'size': '100'}
        with self.assertRaisesRegex(d.Failure, 'missing_digest'):
            d.normalize(row)
        row['digest'] = 'sha256:' + '0' * 64
        for key, reason in [('workflow_run_backend_id', 'invalid_artifact_run'),
                            ('workflow_job_run_backend_id', 'invalid_artifact_job')]:
            for invalid in (None, '', '39312713058', '-' * 36, '1' * 36, '../another-run'):
                with self.subTest(key=key, invalid=invalid):
                    with self.assertRaisesRegex(d.Failure, reason):
                        d.normalize({**row, key: invalid})
        selected = d.select_artifacts({'artifacts': [row]},
                                     {'name': '', 'pattern': 'want*'})
        self.assertEqual(selected[0]['database_id'], '1')
        with self.assertRaisesRegex(d.Failure, 'ambiguous_artifact_identity'):
            d.select_artifacts({'artifacts': [row, row]},
                               {'name': 'wanted', 'pattern': ''})

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
