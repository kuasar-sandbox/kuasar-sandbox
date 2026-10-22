#!/usr/bin/env python3
"""Exercise the real Docker image store with a private, layer-free fixture."""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import uuid

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('image_store_workbench', ROOT / 'ci/hosted/workbench.py')
workbench = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = workbench
spec.loader.exec_module(workbench)
MANIFEST = 'application/vnd.docker.distribution.manifest.v2+json'
CONFIG = 'application/vnd.docker.container.image.v1+json'
OWNER_LABEL = 'org.kuasar.ci.image-store-probe'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def fixture(arch, nonce):
    """Both archive layouts describe exactly the same config, with no layers."""
    tag = 'localhost/kuasar-image-store-probe:' + nonce
    version, revision = 'release-v0.0.0-preview.20000101.1', '0' * 40
    config = canonical({'architecture': workbench.PLATFORMS[arch], 'os': 'linux',
                        'config': {'Labels': {OWNER_LABEL: nonce,
                            'org.opencontainers.image.version': version,
                            'org.opencontainers.image.revision': revision,
                            'org.opencontainers.image.source': 'https://github.com/kuasar-sandbox/kuasar-sandbox'}},
                        'rootfs': {'type': 'layers', 'diff_ids': []}, 'history': []})
    manifest = canonical({'schemaVersion': 2, 'mediaType': MANIFEST,
                          'config': {'mediaType': CONFIG, 'digest': digest(config), 'size': len(config)},
                          'layers': []})
    config_path = 'blobs/sha256/' + digest(config).split(':')[1]
    manifest_path = 'blobs/sha256/' + digest(manifest).split(':')[1]
    descriptor = {'mediaType': MANIFEST, 'digest': digest(manifest), 'size': len(manifest),
                  'platform': {'architecture': workbench.PLATFORMS[arch], 'os': 'linux'},
                  'annotations': {'io.containerd.image.name': tag,
                                  'org.opencontainers.image.ref.name': tag}}
    entries = {config_path: config, manifest_path: manifest,
               'oci-layout': canonical({'imageLayoutVersion': '1.0.0'}),
               'index.json': canonical({'schemaVersion': 2, 'manifests': [descriptor]}),
               'manifest.json': canonical([{'Config': config_path, 'RepoTags': [tag], 'Layers': []}])}
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for name, raw in entries.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(raw), 0o644
            archive.addfile(info, io.BytesIO(raw))
    selection = {'aggregate_version': version, 'source_revision': revision,
                 'architectures': {arch: {'image_id': digest(config),
                     'reference': workbench.REGISTRY + '@' + digest(manifest)}}}
    facts = {'tag': tag, 'nonce': nonce, 'config_digest': digest(config),
             'manifest_digest': digest(manifest), 'config_raw': config.decode(),
             'manifest_raw': manifest.decode(), 'archive_size': len(stream.getvalue())}
    return stream.getvalue(), selection, facts


def command(*arguments):
    completed = subprocess.run(['docker', *arguments], text=True, capture_output=True, timeout=60)
    return {'exit_code': completed.returncode, 'stdout': completed.stdout, 'stderr': completed.stderr}


def successful(record, description):
    if record['exit_code']:
        raise ValueError(description + ': ' + record['stderr'].strip())
    return record['stdout']


def inspect(identity):
    record = command('image', 'inspect', identity)
    record['identity'] = identity
    return record


def cleanup_fixture(facts):
    current = inspect(facts['tag'])
    if current['exit_code']:
        if any(text in current['stderr'] for text in ('No such', 'not found')):
            return {'exit_code': 0, 'absent': True}
        return current
    images = json.loads(current['stdout'])
    if len(images) != 1 or images[0]['Config'].get('Labels', {}).get(OWNER_LABEL) != facts['nonce']:
        return {'exit_code': 1, 'error': 'refusing to remove a foreign fixture image'}
    return command('image', 'rm', facts['tag'])


def check_fixture(selection, arch, facts, inspections):
    # A config-addressed store and a manifest-addressed store must each prove
    # their own exact identity. Neither a matching tag nor either-ID equality
    # alone is sufficient for a manifest-addressed image.
    chosen = inspections['config'] if inspections['config']['exit_code'] == 0 else inspections['manifest']
    images = json.loads(successful(chosen, 'inspect immutable fixture identity'))
    if len(images) != 1:
        raise ValueError('ambiguous fixture identity')
    return workbench.image_check(selection, arch, images[0], metadata=(
        facts['manifest_raw'].encode(), facts['config_raw'].encode()))


