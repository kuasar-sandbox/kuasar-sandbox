#!/usr/bin/env python3
"""Real Unix-socket clients and HTTP bodies exercise cache integrity/coalescing."""
import concurrent.futures
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile

import artifact_cache_server as cache
import download_artifact as d


class Origin(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append(self.headers.get('Range'))
        self.server.gate.wait(5)
        body = self.server.body
        offset = int(self.headers['Range'][6:-1]) if self.headers.get('Range') else 0
        self.send_response(206 if offset else 200)
        if offset:
            self.send_header('Content-Range', f'bytes {offset}-{len(body)-1}/{len(body)}')
        self.send_header('Content-Length', str(len(body)-offset))
        self.end_headers()
        if self.server.interrupt and len(self.server.requests) == 1:
            self.wfile.write(body[:len(body)//2])
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
        else:
            self.wfile.write(body[offset:])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('payload.txt', b'verified artifact\n' * 1000)
        self.body = output.getvalue()
        self.row = {'database_id': '123', 'digest': hashlib.sha256(self.body).hexdigest(), 'size': len(self.body),
                    'workflow_run_backend_id': '11111111-1111-1111-1111-111111111111',
                    'workflow_job_run_backend_id': '22222222-2222-2222-2222-222222222222', 'name': 'wanted'}
        self.origin = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        self.origin.body, self.origin.requests, self.origin.interrupt = self.body, [], False
        self.origin.gate = threading.Event()
        self.origin.gate.set()
        self.origin_thread = threading.Thread(target=self.origin.serve_forever, daemon=True)
        self.origin_thread.start()
        self.url = f'http://127.0.0.1:{self.origin.server_port}/blob?sig=never-log-this'
        self.url_patch = mock.patch.object(d, 'secure_url', side_effect=lambda value: value)
        self.url_patch.start()
        self.backend = cache.Cache(self.root/'cache', reserve=0, fetch=d.transfer,
                                   url_validator=lambda url: None, delays=(.01, .01))
        self.socket = str(self.root/'cache.sock')
        self.server = cache.Server(self.socket, self.backend)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.environment = mock.patch.dict(os.environ, {'KUASAR_ARTIFACT_CACHE_SOCKET': self.socket,
                                                        'GITHUB_REPOSITORY': 'example/project'})
        self.environment.start()

    def tearDown(self):
        self.origin.gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.origin.shutdown()
        self.origin.server_close()
        self.origin_thread.join()
        self.environment.stop()
        self.url_patch.stop()

    def get(self, row=None):
        events = []
        path = d.shared_archive(row or self.row, self.url, events.append)
        return path, events

    def test_eight_clients_share_one_download_and_verified_read_only_archive(self):
        self.origin.gate.clear()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.get) for _ in range(8)]
            deadline = time.monotonic() + 3
            while self.backend.stats().get('coalesced', 0) < 7 and time.monotonic() < deadline:
                time.sleep(.01)
            self.origin.gate.set()
            results = [f.result(timeout=5) for f in futures]
        self.assertEqual(len(self.origin.requests), 1)
        self.assertEqual(self.backend.stats()['coalesced'], 7)
        self.assertEqual(len({str(path) for path, _ in results}), 1)
        self.assertEqual(results[0][0].read_bytes(), self.body)
        self.assertEqual(results[0][0].stat().st_mode & 0o777, 0o440)
        self.assertEqual((self.root/'cache/.partial').stat().st_mode & 0o777, 0o700)
        _, events = self.get()
        self.assertEqual(events[-1]['phase'], 'cache_hit')
        self.assertEqual(len(self.origin.requests), 1)

    def test_disconnected_owner_does_not_cancel_shared_producer(self):
        self.origin.gate.clear()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as owner:
            owner.connect(self.socket)
            owner.sendall(json.dumps({'repository':'example/project', 'artifact':self.row,
                                      'url':self.url}).encode()+b'\n')
            deadline = time.monotonic()+3
            while not self.origin.requests and time.monotonic()<deadline:
                time.sleep(.01)
        self.origin.gate.set()
        path, _ = self.get()
        self.assertEqual(path.read_bytes(), self.body)
        self.assertEqual(len(self.origin.requests), 1)

    def test_partial_body_is_resumed_before_atomic_publish(self):
        self.origin.interrupt = True
        path, _ = self.get()
        self.assertEqual(path.read_bytes(), self.body)
        self.assertEqual(self.origin.requests, [None, f'bytes={len(self.body)//2}-'])
        self.assertFalse(list((self.root/'cache/.partial').iterdir()))

    def test_expired_blob_url_preserves_prefix_for_fresh_authorized_attempt(self):
        def expired(row, url, partial):
            partial.write_bytes(self.body[:len(self.body)//2])
            raise d.Failure('http_403')
        self.backend.fetch = expired
        with self.assertRaisesRegex(d.Failure, 'cache_signed_url_rejected') as result:
            self.attempt(1)
        self.assertTrue(result.exception.retryable)
        parts = list((self.root/'cache/.partial').glob('*.part'))
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].stat().st_size, len(self.body)//2)
        # Metadata denial remains terminal even while a cache prefix exists.
        with self.assertRaisesRegex(d.Failure, 'http_403') as denied:
            self.attempt(2, denied=True)
        self.assertFalse(denied.exception.retryable)
        self.backend.fetch = d.transfer
        path, calls = self.attempt(3)
        self.assertEqual(path.read_bytes(), b'verified artifact\n'*1000)
        self.assertEqual([call.args[2] for call in calls.call_args_list], ['ListArtifacts','GetSignedArtifactURL'])
        self.assertEqual(self.origin.requests, [f'bytes={len(self.body)//2}-'])

    def test_corrupt_cached_archive_is_replaced(self):
        path, _ = self.get()
        path.chmod(0o640)
        path.write_bytes(b'x'*len(self.body))
        replacement, _ = self.get()
        self.assertEqual(replacement.read_bytes(), self.body)
        self.assertEqual(len(self.origin.requests), 2)
        self.assertEqual(self.backend.stats()['corrupt_entries'], 1)

    def test_bad_upstream_digest_is_never_published(self):
        with self.assertRaisesRegex(d.Failure, 'cache_digest_mismatch'):
            self.get(dict(self.row, digest='0'*64))
        self.assertFalse(list((self.root/'cache').glob('*.zip')))

    def test_repository_and_digest_are_part_of_cache_identity(self):
        first, _ = self.get()
        with mock.patch.dict(os.environ, {'GITHUB_REPOSITORY':'another/project'}):
            second, _ = self.get()
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.origin.requests), 2)

    def attempt(self, index, denied=False):
        parent = self.root/str(index)
        (parent/'archives').mkdir(parents=True)
        directory = parent/'attempt'
        directory.mkdir()
        identity = {'workflow_run_backend_id':self.row['workflow_run_backend_id'],
                    'workflow_job_run_backend_id':self.row['workflow_job_run_backend_id']}
        def rpc(service, token, method, data):
            if denied:
                raise d.Failure('http_403')
            return {'artifacts':[self.row]} if method=='ListArtifacts' else {'signed_url':self.url}
        with mock.patch.object(d,'runtime_identity',return_value=('test-token','https://example.test',identity)), \
             mock.patch.object(d,'rpc',side_effect=rpc) as calls:
            d.attempt({'name':'wanted','pattern':''},None,directory,lambda message:None)
        return directory/'payload/payload.txt', calls

    def test_cache_hit_still_authorizes_and_extracts_from_fresh_metadata(self):
        first, _ = self.attempt(1)
        second, calls = self.attempt(2)
        self.assertEqual(first.read_bytes(),second.read_bytes())
        self.assertEqual([call.args[2] for call in calls.call_args_list], ['ListArtifacts','GetSignedArtifactURL'])
        self.assertEqual(len(self.origin.requests),1)
        with self.assertRaisesRegex(d.Failure,'http_403'):
            self.attempt(3,denied=True)
        self.assertEqual(len(self.origin.requests),1)

    def test_absent_cache_falls_back_to_original_verified_download(self):
        with mock.patch.dict(os.environ, {'KUASAR_ARTIFACT_CACHE_SOCKET':str(self.root/'absent.sock')}):
            path, _ = self.attempt(1)
        self.assertEqual(path.read_bytes(), b'verified artifact\n'*1000)
        self.assertEqual(len(self.origin.requests),1)

    def test_capacity_exhaustion_falls_back_without_publishing(self):
        self.backend.capacity=1
        path, _ = self.attempt(1)
        self.assertEqual(path.read_bytes(), b'verified artifact\n'*1000)
        self.assertFalse(list((self.root/'cache').glob('*.zip')))

    def test_retained_failed_downloads_consume_the_storage_budget(self):
        def expired(row, url, partial):
            partial.write_bytes(self.body)
            raise d.Failure('http_403')
        self.backend.fetch = expired
        for artifact_id in ('456', '789'):
            with self.assertRaisesRegex(d.Failure, 'cache_signed_url_rejected'):
                self.get(dict(self.row, database_id=artifact_id))
        self.backend.capacity = 2 * len(self.body)
        self.backend.fetch = d.transfer
        path, _ = self.attempt(1)
        self.assertEqual(path.read_bytes(), b'verified artifact\n'*1000)
        self.assertEqual(len(list((self.root/'cache/.partial').glob('*.part'))), 2)
        self.assertFalse(list((self.root/'cache').glob('*.zip')))

    def test_cache_never_exposes_a_digest_mismatch_to_extraction(self):
        path, _ = self.get()
        bad=self.root/'bad.zip'
        bad.write_bytes(b'x'*len(self.body))
        with mock.patch.object(self.backend,'get',return_value={'status':'ready','cache':'hit','path':str(bad)}):
            with self.assertRaisesRegex(d.Failure,'digest_mismatch'):
                self.get()


class OriginValidation(unittest.TestCase):
    def test_only_https_actions_blob_origins_are_allowed(self):
        cache.validate_url('https://productionresultssa11.blob.core.windows.net/a?sig=secret')
        for url in ['http://productionresultssa11.blob.core.windows.net/a', 'https://127.0.0.1/a',
                    'https://productionresultssa11.blob.core.windows.net.attacker.test/a',
                    'https://user:password@productionresultssa11.blob.core.windows.net/a',
                    'https://productionresultssa11.blob.core.windows.net:22/a']:
            with self.subTest(url=url), self.assertRaises(d.Failure):
                cache.validate_url(url)


if __name__=='__main__':
    unittest.main()
