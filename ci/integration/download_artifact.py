#!/usr/bin/env python3
"""Bounded same-run Actions artifacts; Linux runners, Python standard library only.

The two read-only Twirp methods and protobuf JSON fields follow @actions/artifact
6.2.1, used by actions/download-artifact at
3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c. No SDK/HTTP automatic retry is layered
under the three supervised attempts. Runtime credentials and signed URLs are
never written to files or logs.
"""
import base64
import errno
import fnmatch
import hashlib
import http.client
import json
import multiprocessing
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import socket
import ssl
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ATTEMPT_SECONDS = 600
BACKOFF_SECONDS = (15, 30)
MAX_ARCHIVE_BYTES = 20 * 1024**3
MAX_EXPANDED_BYTES = 40 * 1024**3
MAX_MEMBERS = 200000
SERVICE = 'github.actions.results.api.v1.ArtifactService'
BACKEND_ID = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'


class Failure(Exception):
    def __init__(self, reason, retryable=False):
        super().__init__(reason)
        self.reason, self.retryable = reason, retryable


def require(condition, reason):
    if not condition:
        raise Failure(reason)


def classify(exc):
    """Typed failures only: text mentioning timeout cannot hide auth/integrity."""
    if isinstance(exc, Failure):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        return Failure('http_' + str(exc.code), exc.code in (408, 429, 500, 502, 503, 504))
    if isinstance(exc, urllib.error.URLError):
        return classify(exc.reason)
    if isinstance(exc, ssl.SSLError):
        return Failure('tls_error')
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return Failure('socket_timeout', True)
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                        http.client.RemoteDisconnected, http.client.IncompleteRead)):
        return Failure('connection_interrupted', True)
    if isinstance(exc, socket.gaierror):
        return Failure('dns_temporary', True) if exc.errno == socket.EAI_AGAIN else Failure('dns_error')
    if isinstance(exc, OSError) and exc.errno in (errno.ECONNRESET, errno.ECONNABORTED, errno.ETIMEDOUT):
        return Failure('connection_interrupted', True)
    if isinstance(exc, (zipfile.BadZipFile, NotImplementedError)):
        return Failure('invalid_archive')
    return Failure('unexpected_error')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def secure_url(value):
    parsed = urllib.parse.urlsplit(value)
    require(parsed.scheme == 'https' and parsed.hostname and not parsed.username
            and not parsed.password and not parsed.fragment, 'invalid_service_url')
    return value


