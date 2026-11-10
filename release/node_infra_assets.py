#!/usr/bin/env python3
"""Validate node-infra OCI bytes and assemble the matching host deployment package.

The six component units retain their own versions. This module only packages
the main repository's aggregate deliverable, including its exact image identity.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import sys
import tarfile

import archive_paths
import selection

ARCHES = {'x86_64': 'amd64', 'aarch64': 'arm64'}
REPOSITORY = 'ghcr.io/kuasar-sandbox/node-infra'
OCI_INDEX = 'application/vnd.oci.image.index.v1+json'
OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(data)
    return value.hexdigest()


def validate_components(components):
    require(isinstance(components, dict) and set(components) == set(selection.UNITS),
            'node-infra requires the six selected component units')
    for unit, record in components.items():
        owner = 'guest-runtime' if unit in ('runtime', 'vmlinux') else unit
        require(isinstance(record, dict) and set(record) == {'version', 'repository', 'sha'}
                and record['repository'] == 'kuasar-sandbox/' + owner
                and re.fullmatch(r'[0-9a-f]{40}', record.get('sha', ''))
                and isinstance(record.get('version'), str) and record['version'],
                'invalid node-infra component identity: ' + unit)
    return components


def require_config_sources(sources):
    """Fail early for the published tuple that predates native YAML layering.

    The source check is an input guard, not a substitute for the deployment E2E
    that exercises these capabilities in the selected binaries.
    """
    for relative, feature in [('orchestrator/config/files.go', 'func mergeYAMLFiles('),
                              ('connector/pkg/vswitch/config_file.go', 'func parseConfigYAML(')]:
        path = sources / relative
        require(path.is_file() and feature in path.read_text(),
                'selected component source predates node-infra native configuration support: ' + relative)


def inspect_oci(path, arch, *, version=None, revision=None):
    """Read an OCI save archive without extracting layers or starting a runtime."""
    require(arch in ARCHES, 'unsupported node-infra architecture')
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), 'missing regular node-infra OCI archive')
    with tarfile.open(path, 'r:*') as archive:
        members = {}
        for member in archive:
            name = archive_paths.relative_name(member)
            require(name not in members and (member.isfile() or member.isdir()),
                    'unsafe or duplicate node-infra OCI archive member: ' + name)
            members[name] = member
        used = set()

        def read(name, limit=16 * 1024 * 1024):
            require(name in members and members[name].isfile() and members[name].size <= limit,
                    'missing or oversized OCI metadata: ' + name)
            used.add(name)
            return archive.extractfile(members[name]).read()

        def blob(descriptor, *, metadata=False):
            identity, size = descriptor.get('digest'), descriptor.get('size')
            require(isinstance(identity, str) and DIGEST.fullmatch(identity)
                    and type(size) is int and size >= 0, 'invalid OCI descriptor')
            name = 'blobs/sha256/' + identity.removeprefix('sha256:')
            require(name in members and members[name].isfile() and members[name].size == size,
                    'OCI descriptor size does not match its blob')
            used.add(name)
            actual = hashlib.sha256()
            chunks = []
            require(not metadata or size <= 16 * 1024 * 1024, 'oversized OCI metadata blob')
            with archive.extractfile(members[name]) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    actual.update(chunk)
                    if metadata:
                        chunks.append(chunk)
            require('sha256:' + actual.hexdigest() == identity, 'OCI blob digest differs from its descriptor')
            return b''.join(chunks) if metadata else None

        require(json.loads(read('oci-layout')) == {'imageLayoutVersion': '1.0.0'}, 'unsupported OCI layout')
        index = json.loads(read('index.json'))
        require(index.get('schemaVersion') == 2 and len(index.get('manifests', [])) == 1,
                'native node-infra archive must contain exactly one image')
        descriptor = index['manifests'][0]
        require(descriptor.get('mediaType') == OCI_MANIFEST, 'node-infra archive requires an OCI image manifest')
        manifest_raw = blob(descriptor, metadata=True)
        manifest = json.loads(manifest_raw)
        require(manifest.get('schemaVersion') == 2 and manifest.get('mediaType') == OCI_MANIFEST,
                'invalid OCI image manifest')
        config_raw = blob(manifest.get('config', {}), metadata=True)
        config = json.loads(config_raw)
        require(config.get('os') == 'linux' and config.get('architecture') == ARCHES[arch],
                'node-infra OCI image has another platform')
        layers = manifest.get('layers')
        require(isinstance(layers, list) and layers
                and len(layers) == len(config.get('rootfs', {}).get('diff_ids', [])),
                'node-infra OCI layer/config count differs')
        for layer, expected_diff in zip(layers, config['rootfs']['diff_ids']):
            blob(layer)
            name = 'blobs/sha256/' + layer['digest'][7:]
            with archive.extractfile(members[name]) as raw:
                stream = gzip.GzipFile(fileobj=raw) if layer.get('mediaType', '').endswith('+gzip') else raw
                require('sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest() == expected_diff,
                        'OCI layer diffID differs from image config')
        require({name for name, member in members.items() if member.isfile()} == used,
                'unreferenced files in node-infra OCI archive')
        labels = config.get('config', {}).get('Labels', {})
        if version is not None:
            require(labels.get('org.opencontainers.image.version') == version, 'node-infra image version differs')
        if revision is not None:
            require(labels.get('org.opencontainers.image.revision') == revision, 'node-infra image source differs')
        return {'image_id': manifest['config']['digest'], 'manifest_digest': descriptor['digest'],
                'manifest_size': len(manifest_raw), 'media_type': OCI_MANIFEST}


def inspect_docker(path, arch, *, image_id, version, revision):
    """Verify Docker save bytes against the SAME OCI configuration and rootfs.

    Transport conversion may change a manifest; the configuration hash binds
    platform, ordered uncompressed layer diffIDs, labels and component inputs.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'test/e2e/lib'))
    import workspace
    verified = workspace.verify_image_archive(path, 'linux/' + ARCHES[arch])
    require(verified['image_id'] == image_id, 'Docker configuration differs from OCI image')
    with tarfile.open(path, 'r:*') as archive:
        manifest = json.load(archive.extractfile('manifest.json'))
        config = json.load(archive.extractfile(manifest[0]['Config']))
    labels = config.get('config', {}).get('Labels', {})
    require(labels.get('org.opencontainers.image.version') == version and
            labels.get('org.opencontainers.image.revision') == revision, 'Docker source/version differs')
    return {'image_id': image_id, 'diff_ids': config['rootfs']['diff_ids']}


