"""Read native node-infra release inputs without running or extracting an image."""
import hashlib
import json
from pathlib import Path
import re
import tarfile


def require(condition, message):
    if not condition:
        raise ValueError('node-infra: ' + message)


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def identity(value):
    require(isinstance(value, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', value),
            'invalid content identity')
    return value


def regular(root, relative):
    require(isinstance(relative, str) and relative and '\\' not in relative and
            not Path(relative).is_absolute() and all(p not in ('', '.', '..') for p in relative.split('/')),
            'invalid input path')
    path = root / relative
    require(all(not (root / parent).is_symlink() for parent in (Path(relative), *Path(relative).parents)),
            'linked input: ' + relative)
    require(path.is_file(), 'missing input: ' + relative)
    return path


def verify_oci(path, expected, platform):
    """Verify one platform manifest, its config, and every referenced layer blob."""
    with tarfile.open(path) as archive:
        members = {}
        for member in archive:
            name = member.name.rstrip('/')
            require(name and not name.startswith('/') and '\\' not in name and
                    all(p not in ('', '.', '..') for p in name.split('/')),
                    'unsafe OCI member')
            require(name not in members and (member.isfile() or member.isdir()),
                    'duplicate or non-regular OCI member')
            members[name] = member

        def read(name):
            require(name in members and members[name].isfile(), 'missing OCI member: ' + name)
            return archive.extractfile(members[name])

        def blob(descriptor, *, document=False):
            value = identity(descriptor.get('digest'))
            name = 'blobs/sha256/' + value[7:]
            require(type(descriptor.get('size')) is int and descriptor['size'] >= 0 and
                    name in members and members[name].size == descriptor['size'],
                    'OCI descriptor size mismatch')
            with read(name) as stream:
                actual = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
            require(actual == value, 'OCI descriptor digest mismatch')
            if document:
                with read(name) as stream:
                    return json.load(stream)

        with read('oci-layout') as stream:
            require(json.load(stream).get('imageLayoutVersion') == '1.0.0', 'unsupported OCI layout')
        with read('index.json') as stream:
            index = json.load(stream)
        require(index.get('schemaVersion') == 2 and len(index.get('manifests', [])) == 1,
                'offline archive must contain exactly one platform image')
        descriptor = index['manifests'][0]
        require(descriptor.get('digest') == expected['manifest_digest'] and
                descriptor.get('size') == expected['manifest_size'], 'release/OCI manifest mismatch')
        manifest = blob(descriptor, document=True)
        require(manifest.get('schemaVersion') == 2 and manifest.get('config', {}).get('digest') == expected['id'],
                'release/OCI config mismatch')
        config = blob(manifest['config'], document=True)
        require(config.get('os', '') + '/' + config.get('architecture', '') == platform,
                'image architecture differs from selected native architecture')
        require(config.get('rootfs', {}).get('type') == 'layers' and
                len(config['rootfs'].get('diff_ids', [])) == len(manifest.get('layers', [])),
                'image layer count differs from config')
        for layer in manifest['layers']:
            blob(layer)


def prepare(root, arch):
    deploy = root / 'deploy/node-infra'
    release = json.loads(regular(root, 'deploy/node-infra/release.json').read_text())
    regular(root, 'deploy/node-infra/bin/node-infra-ctl')
    regular(root, 'deploy/node-infra/templates/node-infra.container')
    require(type(release.get('schema')) is int and release['schema'] in (1, 2),
            'unsupported release.json schema')
    require(isinstance(release.get('version'), str) and release['version'] and
            re.fullmatch('[0-9a-f]{40}', release.get('source_revision', '')),
            'missing release version/source identity')
    require(release.get('repository') == 'ghcr.io/kuasar-sandbox/node-infra', 'unexpected image repository')
    identity(release.get('index_digest'))
    image = release.get('images', {}).get(arch)
    require(isinstance(image, dict), 'release has no selected architecture')
    require(identity(image.get('id')) == identity(image.get('config_digest')),
            'image ID differs from config digest')
    identity(image.get('manifest_digest'))
    require(image.get('reference') == release['repository'] + '@' + image['manifest_digest'],
            'image reference is not the selected platform manifest')
    record = image.get('archive', {})
    name = record.get('name', '')
    oci_arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}[arch]
    require(name == f"node-infra-{release['version']}-linux-{oci_arch}.oci.tar.gz",
            'archive name differs from release version/architecture')
    archive = regular(root, 'images/' + name)
    require(type(record.get('size')) is int and archive.stat().st_size == record['size'], 'archive size mismatch')
    require(re.fullmatch('[0-9a-f]{64}', record.get('sha256', '')) and checksum(archive) == record['sha256'],
            'archive digest mismatch')
    target = 'linux/' + oci_arch
    verify_oci(archive, image, target)
    return ({'runtime': 'podman', 'platform': target, 'archive': 'images/' + name,
             'sha256': record['sha256'], 'image_id': image['id'], 'reference': image['reference'],
             'manifest_digest': image['manifest_digest'], 'owner': 'platform'},
            {'deploy': str(deploy.relative_to(root)), 'release_version': release['version']})


def prepare_docker(root, arch):
    release = json.loads(regular(root, 'deploy/node-infra/release.json').read_text())
    if release['schema'] == 1:
        return None
    image = release['images'][arch]
    record = image['docker_archive']
    target = 'linux/' + {'x86_64': 'amd64', 'aarch64': 'arm64'}[arch]
    name = f"node-infra-{release['version']}-{target.replace('/', '-')}.docker.tar.gz"
    require(record['name'] == name, 'Docker archive name differs')
    path = regular(root, 'images/' + name)
    require(path.stat().st_size == record['size'] and checksum(path) == record['sha256'], 'Docker archive checksum differs')
    import workspace
    verified = workspace.verify_image_archive(path, target)
    require(verified['image_id'] == image['id'], 'Docker/OCI config differs')
    return {'runtime': 'docker', 'platform': target, 'archive': 'images/' + name,
            'sha256': record['sha256'], 'image_id': image['id'], 'owner': 'platform'}
