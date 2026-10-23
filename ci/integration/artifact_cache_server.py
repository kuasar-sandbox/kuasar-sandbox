#!/usr/bin/env python3
"""Optional host-local artifact cache. No GitHub credentials or signed URLs persist.

Clients first resolve immutable metadata with their own Actions runtime identity.
The daemon coalesces identical downloads and publishes only verified archives.
Run as a dedicated user; only that user may write the cache directory.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import socketserver
import stat
import subprocess
import sys
import threading
import time
import urllib.parse

import download_artifact as d

MAX_REQUEST = 16384


def validate_request(request):
    repository = request.get('repository', '')
    d.require(isinstance(repository, str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository),
              'invalid_cache_repository')
    row = request.get('artifact', {})
    d.require(isinstance(row, dict), 'invalid_cache_artifact')
    artifact_id, digest, size = (row.get(k) for k in ('database_id', 'digest', 'size'))
    d.require(isinstance(artifact_id, str) and re.fullmatch(r'[1-9][0-9]*', artifact_id), 'invalid_artifact_id')
    d.require(isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest), 'invalid_digest')
    d.require(type(size) is int and 0 < size <= d.MAX_ARCHIVE_BYTES, 'artifact_size_limit')
    row = {'database_id': artifact_id, 'digest': digest, 'size': size}
    identity = json.dumps([repository, artifact_id, digest, size], separators=(',', ':')).encode()
    return row, hashlib.sha256(identity).hexdigest()


def validate_url(url):
    d.require(isinstance(url, str), 'invalid_signed_url')
    parsed = urllib.parse.urlsplit(d.secure_url(url))
    # Only the Azure blob endpoint used by Actions artifacts; no redirects,
    # arbitrary web fetches, Authorization headers or localhost access.
    d.require(parsed.port in (None, 443) and
              re.fullmatch(r'productionresults[a-z0-9]+\.blob\.core\.windows\.net', parsed.hostname),
              'unsupported_cache_origin')


class Entry:
    def __init__(self):
        self.condition = threading.Condition()
        self.running = False
        self.url = None
        self.result = None
        self.error = None


class Cache:
    def __init__(self, root, *, capacity=64 * 1024**3, reserve=16 * 1024**3,
                 fetch=None, url_validator=validate_url, delays=(15, 30)):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.partial = self.root / '.partial'
        self.partial.mkdir(mode=0o700, exist_ok=True)
        self.partial.chmod(0o700)
        self.capacity, self.reserve = capacity, reserve
        self.fetch = fetch or self.fetch_process
        self.url_validator = url_validator
        self.delays = delays
        self.entries = {}
        self.lock = threading.Lock()
        self.download_slots = threading.BoundedSemaphore(4)
        self.counters = Counter()
        self.reserved = 0

    def count(self, **values):
        with self.lock:
            self.counters.update(values)

    def stats(self):
        with self.lock:
            return dict(self.counters)

    def make_room(self, required, protected):
        """LRU eviction only for complete archives unused for at least an hour."""
        with self.lock:
            for path in self.partial.glob('*.part'):
                entry = self.entries.get(path.stem)
                if path.stat().st_mtime < time.time() - 86400 and not (entry and entry.running):
                    path.unlink()
            files = [(p.stat().st_mtime, p.stat().st_size, p) for p in self.root.glob('*.zip')
                     if p.is_file() and not p.is_symlink()]
            used = sum(size for _, size, _ in files)
            for accessed, size, path in sorted(files):
                free = d.shutil.disk_usage(self.root).free
                if used + self.reserved + required <= self.capacity and free >= self.reserved + required + self.reserve:
                    break
                entry = self.entries.get(path.stem)
                if path == protected or accessed > time.time() - 3600 or (entry and entry.running):
                    continue
                path.unlink()
                used -= size
                self.counters['evictions'] += 1
            d.require(used + self.reserved + required <= self.capacity and
                      d.shutil.disk_usage(self.root).free >= self.reserved + required + self.reserve, 'cache_capacity')
            self.reserved += required

    def fetch_process(self, row, url, partial):
        # A separate process gives transfers a hard deadline without forking the
        # multithreaded server. The signed URL travels only through stdin.
        payload = json.dumps({'artifact': row, 'url': url, 'path': str(partial)})
        process = subprocess.Popen([sys.executable, '-B', __file__, '--worker'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True)
        try:
            output, _ = process.communicate(payload, timeout=600)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise d.Failure('attempt_deadline', True) from None
        d.require(process.returncode == 0, 'cache_worker_failed')
        result = json.loads(output)
        if not result['ok']:
            raise d.Failure(result['reason'], result['retryable'], result.get('retry_after', 0))

    def produce(self, entry, row, key):
        final, partial = self.root / (key + '.zip'), self.partial / (key + '.part')
        result, error, reserved = None, None, False
        try:
            if os.path.lexists(final):
                try:
                    d.require(stat.S_ISREG(final.lstat().st_mode) and final.stat().st_size == row['size'],
                              'invalid_cache_archive')
                    d.transfer(row, None, final)
                    os.utime(final, None)
                    self.count(hits=1)
                    result = {'status': 'ready', 'path': str(final), 'cache': 'hit'}
                except Exception:
                    final.unlink()
                    self.count(corrupt_entries=1)
            if result is None:
                with self.download_slots:
                    self.make_room(row['size'], final)
                    reserved = True
                    self.count(misses=1)
                    for attempt in range(len(self.delays) + 1):
                        with entry.condition:
                            url = entry.url
                        before = partial.stat().st_size if partial.exists() else 0
                        self.count(upstream_attempts=1)
                        try:
                            self.fetch(row, url, partial)
                            break
                        except Exception as exc:
                            failure = d.classify(exc)
                            self.count(download_failures=1)
                            if not failure.retryable or attempt == len(self.delays):
                                if not failure.retryable:
                                    partial.unlink(missing_ok=True)
                                raise failure from None
                            time.sleep(min(60, max(self.delays[attempt], failure.retry_after)))
                        finally:
                            after = partial.stat().st_size if partial.exists() else 0
                            self.count(retained_download_bytes=max(0, after - before))
                    # Verify even injected/custom fetch implementations before publish.
                    d.transfer(row, None, partial)
                    with partial.open('rb') as source:
                        os.fsync(source.fileno())
                    partial.chmod(0o440)
                    partial.replace(final)
                    self.count(published=1)
                    result = {'status': 'ready', 'path': str(final), 'cache': 'miss'}
                    print(json.dumps({'event': 'published', 'artifact_id': row['database_id'],
                                      'bytes': row['size'], 'key': key}), flush=True)
        except Exception as exc:
            failure = d.classify(exc)
            error = {'status': 'unavailable' if failure.reason in ('cache_capacity', 'cache_worker_failed', 'unexpected_error') else 'error',
                     'reason': failure.reason, 'retryable': failure.retryable,
                     'retry_after': failure.retry_after}
            print(json.dumps({'event': 'cache_failure', 'artifact_id': row['database_id'],
                              'reason': failure.reason}), flush=True)
        finally:
            if reserved:
                with self.lock:
                    self.reserved -= row['size']
            with entry.condition:
                entry.result, entry.error, entry.running = result, error, False
                entry.condition.notify_all()

    def get(self, request, notify=lambda value: None):
        row, key = validate_request(request)
        self.url_validator(request.get('url'))
        with self.lock:
            entry = self.entries.setdefault(key, Entry())
        with entry.condition:
            joined = entry.running
            entry.url = request['url']
            if not joined:
                entry.running, entry.result, entry.error = True, None, None
                threading.Thread(target=self.produce, args=(entry, row, key), daemon=True).start()
        if joined:
            self.count(coalesced=1)
        while True:
            with entry.condition:
                if not entry.running:
                    response = dict(entry.error or entry.result)
                    if joined and response['status'] == 'ready':
                        response['cache'] = 'shared'
                    return response
                entry.condition.wait(2)
            notify({'status': 'waiting', 'artifact_id': row['database_id']})


class Handler(socketserver.StreamRequestHandler):
    def send(self, message):
        self.wfile.write(json.dumps(message).encode() + b'\n')
        self.wfile.flush()

    def handle(self):
        self.request.settimeout(10)
        try:
            line = self.rfile.readline(MAX_REQUEST + 1)
            d.require(len(line) <= MAX_REQUEST and line.endswith(b'\n'), 'invalid_cache_request')
            request = json.loads(line)
            if request.get('op') == 'stats':
                self.send({'status': 'stats', **self.server.cache.stats()})
            else:
                self.send(self.server.cache.get(request, self.send))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass  # The bounded producer serves the other waiters and later retries.
        except Exception as exc:
            try:
                failure = d.classify(exc)
                self.send({'status': 'unavailable' if failure.reason == 'unsupported_cache_origin' else 'error',
                           'reason': failure.reason, 'retryable': failure.retryable})
            except OSError:
                pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, socket_path, cache):
        self.cache = cache
        self.client_slots = threading.BoundedSemaphore(64)
        super().__init__(socket_path, Handler)

    def process_request(self, request, client_address):
        if not self.client_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.client_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.client_slots.release()

    def handle_error(self, request, address):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--socket', default='/run/ci-artifact-cache/cache.sock')
    parser.add_argument('--root', default='/var/cache/ci-artifacts')
    args = parser.parse_args()
    os.umask(0o007)
    if args.worker:
        request = json.load(sys.stdin)
        try:
            validate_url(request['url'])
            d.transfer(request['artifact'], request['url'], Path(request['path']))
            result = {'ok': True}
        except Exception as exc:
            failure = d.classify(exc)
            result = {'ok': False, 'reason': failure.reason, 'retryable': failure.retryable,
                      'retry_after': failure.retry_after}
        print(json.dumps(result))
        return
    socket_path = Path(args.socket)
    socket_path.unlink(missing_ok=True)
    with Server(str(socket_path), Cache(args.root)) as server:
        socket_path.chmod(0o660)
        server.serve_forever()


if __name__ == '__main__':
    main()