def pull_fixture(arch, report):
    """Serve only a second, private zero-layer fixture on loopback; no containers."""
    _, selection, facts = fixture(arch, uuid.uuid4().hex)
    report.update(fixture=facts, inspections={})
    manifest, config = facts['manifest_raw'].encode(), facts['config_raw'].encode()
    prefix = '/v2/kuasar-image-store-probe/'
    resources = {'/v2/': (b'{}', 'application/json'),
                 prefix + 'manifests/' + facts['manifest_digest']: (manifest, MANIFEST),
                 prefix + 'blobs/' + facts['config_digest']: (config, CONFIG)}

    class Registry(BaseHTTPRequestHandler):
        def handle(self):
            # Docker's localhost resolver tries HTTPS first and recognizes a
            # plaintext HTTP status line before using its existing HTTP fallback.
            # BaseHTTPRequestHandler otherwise emits an HTTP/0.9 error body for
            # a ClientHello, which the containerd resolver cannot recognize.
            self.connection.settimeout(5)
            if self.connection.recv(1, socket.MSG_PEEK) == b'\x16':
                header = self.rfile.read(5)
                if len(header) == 5:
                    self.rfile.read(int.from_bytes(header[3:5], 'big'))
                self.connection.sendall(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                return
            super().handle()

        def do_GET(self):
            path = self.path.split('?')[0]
            if path not in resources:
                self.send_error(404)
                return
            raw, media_type = resources[path]
            self.send_response(200)
            self.send_header('Content-Type', media_type)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Docker-Distribution-API-Version', 'registry/2.0')
            self.send_header('Docker-Content-Digest', digest(raw))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(raw)

        do_HEAD = do_GET

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Registry)
    reference = '127.0.0.1:' + str(server.server_port) + '/kuasar-image-store-probe@' + facts['manifest_digest']
    facts['tag'] = reference  # Owned cleanup is restricted to this private digest reference.
    selection['architectures'][arch]['reference'] = reference
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    owned = False
    try:
        existing = inspect(reference)
        if existing['exit_code'] == 0 or not any(
                text in existing['stderr'] for text in ('No such', 'not found')):
            raise ValueError('private pull fixture name is not confirmed absent')
        owned = True
        report['acquire'] = command('image', 'pull', reference)
        successful(report['acquire'], 'pull private fixture')
        for name, identity in [('tag', reference), ('config', facts['config_digest']),
                               ('manifest', facts['manifest_digest'])]:
            report['inspections'][name] = inspect(identity)
        report['docker_image_id'] = check_fixture(selection, arch, facts, report['inspections'])
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        if owned:
            try:
                report['cleanup'] = cleanup_fixture(facts)
            except Exception as error:
                report['cleanup'] = {'error': str(error), 'exit_code': None}
            if report['cleanup']['exit_code'] != 0:
                raise ValueError('private pull fixture cleanup failed')


def run_probe(output):
    if output.exists() or output.is_symlink():
        raise ValueError('probe evidence already exists')
    arch, nonce = platform.machine(), uuid.uuid4().hex
    if arch not in workbench.PLATFORMS:
        raise ValueError('probe requires a supported native architecture')
    archive, selection, facts = fixture(arch, nonce)
    report = {'schema': 1, 'kind': 'image-store-regression-fixture', 'conclusion': 'failure',
              'arch': arch, 'fixture': facts, 'inspections': {},
              'runner': {key: os.environ.get(key) for key in
                         ('ImageOS', 'ImageVersion', 'RUNNER_OS', 'RUNNER_ARCH',
                          'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_SHA')}}
    owned = False
    try:
        report['version'] = json.loads(successful(command('version', '--format', '{{json .}}'), 'docker version'))
        info = json.loads(successful(command('info', '--format', '{{json .}}'), 'docker info'))
        report['store'] = {key: info.get(key) for key in
                           ('ServerVersion', 'Driver', 'DriverStatus', 'OSType', 'Architecture',
                            'OperatingSystem', 'KernelVersion')}
        existing = inspect(facts['tag'])
        if existing['exit_code'] == 0 or not any(
                text in existing['stderr'] for text in ('No such', 'not found')):
            raise ValueError('private fixture name is not confirmed absent')
        owned = True
        with tempfile.TemporaryDirectory(prefix='image-store-probe-') as directory:
            path = Path(directory) / 'fixture.tar'
            path.write_bytes(archive)
            report['load'] = command('image', 'load', '--input', str(path))
            successful(report['load'], 'load private fixture')
        for name, identity in [('tag', facts['tag']), ('config', facts['config_digest']),
                               ('manifest', facts['manifest_digest'])]:
            report['inspections'][name] = inspect(identity)
        tagged = json.loads(successful(report['inspections']['tag'], 'inspect private fixture'))
        if len(tagged) != 1 or tagged[0]['Config'].get('Labels', {}).get(OWNER_LABEL) != nonce:
            raise ValueError('loaded image does not belong to this probe')
        report['docker_image_id'] = check_fixture(selection, arch, facts, report['inspections'])
        report['pull'] = {}
        pull_fixture(arch, report['pull'])
        report['conclusion'] = 'success'
    except Exception as error:
        report['error'] = {'type': type(error).__name__, 'message': str(error)}
    finally:
        if owned:
            try:
                report['cleanup'] = cleanup_fixture(facts)
            except Exception as error:
                report['cleanup'] = {'error': str(error), 'exit_code': None}
            if report['cleanup']['exit_code'] != 0:
                report['conclusion'] = 'failure'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(canonical(report) + b'\n')
    print(json.dumps({'conclusion': report['conclusion'], 'evidence': str(output),
                      'error': report.get('error')}, sort_keys=True))
    return 0 if report['conclusion'] == 'success' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(run_probe(args.output))
