#!/usr/bin/env python3
"""Publish the tested OCI bytes with their precomputed immutable index identity."""
import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

import archive_paths
import node_infra_assets as node
import selection
import workbench_registry as registry


def validate_stage(bundle, version, revision, plan, validation, workbench):
    node.require(plan['baseline'].get('delivery') in selection.NODE_INFRA_DELIVERIES
                 and plan['baseline']['version'] == version and plan['baseline']['sha'] == revision,
                 'node-infra publication differs from the admitted aggregate')
    spec = importlib.util.spec_from_file_location('node_infra_publication_checks', Path(__file__).with_name('bind-validation.py'))
    checks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checks)
    checks.verify_stage(bundle, plan, validation, workbench)
    document = node.validate_set(bundle / 'assets', version, revision, receipts=bundle / 'node-infra')
    node.require(document['components'] == plan['baseline']['units'], 'node-infra image components differ from selected aggregate')
    return document


def layout_from_archives(bundle, document, destination):
    """Merge only verified content-addressed OCI blobs, never image root files."""
    blobs = destination / 'blobs/sha256'
    blobs.mkdir(parents=True)
    descriptors = []
    for arch, image in sorted(document['images'].items()):
        archive = bundle / 'assets' / image['archive']['name']
        with tarfile.open(archive, 'r:gz') as source:
            for member in source:
                name = archive_paths.relative_name(member)
                if not member.isfile() or not name.startswith('blobs/sha256/'):
                    continue
                target = destination / name
                if target.exists():
                    node.require(node.digest(target) == target.name, 'conflicting OCI blob in publication layout')
                    continue
                with source.extractfile(member) as data, target.open('xb') as output:
                    shutil.copyfileobj(data, output, 1024 * 1024)
        descriptors.append({'mediaType': image['media_type'], 'digest': image['manifest_digest'],
                            'size': image['manifest_size'], 'annotations': {'org.opencontainers.image.ref.name': arch}})
    index = node.index_bytes(document['images'])
    (blobs / document['index_digest'].removeprefix('sha256:')).write_bytes(index)
    descriptors.append({'mediaType': node.OCI_INDEX, 'digest': document['index_digest'], 'size': len(index),
                        'annotations': {'org.opencontainers.image.ref.name': 'aggregate'}})
    (destination / 'oci-layout').write_bytes(node.canonical({'imageLayoutVersion': '1.0.0'}))
    (destination / 'index.json').write_bytes(node.canonical({'schemaVersion': 2, 'manifests': descriptors}))


def publish(bundle, version, revision, plan, validation, workbench, *, read_only=False):
    document = validate_stage(bundle, version, revision, plan, validation, workbench)
    token, actor = os.environ.get('GH_TOKEN', ''), os.environ.get('GITHUB_ACTOR', '')
    node.require(token and actor, 'node-infra registry publication needs the job package-write identity')
    reference = node.REPOSITORY + ':' + version
    with tempfile.TemporaryDirectory(prefix='node-infra-registry-') as temporary:
        root = Path(temporary)
        auth = root / 'auth.json'
        auth.write_text(json.dumps({'auths': {'ghcr.io': {'auth': base64.b64encode((actor + ':' + token).encode()).decode()}}}))
        auth.chmod(0o600)
        anonymous = root / 'anonymous.json'
        anonymous.write_text('{"auths":{}}\n')
        layout = root / 'oci'
        layout_from_archives(bundle, document, layout)
        for arch, image in document['images'].items():
            tag = reference + '-' + node.ARCHES[arch]
            raw = registry.inspect_raw(tag, auth, missing=True)
            if raw is None:
                node.require(not read_only, 'published aggregate is missing its node-infra image')
                subprocess.run(['skopeo', 'copy', '--preserve-digests', '--authfile', str(auth),
                                'oci:' + str(layout) + ':' + arch, 'docker://' + tag], check=True, timeout=1800)
                raw = registry.inspect_raw(tag, auth)
            node.require(registry.digest(raw) == image['manifest_digest'], 'existing node-infra native tag has different bytes')
            immutable = image['reference']
            node.require(registry.inspect_raw(immutable, anonymous) == raw,
                         'node-infra native manifest is not anonymously readable with identical bytes')
            config = registry.inspect_raw(immutable, anonymous, config=True)
            node.require(registry.digest(config) == image['config_digest'], 'published node-infra config differs from tested image')
            # Read all referenced layer bytes, not only an index/config receipt.
            readback = root / 'readback.tar'
            subprocess.run(['skopeo', 'copy', '--preserve-digests', '--authfile', str(anonymous),
                            'docker://' + immutable, 'oci-archive:' + str(readback)], check=True, timeout=1800)
            observed = node.inspect_oci(readback, arch, version=version, revision=revision)
            node.require(observed['manifest_digest'] == image['manifest_digest']
                         and observed['image_id'] == image['id'], 'public node-infra layer/config identity differs')
            readback.unlink()
        raw = registry.inspect_raw(reference, auth, missing=True)
        if raw is None:
            node.require(not read_only, 'published aggregate is missing its node-infra index')
            subprocess.run(['skopeo', 'copy', '--all', '--preserve-digests', '--authfile', str(auth),
                            'oci:' + str(layout) + ':aggregate', 'docker://' + reference], check=True, timeout=1800)
            raw = registry.inspect_raw(reference, auth)
        node.require(raw == node.index_bytes(document['images']) and registry.digest(raw) == document['index_digest'],
                     'node-infra aggregate tag differs from the tested deployment index')
        node.require(registry.inspect_raw(reference, anonymous) == raw, 'node-infra aggregate index is not public')
    return {'reference': reference, 'digest': document['index_digest'], 'images': document['images']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('version')
    parser.add_argument('revision')
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--validation', type=Path, required=True)
    parser.add_argument('--workbench', type=Path, required=True)
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--read-only', action='store_true')
    args = parser.parse_args()
    inputs = (json.loads(args.plan.read_text()), json.loads(args.validation.read_text()),
              {arch: json.loads((args.workbench / f'{arch}.json').read_text()) for arch in node.ARCHES})
    if args.validate_only:
        result = validate_stage(args.bundle, args.version, args.revision, *inputs)
        print(json.dumps({'mode': 'validation-only', 'conclusion': 'success', 'index_digest': result['index_digest']}))
    else:
        print(json.dumps(publish(args.bundle, args.version, args.revision, *inputs, read_only=args.read_only), sort_keys=True))
