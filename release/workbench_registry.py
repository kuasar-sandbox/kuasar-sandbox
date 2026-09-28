"""Publish verified workbench archives without rebuilding or replacing image tags."""
import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import selection
import workbench_assets

REPOSITORY = 'ghcr.io/kuasar-sandbox/workbench'
PLATFORMS = {'x86_64': 'amd64', 'aarch64': 'arm64'}
MARKER = '<!-- kuasar-integration-validation '


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def inspect_raw(reference, auth, *, config=False, missing=False):
    command = ['skopeo', 'inspect', '--authfile', str(auth), '--config' if config else '--raw', 'docker://' + reference]
    result = subprocess.run(command, capture_output=True, timeout=120)
    if result.returncode:
        error = result.stderr.decode(errors='replace')
        # Authentication, transport and registry failures must not authorize a
        # same-name write. Only an explicit registry absence can do that.
        if missing and ('manifest unknown' in error.lower() or 'name unknown' in error.lower()):
            return None
        raise ValueError(f'cannot inspect registry reference {reference}: {error}')
    return result.stdout


def check_manifest(raw, config, receipt):
    manifest, image = json.loads(raw), json.loads(config)
    require(manifest.get('schemaVersion') == 2 and 'layers' in manifest, 'registry image is not a schema-2 manifest')
    require(manifest.get('config', {}).get('digest') == receipt['image_id'] == digest(config),
            'registry config differs from the verified offline archive')
    require(manifest['config'].get('size') == len(config), 'registry config descriptor size differs')
    require(image.get('architecture') == PLATFORMS[receipt['arch']] and image.get('os') == 'linux',
            'registry image platform differs from offline archive')
    labels = image.get('config', {}).get('Labels', {})
    require(labels.get('org.opencontainers.image.revision') == receipt['source_revision']
            and labels.get('org.opencontainers.image.version') == receipt['aggregate_version'],
            'registry image source/version differs')
    return {'digest': digest(raw), 'size': len(raw), 'image_id': receipt['image_id'], 'archive_sha256': receipt['sha256']}


def check_index(raw, children):
    index = json.loads(raw)
    require(index.get('schemaVersion') == 2 and len(index.get('manifests', [])) == 2,
            'workbench registry index must contain exactly both architectures')
    observed = {}
    for descriptor in index['manifests']:
        platform = descriptor.get('platform', {})
        arch = platform.get('architecture')
        require(platform.get('os') == 'linux' and arch in PLATFORMS.values() and arch not in observed,
                'registry index has an unexpected or duplicate platform')
        observed[arch] = (descriptor.get('digest'), descriptor.get('size'))
    expected = {PLATFORMS[arch]: (record['digest'], record['size']) for arch, record in children.items()}
    require(observed == expected, 'registry index does not select the exact verified native images')
    return digest(raw)


def publish(bundle, version, revision):
    require(selection.AGGREGATE_RE.fullmatch(version), "invalid aggregate version")
    receipts = {arch: workbench_assets.validate(bundle / 'assets', version, arch, revision,
                                               receipt_directory=bundle / 'workbench') for arch in PLATFORMS}
    notes = bundle / 'release-notes.md'
    text = notes.read_text()
    require(text.count(MARKER) == 1, 'registry publication requires the exact successful validation binding')
    prefix, marker = text.split(MARKER)
    encoded, suffix = marker.split(' -->', 1)
    binding = json.loads(encoded)
    require(binding.get('delivery') == 'workbench-v1' and binding.get('aggregate_sha') == revision,
            'registry publication validation has another contract or source')
    for arch, receipt in receipts.items():
        result = binding.get('workbench', {}).get(arch, {})
        require(result.get('conclusion') == 'success' and result.get('image_id') == receipt['image_id']
                and result.get('sha256') == receipt['sha256'], 'registry image has not passed staged workbench validation')
    token = os.environ.get('GH_TOKEN', '')
    actor = os.environ.get('GITHUB_ACTOR', '')
    require(token and actor, 'registry publication needs the job package-write identity')
    reference = REPOSITORY + ':' + version.removeprefix('release-')
    with tempfile.TemporaryDirectory(prefix='workbench-registry-') as directory:
        root = Path(directory)
        auth = root / 'config.json'
        auth.write_text(json.dumps({'auths': {'ghcr.io': {'auth': base64.b64encode((actor + ':' + token).encode()).decode()}}}))
        auth.chmod(0o600)
        anonymous = root / 'anonymous.json'
        anonymous.write_text('{"auths":{}}\n')
        children = {}
        for arch, receipt in receipts.items():
            tag = reference + '-' + arch
            raw = inspect_raw(tag, auth, missing=True)
            if raw is None:
                archive = root / 'image.tar'
                # skopeo consumes the saved image, never a rebuilt Dockerfile.
                with gzip.open(bundle / 'assets' / receipt['archive'], 'rb') as source, archive.open('wb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                subprocess.run(['skopeo', 'copy', '--authfile', str(auth), 'docker-archive:' + str(archive),
                                'docker://' + tag], check=True, timeout=1800)
                archive.unlink()
                raw = inspect_raw(tag, auth)
            children[arch] = check_manifest(raw, inspect_raw(tag, auth, config=True), receipt)
            # No private-only image is a public delivery. This also verifies
            # the content-addressed reference independently of mutable tags.
            immutable = REPOSITORY + '@' + children[arch]['digest']
            public = inspect_raw(immutable, anonymous)
            require(public == raw, 'public registry manifest differs from the published bytes')
            check_manifest(public, inspect_raw(immutable, anonymous, config=True), receipt)
        raw = inspect_raw(reference, auth, missing=True)
        if raw is None:
            environment = dict(os.environ, DOCKER_CONFIG=str(root))
            subprocess.run(['docker', 'manifest', 'create', reference,
                            *(REPOSITORY + '@' + row['digest'] for row in children.values())],
                           check=True, env=environment, timeout=120)
            subprocess.run(['docker', 'manifest', 'push', reference], check=True, env=environment, timeout=300)
            raw = inspect_raw(reference, auth)
        identity = check_index(raw, children)
        require(inspect_raw(reference, anonymous) == raw, 'aggregate registry tag is not publicly readable with identical bytes')
        proof = {'reference': reference, 'digest': identity, 'architectures': children}
    require('registry' not in binding or binding['registry'] == proof, 'existing validation names different registry bytes')
    binding['registry'] = proof
    notes.write_text(prefix + MARKER + json.dumps(binding, sort_keys=True, separators=(',', ':')) + ' -->' + suffix)
    return proof


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('version')
    parser.add_argument('revision')
    args = parser.parse_args()
    print(json.dumps(publish(args.bundle, args.version, args.revision), sort_keys=True))
