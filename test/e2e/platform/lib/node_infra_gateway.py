#!/usr/bin/env python3
"""Private HTTP endpoint inside the test-owned Linux GENEVE gateway netns."""
import http.server
from pathlib import Path
import sys


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(sys.argv[2].encode())

    def log_message(self, *_args):
        pass


if __name__ == '__main__':
    with http.server.ThreadingHTTPServer(('10.231.0.1', 18080), Handler) as server:
        Path(sys.argv[1]).write_text('ready\n')
        server.serve_forever()
