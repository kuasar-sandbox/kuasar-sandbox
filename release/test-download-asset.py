#!/usr/bin/env python3
"""Exercise real curl retries against a local, deliberately truncated response."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = b'complete release asset\n' * 1024


class DownloadTests(unittest.TestCase):
    def download(self, always_fail=False):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.send_header('Content-Length', str(len(PAYLOAD)))
                self.end_headers()
                self.wfile.write(PAYLOAD[:117] if always_fail or len(requests) == 1 else PAYLOAD)
                self.close_connection = True

            def log_message(self, *_args):
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with tempfile.TemporaryDirectory() as temporary:
                    output = Path(temporary) / 'asset'
                    output.write_bytes(b'previous destination')
                    result = subprocess.run(
                        ['bash', '-c', 'source "$1"; github_download_asset owner/repo 1 "$2"',
                         'download-test', str(ROOT / 'release/lib.sh'), str(output)],
                        env={**os.environ, 'GH_TOKEN': 'local-test-token',
                             'GITHUB_API_URL': 'http://127.0.0.1:' + str(server.server_port),
                             'NO_PROXY': '127.0.0.1', 'no_proxy': '127.0.0.1'},
                        capture_output=True, timeout=40)
                    self.assertEqual(list(Path(temporary).iterdir()), [output])
                    if always_fail:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(output.read_bytes(), b'previous destination')
                        self.assertEqual(len(requests), 5)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr.decode())
                        self.assertEqual(len(requests), 2)
                        self.assertEqual(output.read_bytes(), PAYLOAD)
                        self.assertEqual(hashlib.sha256(output.read_bytes()).digest(),
                                         hashlib.sha256(PAYLOAD).digest())
            finally:
                server.shutdown()
                thread.join()

    def test_truncated_response_retries_without_concatenation(self):
        self.download()

    def test_exhausted_retries_preserve_destination(self):
        self.download(always_fail=True)


if __name__ == '__main__':
    unittest.main()