def open_url(url, *, token=None, data=None):
    # A new opener/request for every call; Authorization never reaches blob URLs.
    headers = {'User-Agent': 'kuasar-bounded-artifact/1', 'Connection': 'close'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if data is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(data).encode()
    request = urllib.request.Request(secure_url(url), data=data, headers=headers)
    return urllib.request.build_opener(NoRedirect).open(request, timeout=30)


def runtime_identity():
    token = os.environ.get('ACTIONS_RUNTIME_TOKEN', '')
    service = os.environ.get('ACTIONS_RESULTS_URL', '')
    require(bool(token and service), 'missing_runtime_identity')
    try:
        payload = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        scopes = [s.split(':') for s in claims['scp'].split()
                  if s.startswith('Actions.Results:')]
        require(len(scopes) == 1 and len(scopes[0]) == 3, 'invalid_runtime_identity')
        _, run, job = scopes[0]
        require(all(re.fullmatch(BACKEND_ID, x) for x in (run, job)),
                'invalid_runtime_identity')
        secure_url(service)
    except (ValueError, KeyError, IndexError, TypeError):
        raise Failure('invalid_runtime_identity') from None
    return token, service, {'workflow_run_backend_id': run, 'workflow_job_run_backend_id': job}


def rpc(service, token, method, data):
    url = urllib.parse.urljoin(service, '/twirp/' + SERVICE + '/' + method)
    with open_url(url, token=token, data=data) as response:
        raw = response.read(4 * 1024**2 + 1)
        require(len(raw) <= 4 * 1024**2, 'metadata_too_large')
        return json.loads(raw)


def field(row, snake):
    camel = snake.split('_')[0] + ''.join(p.title() for p in snake.split('_')[1:])
    return row.get(snake, row.get(camel))


def normalize(row):
    digest = field(row, 'digest')
    if isinstance(digest, dict):
        digest = digest.get('value')
    require(isinstance(digest, str), 'missing_digest')
    digest = digest.removeprefix('sha256:')
    require(re.fullmatch(r'[0-9a-f]{64}', digest), 'invalid_digest')
    result = {key: field(row, key) for key in
              ('workflow_run_backend_id', 'workflow_job_run_backend_id', 'database_id', 'name', 'size')}
    # ListArtifacts is authorized with the current runtime token's backend IDs.
    # A failed-job rerun may receive artifacts from an earlier attempt of this
    # same logical GitHub run, with different producer backend IDs. Preserve
    # those IDs for signing and freeze them with the artifact; do not compare
    # them to the caller's backend plan identity or to numeric GITHUB_RUN_ID.
    require(isinstance(result['workflow_run_backend_id'], str)
            and re.fullmatch(BACKEND_ID, result['workflow_run_backend_id']),
            'invalid_artifact_run')
    require(isinstance(result['name'], str) and 0 < len(result['name']) <= 256, 'invalid_artifact_name')
    require(isinstance(result['workflow_job_run_backend_id'], str)
            and re.fullmatch(BACKEND_ID, result['workflow_job_run_backend_id']),
            'invalid_artifact_job')
    result['database_id'] = str(result['database_id'])
    require(re.fullmatch(r'[1-9][0-9]*', result['database_id']), 'invalid_artifact_id')
    result['size'] = int(result['size'])
    require(0 < result['size'] <= MAX_ARCHIVE_BYTES, 'artifact_size_limit')
    result['digest'] = digest
    return result


def select_artifacts(response, spec):
    rows = response.get('artifacts')
    require(isinstance(rows, list), 'invalid_metadata')
    selected = [normalize(row) for row in rows
                if (field(row, 'name') == spec['name'] if spec['name'] else
                    fnmatch.fnmatchcase(field(row, 'name') or '', spec['pattern']))]
    ids = [row['database_id'] for row in selected]
    require(len(set(ids)) == len(ids), 'ambiguous_artifact_identity')
    # Match @actions/artifact 6.2.1 getArtifact/filterLatest: reruns can leave
    # multiple artifacts with one name, and the greatest numeric artifact ID
    # is authoritative. Never depend on response order, timestamps or the
    # caller's backend plan ID. attempt() freezes these winners before I/O;
    # a replacement or rollback on retry must fail, not pick another version.
    latest = {}
    for row in sorted(selected, key=lambda row: int(row['database_id']), reverse=True):
        latest.setdefault(row['name'], row)
    require(latest and len(latest) <= 100, 'artifact_selection_empty_or_large')
    return sorted(latest.values(), key=lambda row: int(row['database_id']))


def extract_archive(archive, destination):
    """Reject traversal, links, special files, duplicates and cross-artifact collisions."""
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        require(len(members) <= MAX_MEMBERS, 'archive_member_limit')
        expanded = sum(m.file_size for m in members)
        require(expanded <= MAX_EXPANDED_BYTES, 'archive_expansion_limit')
        require(shutil.disk_usage(destination).free > expanded + 32 * 1024**2, 'insufficient_disk_space')
        seen = set()
        for member in members:
            name = member.orig_filename
            parts = name.rstrip('/').split('/')
            require(name and '\0' not in name and '\\' not in name and ':' not in name
                    and not PurePosixPath(name).is_absolute()
                    and all(p not in ('', '.', '..') for p in parts), 'unsafe_archive_path')
            canonical = '/'.join(parts)
            require(canonical not in seen, 'duplicate_archive_path')
            seen.add(canonical)
            mode = stat.S_IFMT(member.external_attr >> 16)
            require(mode in (0, stat.S_IFREG, stat.S_IFDIR), 'unsafe_archive_type')
            require(not (member.flag_bits & 1), 'encrypted_archive')
            target = destination.joinpath(*parts)
            if member.is_dir():
                require(mode != stat.S_IFREG, 'invalid_archive_type')
                target.mkdir(parents=True, exist_ok=True)
            else:
                require(mode != stat.S_IFDIR, 'invalid_archive_type')
                target.parent.mkdir(parents=True, exist_ok=True)
                require(not target.exists(), 'artifact_path_collision')
                with source.open(member) as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst, 1024**2)


def transfer(row, url, archive, phase=lambda value: None):
    require(shutil.disk_usage(archive.parent).free > row['size'] + 32 * 1024**2,
            'insufficient_disk_space')
    total, digest = 0, hashlib.sha256()
    with open_url(url) as response, archive.open('xb') as output:
        length = response.headers.get('Content-Length')
        if length is not None:
            require(int(length) == row['size'], 'archive_length_mismatch')
        while True:
            chunk = response.read(1024**2)
            if not chunk:
                break
            total += len(chunk)
            require(total <= row['size'], 'archive_size_mismatch')
            digest.update(chunk)
            output.write(chunk)
    phase('verify')
    if total < row['size']:
        raise Failure('truncated_archive', True)
    require(digest.hexdigest() == row['digest'], 'digest_mismatch')