def validate(directory, version, arch, revision, *, receipts=None):
    name = selection.node_infra_archive(version, arch)
    receipt = (receipts or directory) / f'node-infra-{arch}.json'
    require(receipt.is_file() and not receipt.is_symlink(), 'missing node-infra image receipt')
    record = json.loads(receipt.read_text())
    require(record.get('archive') == name and record.get('arch') == arch
            and record.get('aggregate_version') == version and record.get('source_revision') == revision,
            'node-infra receipt source/version/architecture differs')
    path = directory / name
    require(type(record.get('size')) is int and record['size'] > 0
            and path.stat().st_size == record['size'] and digest(path) == record.get('sha256'),
            'node-infra archive differs from its receipt')
    require(all(record.get(key) == value for key, value in inspect_oci(path, arch, version=version, revision=revision).items()),
            'node-infra image identity differs from its receipt')
    validate_components(record.get('components'))
    if 'docker_archive' in record:
        docker = record['docker_archive']
        path = directory / selection.node_infra_docker_archive(version, arch)
        require(docker.get('name') == path.name and path.is_file() and not path.is_symlink()
                and docker.get('size') == path.stat().st_size and docker.get('sha256') == digest(path),
                'Docker archive differs from receipt')
        inspect_docker(path, arch, image_id=record['image_id'], version=version, revision=revision)
    return record


def index_bytes(images):
    require(images and set(images) <= set(ARCHES), 'invalid node-infra image architecture set')
    return canonical({'schemaVersion': 2, 'mediaType': OCI_INDEX, 'manifests': [
        {'mediaType': images[arch]['media_type'], 'digest': images[arch]['manifest_digest'],
         'size': images[arch]['manifest_size'], 'platform': {'os': 'linux', 'architecture': ARCHES[arch]}}
        for arch in sorted(images)]})