def attempt(spec, frozen, directory, send):
    token, service, identity = runtime_identity()
    send({'phase': 'metadata'})
    selected = select_artifacts(rpc(service, token, 'ListArtifacts', identity), spec)
    if frozen is not None:
        require(selected == frozen, 'artifact_identity_changed')
    else:
        send({'identity': selected})
    payload = directory / 'payload'
    payload.mkdir()
    for row in selected:
        send({'phase': 'url', 'artifact_id': row['database_id']})
        signed = rpc(service, token, 'GetSignedArtifactURL',
                     {key: row[key] for key in ('workflow_run_backend_id', 'workflow_job_run_backend_id', 'name')})
        url = field(signed, 'signed_url')
        require(isinstance(url, str), 'invalid_signed_url')
        send({'phase': 'download', 'artifact_id': row['database_id']})
        archive = directory / (row['database_id'] + '.zip')
        transfer(row, url, archive, lambda value: send({'phase': value, 'artifact_id': row['database_id']}))
        send({'phase': 'extract', 'artifact_id': row['database_id']})
        extract_archive(archive, payload)
        archive.unlink()


def worker(spec, frozen, directory, channel, operation):
    os.setsid()
    os.umask(0o022)  # Only this worker; extracted files must not inherit group write access.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        operation(spec, frozen, directory, channel.send)
        channel.send({'done': True})
    except Exception as exc:
        failure = classify(exc)
        channel.send({'done': False, 'reason': failure.reason, 'retryable': failure.retryable})
    finally:
        channel.close()


def stop_worker(process):
    if process.pid is None:
        return
    # Reap before removing staging or starting another request. No writes survive.
    if process.is_alive():
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.terminate()  # Cancellation can race with setsid at worker startup.
        process.join(1)
    if process.is_alive():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            process.kill()
        process.join(5)
    require(not process.is_alive(), 'worker_did_not_stop')
    process.join()


def log_event(number, started, **values):
    print(json.dumps({'artifact_attempt': number, 'elapsed_seconds': round(time.monotonic() - started, 3),
                      **values}, sort_keys=True), flush=True)


def download(spec, destination, *, seconds=ATTEMPT_SECONDS, delays=BACKOFF_SECONDS,
             operation=attempt, sleep=time.sleep):
    require(bool(spec['name']) != bool(spec['pattern']), 'select_name_or_pattern')
    destination = Path(destination).absolute()
    require(destination.name not in ('', '.', '..') and not os.path.lexists(destination),
            'destination_must_be_new')
    destination.parent.mkdir(parents=True, exist_ok=True)
    frozen = None
    context = multiprocessing.get_context('fork')
    root = tempfile.mkdtemp(prefix='.kuasar-artifact-', dir=destination.parent)
    stopped = True
    try:
        for number in range(1, len(delays) + 2):
            directory = Path(tempfile.mkdtemp(prefix='attempt-', dir=root))
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=worker,
                args=(spec, frozen, directory, sender, operation))
            started = time.monotonic()  # Covers metadata, headers, body, hash and extract.
            result = None
            try:
                stopped = False
                process.start()
                sender.close()
                deadline = started + seconds
                while time.monotonic() < deadline:
                    if receiver.poll(min(0.1, max(0, deadline - time.monotonic()))):
                        try:
                            message = receiver.recv()
                        except EOFError:
                            break
                        if 'identity' in message:
                            frozen = message['identity']
                        elif 'done' in message:
                            result = message
                            break
                        else:
                            log_event(number, started, **message)
                    if not process.is_alive():
                        break
                # Drain already emitted terminal errors before classifying timeout.
                while receiver.poll():
                    try:
                        message = receiver.recv()
                    except EOFError:
                        break
                    if 'identity' in message:
                        frozen = message['identity']
                    if 'done' in message:
                        result = message
                if result is None:
                    result = {'done': False, 'reason': 'attempt_deadline' if process.is_alive()
                              else 'worker_failed', 'retryable': process.is_alive()}
                if result['done'] and time.monotonic() >= deadline:
                    result = {'done': False, 'reason': 'attempt_deadline', 'retryable': True}
            finally:
                stop_worker(process)
                stopped = True
                receiver.close()
                sender.close()
            if result['done']:
                require(not os.path.lexists(destination), 'destination_must_be_new')
                os.rename(directory / 'payload', destination)
                log_event(number, started, phase='complete')
                return
            log_event(number, started, phase='failed', reason=result['reason'])
            shutil.rmtree(directory)
            if not result['retryable'] or number > len(delays):
                raise Failure(result['reason'])
            log_event(number, started, phase='backoff', seconds=delays[number - 1])
            sleep(delays[number - 1])
    finally:
        if stopped:
            shutil.rmtree(root)


def main():
    def cancel(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    try:
        download({'name': os.environ.get('INPUT_NAME', ''),
                  'pattern': os.environ.get('INPUT_PATTERN', '')},
                 os.environ.get('INPUT_PATH', ''))
    except KeyboardInterrupt:
        print('artifact: canceled', flush=True)
        return 130
    except Exception as exc:
        print('artifact: ' + classify(exc).reason, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