def release_document(version, revision, records):
    require(re.fullmatch(r'[0-9a-f]{40}', revision), 'node-infra needs an exact platform source')
    components = next(iter(records.values()))['components']
    require(all(row['components'] == components for row in records.values()),
            'node-infra architectures select different components')
    images = {arch: {'reference': REPOSITORY + '@' + record['manifest_digest'],
                    'id': record['image_id'], 'config_digest': record['image_id'],
                    **{key: record[key] for key in ('manifest_digest', 'manifest_size', 'media_type')},
                    'archive': {'name': record['archive'], 'sha256': record['sha256'], 'size': record['size']}}
              for arch, record in records.items()}
    docker = ['docker_archive' in record for record in records.values()]
    require(all(docker) or not any(docker), 'node-infra archive contracts differ by architecture')
    if all(docker):
        for arch in images:
            images[arch]['docker_archive'] = records[arch]['docker_archive']
    return {'schema': 2 if all(docker) else 1, 'version': version, 'source_revision': revision, 'repository': REPOSITORY,
            'index_digest': 'sha256:' + hashlib.sha256(index_bytes(images)).hexdigest(),
            'images': images, 'components': components,
            'host': {'podman_min_version': '4.9.3', 'cgroup_version': 2}}


def package(platform, directory, output, version, revision, *, arches=tuple(ARCHES)):
    """Create one architecture-independent deployment archive from exact sources."""
    records = {arch: validate(directory, version, arch, revision) for arch in arches}
    document = release_document(version, revision, records)
    output.mkdir(parents=True, exist_ok=True)
    target = output / selection.node_infra_deploy_archive(version)
    require(not target.exists(), 'node-infra deployment stage already exists')
    entries = {}
    for prefix, root in [('bin', platform / 'node-infra/deploy/bin'),
                         ('templates', platform / 'node-infra/deploy/templates'),
                         ('examples', platform / 'node-infra/examples')]:
        require(root.is_dir() and not root.is_symlink(), 'missing node-infra deployment source: ' + str(root))
        for path in root.rglob('*'):
            require(not path.is_symlink(), 'deployment package cannot include symlinks')
            if path.is_file():
                entries[prefix + '/' + str(path.relative_to(root))] = (path.read_bytes(), 0o755 if path.stat().st_mode & 0o111 else 0o644)
    for target_name, source in [('README.md', 'docs/node-infra.md'), ('README_zh.md', 'docs/node-infra_zh.md')]:
        path = platform / source
        require(path.is_file() and not path.is_symlink(), 'missing node-infra deployment guide: ' + source)
        text = path.read_text().replace('(node-infra.md)', '(README.md)').replace('(node-infra_zh.md)', '(README_zh.md)')
        entries[target_name] = (text.encode(), 0o644)
    require('bin/node-infra-ctl' in entries and 'templates/node-infra.container' in entries,
            'incomplete node-infra deployment payload')
    entries['release.json'] = (canonical(document) + b'\n', 0o644)
    with target.open('xb') as raw, gzip.GzipFile(fileobj=raw, filename='', mode='wb', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as archive:
            for name, (data, mode) in sorted(entries.items()):
                entry = tarfile.TarInfo(name)
                entry.size, entry.mode = len(data), mode
                archive.addfile(entry, io.BytesIO(data))
    (output / 'node-infra-index.json').write_bytes(index_bytes(document['images']))
    return document


def read_deploy(path, version, revision=None, *, arches=None):
    seen = archive_paths.inspect(path, {})
    require({'release.json', 'bin/node-infra-ctl', 'templates/node-infra.container'} <= set(seen),
            'incomplete node-infra deployment archive')
    with tarfile.open(path, 'r:gz') as archive:
        entry = next(member for member in archive if archive_paths.relative_name(member) == 'release.json')
        require(entry.size < 1024 * 1024, 'oversized node-infra release.json')
        document = json.load(archive.extractfile(entry))
    require(document.get('schema') in (1, 2) and document.get('version') == version
            and document.get('repository') == REPOSITORY, 'invalid node-infra deployment release identity')
    require(re.fullmatch(r'[0-9a-f]{40}', document.get('source_revision', ''))
            and (revision is None or document['source_revision'] == revision), 'node-infra deployment source differs')
    validate_components(document.get('components'))
    images = document.get('images', {})
    require(images and set(images) <= set(ARCHES) and (arches is None or set(images) == set(arches)),
            'node-infra deployment image architecture set differs')
    for arch, image in images.items():
        require(image.get('id') == image.get('config_digest') and DIGEST.fullmatch(image.get('id', ''))
                and DIGEST.fullmatch(image.get('manifest_digest', ''))
                and image.get('reference') == REPOSITORY + '@' + image['manifest_digest']
                and image.get('media_type') == OCI_MANIFEST
                and type(image.get('manifest_size')) is int and image['manifest_size'] > 0,
                'invalid node-infra platform image identity')
        archive = image.get('archive', {})
        require(archive.get('name') == selection.node_infra_archive(version, arch)
                and re.fullmatch(r'[0-9a-f]{64}', archive.get('sha256', ''))
                and type(archive.get('size')) is int and archive['size'] > 0,
                'invalid node-infra archive identity')
        docker = image.get('docker_archive')
        require((document['schema'] == 2) == (docker is not None), 'Docker archive/schema differs')
        if docker is not None:
            require(docker.get('name') == selection.node_infra_docker_archive(version, arch)
                    and re.fullmatch(r'[0-9a-f]{64}', docker.get('sha256', ''))
                    and type(docker.get('size')) is int and docker['size'] > 0, 'invalid Docker archive identity')
    require(document.get('index_digest') == 'sha256:' + hashlib.sha256(index_bytes(images)).hexdigest(),
            'node-infra index digest differs from selected native images')
    return document


def install_inputs(assets, destination, version, arch, *, revision=None, components=None):
    """Verify and copy prebuilt inputs; preparation never builds a system image."""
    deploy = assets / selection.node_infra_deploy_archive(version)
    document = read_deploy(deploy, version, revision)
    require(arch in document['images'], 'node-infra deployment lacks the requested architecture')
    require(components is None or document['components'] == components, 'node-infra component selection differs')
    image = document['images'][arch]
    path = assets / image['archive']['name']
    require(path.is_file() and not path.is_symlink() and path.stat().st_size == image['archive']['size']
            and digest(path) == image['archive']['sha256'], 'node-infra input archive digest differs')
    observed = inspect_oci(path, arch, version=version, revision=document['source_revision'])
    require(observed['image_id'] == image['id'] and all(observed[key] == image[key]
            for key in ('manifest_digest', 'manifest_size', 'media_type')), 'node-infra input image identity differs')
    image_dir = destination / 'images'
    image_dir.mkdir(parents=True, exist_ok=True)
    require(not (image_dir / path.name).exists(), 'node-infra image input already exists')
    shutil.copyfile(path, image_dir / path.name)
    (image_dir / path.name).chmod(0o644)
    if 'docker_archive' in image:
        record = image['docker_archive']
        docker = assets / record['name']
        require(docker.is_file() and not docker.is_symlink() and docker.stat().st_size == record['size']
                and digest(docker) == record['sha256'], 'Docker input archive digest differs')
        inspect_docker(docker, arch, image_id=image['id'], version=version, revision=document['source_revision'])
        require(not (image_dir / docker.name).exists(), 'Docker image input already exists')
        shutil.copyfile(docker, image_dir / docker.name)
        (image_dir / docker.name).chmod(0o644)
    archive_paths.extract([deploy], destination / 'deploy/node-infra')
    return document


def validate_set(directory, version, revision, *, receipts=None):
    records = {arch: validate(directory, version, arch, revision, receipts=receipts) for arch in ARCHES}
    document = read_deploy(directory / selection.node_infra_deploy_archive(version), version, revision, arches=ARCHES)
    require(document == release_document(version, revision, records), 'deployment package differs from image receipts')
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('package', 'validate', 'install'))
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--platform', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--arch', choices=ARCHES)
    parser.add_argument('--receipts', type=Path)
    args = parser.parse_args()
    if args.mode == 'package':
        require(args.platform is not None and args.output is not None, 'package needs platform and output')
        result = package(args.platform, args.directory, args.output, args.version, args.revision,
                         arches=(args.arch,) if args.arch else tuple(ARCHES))
    elif args.mode == 'install':
        require(args.arch and args.output is not None, 'install needs architecture and output')
        result = install_inputs(args.directory, args.output, args.version, args.arch, revision=args.revision)
    else:
        result = validate_set(args.directory, args.version, args.revision, receipts=args.receipts)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
