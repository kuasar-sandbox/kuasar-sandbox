"""Immutable file, image and helper inputs shared by public and CI preparation."""
from __future__ import annotations

import hashlib
import gzip
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import importlib.util

_spec = importlib.util.spec_from_file_location('demo_wheels', Path(__file__).with_name('demo_wheels.py'))
demo_wheels = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo_wheels)

HELPERS = {
    "zot": "ZOT_BIN", "versitygw": "VGW_BIN", "custom-proxy": "CUSTOM_PROXY_BIN",
    "telemetry-grpc-probe": "TELEMETRY_GRPC_PROBE_BIN", "usage-probe": "USAGE_PROBE_BIN",
    "cgroup-fork-probe": "CGROUP_FORK_PROBE_BIN",
    "node-ctl-runner-test": "NODE_CTL_RUNNER_TEST_BINARY",
}
IMAGE_VARIABLES = {
    "prometheus": "TELEMETRY_PROMETHEUS_IMAGE", "clickhouse": "TELEMETRY_CLICKHOUSE_IMAGE",
    "orchestrator-base": "ORCHESTRATOR_BASE_IMAGE", "orchestrator-execute": "ORCHESTRATOR_EXECUTE_IMAGE",
    "busybox": "KUASAR_BUSYBOX_IMAGE",
}
BACKENDS = {
    "prometheus": "prom/prometheus:v3.5.0@sha256:63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996",
    "clickhouse": "clickhouse/clickhouse-server:25.8@sha256:0152dd511befe6a2c2ef53e930726179669b08116da78500b37c51c96ff5ee77",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open('rb') as data:
        return hashlib.file_digest(data, 'sha256').hexdigest()


def check_helper(path, arch):
    require(path.is_file() and not path.is_symlink(), 'missing prebuilt helper file')
    mode = path.stat().st_mode
    require(mode & 0o111 and not mode & 0o7022, 'unsafe or non-executable helper')
    with path.open('rb') as stream:
        header = stream.read(64)
    require(len(header) == 64 and header[:7] == b'\x7fELF\x02\x01\x01' and
            struct.unpack_from('<H', header, 18)[0] == {'x86_64': 62, 'aarch64': 183}[arch],
            'prebuilt helper has the wrong architecture')


def files(root):
    require(root.is_dir() and not root.is_symlink(), f'missing input directory: {root}')
    result = {}
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink(), f'symlink in prepared inputs: {path}')
        if path.is_dir():
            continue
        require(path.is_file(), f'non-regular prepared input: {path}')
        result[str(path.relative_to(root))] = digest(path)
    return result


def seal(root, provenance):
    records = files(root)
    records.pop('provenance.json', None)
    provenance.update(files=records, modes={name: (root / name).stat().st_mode & 0o7777 for name in records})
    (root / 'provenance.json').write_text(json.dumps(provenance, sort_keys=True, separators=(',', ':')) + '\n')


def verify(root):
    path = root / 'provenance.json'
    require(path.is_file() and not path.is_symlink(), 'prepared workspace is missing provenance.json')
    provenance = json.loads(path.read_text())
    actual = files(root)
    actual.pop('provenance.json')
    require(actual == provenance['files'], 'prepared workspace file contents changed')
    require({name: (root / name).stat().st_mode & 0o7777 for name in actual} == provenance['modes'],
            'prepared workspace permissions changed')
    return provenance


def runtime_init_identity(root, arch):
    """Read the selected bundle's init as data, never execute an image payload."""
    bundle = root / 'bin/sandbox-runtime.bundle'
    fsck, dump = shutil.which('fsck.erofs'), shutil.which('dump.erofs')
    require(fsck and dump, 'Runtime identity requires fsck.erofs and dump.erofs with --cat support')
    subprocess.run([fsck, '--extract', str(bundle)], stdout=subprocess.DEVNULL, check=True)
    metadata = subprocess.check_output([dump, '--path=/sbin/init', str(bundle)], text=True)
    sizes = re.findall(r'^Size: ([0-9]+)\s+On-disk size: [0-9]+\s+regular file$', metadata, re.M)
    require(len(sizes) == 1 and 0 < int(sizes[0]) and
            len(re.findall(r'^Uid: 0\s+Gid: 0\s+Access: 0755/rwxr-xr-x$', metadata, re.M)) == 1,
            'Runtime init must be a root-owned executable regular file')
    with tempfile.TemporaryDirectory(prefix='runtime-init-') as directory:
        target = Path(directory) / 'init'
        with target.open('xb') as stream:
            subprocess.run([dump, '--cat', '--path=/sbin/init', str(bundle)], stdout=stream, check=True)
        require(target.stat().st_size == int(sizes[0]), 'Runtime init read size mismatch')
        with target.open('rb') as stream:
            header = stream.read(64)
        require(len(header) == 64 and header[:7] == b'\x7fELF\x02\x01\x01' and
                struct.unpack_from('<H', header, 18)[0] == {'x86_64': 62, 'aarch64': 183}[arch],
                'Runtime init has the wrong architecture')
        return digest(target)


def prepare_demo_sdk(root, arch=None):
    requirements = root / 'test/demo/requirements.txt'
    require(requirements.is_file() and not requirements.is_symlink(), 'missing prepared Demo SDK requirements')
    arch = arch or platform.machine()
    lock = root / 'test/demo/requirements.lock'
    wheelhouse = root / 'test/demo/wheels' / arch
    manifest = demo_wheels.validate(wheelhouse, lock, requirements, arch)
    require(f'{sys.version_info.major}.{sys.version_info.minor}' == manifest['python'], 'Demo SDK requires prepared Python 3.12 wheels')
    site = root / 'fixtures/demo-sdk'
    require(not site.exists() and not site.is_symlink(), 'Demo SDK fixture already exists')
    subprocess.run([sys.executable, '-m', 'pip', '--isolated', '--no-input',
                    '--disable-pip-version-check', '--no-cache-dir', 'install',
                    '--only-binary=:all:', '--no-compile', '--ignore-installed', '--require-hashes',
                    '--no-index', '--find-links', str(wheelhouse),
                    '--target', str(site), '--requirement', str(lock)], check=True)
    require((site / 'e2b').is_dir(), 'prepared Demo SDK is missing e2b')
    subprocess.run([sys.executable, '-I', '-S', '-B', '-c',
                    'import sys; sys.path.insert(0, sys.argv[1]); import e2b', str(site)], check=True)
    records = files(site)
    require(records and not any('__pycache__' in Path(name).parts for name in records),
            'Demo SDK must contain wheel inputs without generated bytecode')
    return {'directory': str(site.relative_to(root)), 'requirements_sha256': digest(requirements),
            'python': manifest['python'], 'wheelhouse': manifest, 'files': records}


def image_id(path, go_arch):
    with tarfile.open(path) as archive:
        manifests = json.load(archive.extractfile('manifest.json'))
        require(len(manifests) == 1, 'fixture must describe exactly one image')
        name = manifests[0]['Config']
        require(not Path(name).is_absolute() and '..' not in Path(name).parts, 'unsafe image configuration path')
        data = archive.extractfile(name).read()
        config = json.loads(data)
        require(config['architecture'] == go_arch and config['os'] == 'linux', 'fixture has the wrong image platform')
        return 'sha256:' + hashlib.sha256(data).hexdigest()


def fixture_helper(root, owner, name):
    path = root / 'test/e2e/lib' / owner / name
    require(path.is_file() and not path.is_symlink(), f'missing prepared {owner} fixture helper')
    return path


def dependency_options(deps_dir=None, offline=False, environment=None):
    """Resolve the public CLI/environment options without detecting a container."""
    environment = os.environ if environment is None else environment
    value = environment.get('E2E_OFFLINE', '0')
    require(value in ('0', '1'), 'E2E_OFFLINE must be 0 or 1')
    directory = deps_dir if deps_dir is not None else environment.get('E2E_DEPS_DIR')
    if directory is not None:
        require(str(directory) != '', 'deps-dir must not be empty')
        directory = Path(directory)
        require(directory.is_dir() and not directory.is_symlink(), f'missing or unsafe deps-dir: {directory}')
        directory = directory.resolve()
    return directory, bool(offline or value == '1')


def external_image_requests(cases, arch):
    """The one case/architecture selection used by prepare and input collection."""
    names = {Path(case).name for case in cases}
    target = 'linux/' + {'x86_64': 'amd64', 'aarch64': 'arm64'}[arch]
    orchestrator = needs_orchestrator_images(names)
    sandbox = any(name.startswith(('sandbox.', 'snapshot.', 'telemetry.')) for name in names)
    sandbox |= bool(names & {'network.tapfd.sh', 'image.manifest-boot.sh', 'image.sandbox-assembly.sh'})
    references = {}
    if orchestrator or sandbox or 'basic.demo.sh' in names:
        references['python'] = 'python:3.12-slim'
    if sandbox:
        references['busybox'] = 'busybox:latest'
    if 'telemetry.backends.sh' in names:
        references.update(BACKENDS)
    return {label: {'reference': reference, 'platform': target} for label, reference in references.items()}


def needs_orchestrator_images(names):
    return (any(name.startswith(('orchestrator.', 'builder.')) for name in names) or
            bool(set(names) & {'telemetry.backends.sh', 'telemetry.guest.sh', 'telemetry.proxy.sh'}))


def required_helpers(cases):
    """Return the existing case-selected helper owners for build and prepare."""
    cases = set(cases)
    orchestrator = any(name.startswith(('orchestrator.', 'builder.')) for name in cases)
    orchestrator |= bool(cases & {'telemetry.backends.sh', 'telemetry.guest.sh', 'telemetry.proxy.sh'})
    sandboxer = any(name.startswith(('sandbox.', 'snapshot.')) for name in cases)
    sandboxer |= bool(cases & {'network.tapfd.sh', 'image.manifest-boot.sh', 'image.sandbox-assembly.sh',
                               'telemetry.usage.sh', 'telemetry.usage-faults.sh', 'telemetry.source-faults.sh'})
    helpers = {}
    if orchestrator or sandboxer or cases & {'basic.demo.sh', 'image.flatten.sh', 'image.registry.sh'}:
        helpers['zot'] = 'framework'
    if orchestrator or 'basic.demo.sh' in cases:
        helpers['versitygw'] = 'framework'
    if orchestrator:
        helpers.update({'custom-proxy': 'orchestrator', 'telemetry-grpc-probe': 'orchestrator',
                        'node-ctl-runner-test': 'orchestrator'})
    if sandboxer:
        helpers['usage-probe'] = 'sandboxer'
    if 'sandbox.cgroup.sh' in cases:
        helpers['cgroup-fork-probe'] = 'sandboxer'
    return helpers



def relative_input(name):
    require(isinstance(name, str) and name and '\\' not in name and '\x00' not in name and
            not Path(name).is_absolute() and all(part not in ('', '.', '..') for part in name.split('/')),
            f'unsafe dependency path: {name!r}')
    return Path(name)


def dependency_file(directory, name):
    path = directory / relative_input(name)
    for entry in (path, *path.parents):
        if entry == directory:
            break
        require(not entry.is_symlink(), f'symlink in dependency path: {path}')
    require(path.is_file() and not path.stat().st_mode & 0o7022, f'missing or unsafe dependency file: {path}')
    return path


def sha256_bytes(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def verify_image_archive(path, target):
    """Verify a Docker-save archive without loading it or extracting its layers."""
    with tarfile.open(path) as archive:
        members = {}
        for member in archive:
            name = member.name.rstrip('/') if member.isdir() else member.name
            relative_input(name)
            require(name not in members, f'duplicate image archive path: {name}')
            # Skopeo's Docker writer adds v1 compatibility layer aliases.
            # Never follow them: only this exact layout may refer back to a
            # manifest-selected regular layer, checked after its bytes below.
            legacy_alias = (member.issym() and re.fullmatch(r'[0-9a-f]{64}/layer\.tar', name)
                            and re.fullmatch(r'\.\./[0-9a-f]{64}\.tar', member.linkname))
            require(member.isfile() or member.isdir() or legacy_alias, f'unsafe image archive member: {name}')
            require(not member.mode & 0o7022, f'unsafe image archive permissions: {name}')
            members[name] = member
        for name in members:
            require(all(str(parent) not in members or members[str(parent)].isdir()
                        for parent in Path(name).parents), f'conflicting image archive path: {name}')

        def regular(name):
            relative_input(name)
            require(name in members and members[name].isfile(), f'missing image archive member: {name}')
            return archive.extractfile(members[name])

        with regular('manifest.json') as stream:
            manifests = json.load(stream)
        require(isinstance(manifests, list) and len(manifests) == 1, 'fixture must describe exactly one image')
        manifest = manifests[0]
        with regular(manifest['Config']) as stream:
            data = stream.read()
        config = json.loads(data)
        require(config['os'] + '/' + config['architecture'] == target, 'fixture has the wrong image platform')
        rootfs = config['rootfs']
        layers = manifest['Layers']
        require(rootfs['type'] == 'layers' and isinstance(layers, list) and
                len(layers) == len(rootfs['diff_ids']), 'image archive layer count differs from config')
        for name, expected in zip(layers, rootfs['diff_ids']):
            with regular(name) as stream:
                compressed = stream.read(2) == b'\x1f\x8b'
                stream.seek(0)
                if compressed:
                    with gzip.GzipFile(fileobj=stream) as expanded:
                        actual = 'sha256:' + hashlib.file_digest(expanded, 'sha256').hexdigest()
                else:
                    actual = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
            require(actual == expected, f'image archive layer differs from config: {name}')
        verified_layers = dict(zip(layers, rootfs['diff_ids']))
        for member in members.values():
            if member.issym():
                target_name = member.linkname.removeprefix('../')
                require(target_name in verified_layers and members[target_name].isfile()
                        and target_name == verified_layers[target_name].removeprefix('sha256:') + '.tar',
                        f'legacy image alias must target a verified regular layer: {member.name}')
        if 'index.json' in members:
            # Recent Docker saves also have an OCI index. Docker/containerd can
            # load that instead of manifest.json, so both must name one image.
            def blob(descriptor):
                identity = descriptor['digest']
                require(re.fullmatch(r'sha256:[0-9a-f]{64}', identity), 'unsupported archive blob digest')
                name = 'blobs/sha256/' + identity.split(':')[1]
                with regular(name) as stream:
                    require(members[name].size == descriptor['size'] and
                            'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest() == identity,
                            'archive OCI descriptor mismatch')
                return name

            def manifests_from(index, seen):
                result = []
                for descriptor in index['manifests']:
                    identity = descriptor['digest']
                    require(identity not in seen, 'duplicate or recursive archive OCI descriptor')
                    seen.add(identity)
                    name = 'blobs/sha256/' + identity.split(':')[-1]
                    if name not in members:
                        continue  # Docker may retain index entries for unsaved platforms.
                    with regular(blob(descriptor)) as stream:
                        document = json.load(stream)
                    require(document['schemaVersion'] == 2, 'unsupported archive OCI document')
                    if 'manifests' in document:
                        result.extend(manifests_from(document, seen))
                    else:
                        result.append(document)
                return result

            with regular('index.json') as stream:
                index = json.load(stream)
            require(index['schemaVersion'] == 2, 'unsupported archive OCI index')
            oci = manifests_from(index, set())
            require(len(oci) == 1, 'archive OCI index must describe exactly one saved image')
            require(oci[0]['config']['digest'] == sha256_bytes(data) and
                    blob(oci[0]['config']) == manifest['Config'] and
                    [blob(layer) for layer in oci[0]['layers']] == layers,
                    'archive OCI and Docker manifests disagree')
        return {'image_id': sha256_bytes(data), 'config_size': len(data), 'layers': len(layers)}


def verify_registry_identity(record, image):
    """Bind raw registry evidence to config and verified uncompressed layer bytes.

    A registry manifest binds its config digest; that config binds every layer's
    diff_id. Archive hashes, config IDs and registry digests remain distinct.
    """
    require(isinstance(record['manifest'], str), 'registry manifest must contain raw JSON text')
    manifest_bytes = record['manifest'].encode('utf-8')
    manifest_digest = sha256_bytes(manifest_bytes)
    require(manifest_digest == record['manifest_digest'], 'registry manifest digest mismatch')
    manifest = json.loads(manifest_bytes)
    require(manifest['schemaVersion'] == 2 and manifest['config']['digest'] == image['image_id'] and
            manifest['config']['size'] == image['config_size'] and len(manifest['layers']) == image['layers'],
            'registry manifest does not bind the archive config/layers')
    resolved = manifest_digest
    if 'index' in record:
        require(isinstance(record['index'], str), 'registry index must contain raw JSON text')
        index_bytes = record['index'].encode('utf-8')
        resolved = sha256_bytes(index_bytes)
        require(resolved == record['index_digest'], 'registry index digest mismatch')
        index = json.loads(index_bytes)
        require(index['schemaVersion'] == 2, 'unsupported registry index')
        matching = [entry for entry in index['manifests']
                    if entry.get('platform', {}).get('os', '') + '/' +
                    entry.get('platform', {}).get('architecture', '') == record['platform']]
        require(len(matching) == 1 and matching[0]['digest'] == manifest_digest and
                matching[0]['size'] == len(manifest_bytes), 'registry index does not bind a unique target manifest')
    else:
        require('index_digest' not in record, 'registry index bytes are missing')
    require(record['registry_digest'] == resolved, 'resolved registry digest mismatch')
    if '@' in record['reference']:
        require(record['reference'].rsplit('@', 1)[1] == resolved,
                'requested registry digest does not match verified registry evidence')


def local_image_inputs(requests, directory, offline):
    """Validate all selected local inputs before any remote fallback is possible."""
    records = []
    description = directory / 'images.json' if directory is not None else None
    if requests and description is not None and (description.exists() or description.is_symlink()):
        records = json.loads(dependency_file(directory, 'images.json').read_text())
        require(isinstance(records, list) and all(isinstance(record, dict) for record in records),
                'images.json must contain a flat list of image records')
    inputs = {}
    for label, request in requests.items():
        matches = [record for record in records if all(record.get(key) == value for key, value in request.items())]
        location = str(description) if description is not None else '(no deps-dir configured)'
        context = f"{request['reference']} {request['platform']} in {location}"
        require(len(matches) <= 1, f'ambiguous local image: {context}')
        if not matches:
            require(not offline, f'missing offline image: {context}')
            continue
        record = matches[0]
        try:
            path = dependency_file(directory, record['archive'])
            require(re.fullmatch(r'[0-9a-f]{64}', record['sha256']) and digest(path) == record['sha256'],
                    'local image archive sha256 mismatch')
            image = verify_image_archive(path, request['platform'])
            require(image['image_id'] == record['image_id'], 'local image config ID mismatch')
            verify_registry_identity(record, image)
        except (KeyError, TypeError, ValueError, OSError, tarfile.TarError) as error:
            raise ValueError(f'invalid local image: {context}: {error}') from error
        inputs[label] = (path, record)
    return inputs


def prepare_fixtures(root, arch, cases, deps_dir=None, offline=False):
    """Prepare immutable inputs; Build/flatten/snapshot/publication stay in cases."""
    names = {Path(case).name for case in cases}
    go_arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}[arch]
    target = 'linux/' + go_arch
    deps_dir, offline = dependency_options(deps_dir, offline)
    requests = external_image_requests(cases, arch)
    local = local_image_inputs(requests, deps_dir, offline)
    before = files(root)
    before_modes = {name: (root / name).stat().st_mode & 0o7777 for name in before}
    fixtures, images, python = {}, {}, {}
    for directory in ('images', 'fixtures'):
        (root / directory).mkdir(exist_ok=True)
    if 'basic.demo.sh' in names:
        require(platform.machine() == arch, 'Demo SDK preparation requires the selected native architecture')
        python['demo'] = prepare_demo_sdk(root, arch)
    if 'image.manifest.sh' in names:
        directory = root / 'fixtures/manifest'
        subprocess.run([sys.executable, '-B', str(fixture_helper(root, 'accelerator', 'manifest_fixture.py')),
                        str(directory), '--architecture', go_arch], check=True)
        for variant in ('a', 'b'):
            path = directory / f'image-{variant}.tar'
            fixtures[str(path.relative_to(root))] = {'platform': target, 'image_id': image_id(path, go_arch),
                                                     'sha256': digest(path), 'owner': 'accelerator'}
    if names & {'image.flatten.sh', 'image.registry.sh'}:
        path = root / 'images/guest-runtime.tar'
        reference = 'kuasar-e2e-guest-runtime:' + arch
        subprocess.run([sys.executable, '-B', str(fixture_helper(root, 'guest-runtime', 'fixture.py')), str(path),
                        '--tag', reference, '--architecture', go_arch], check=True)
        images['guest-runtime'] = {'reference': reference, 'platform': target, 'image_id': image_id(path, go_arch),
                                  'archive': str(path.relative_to(root)), 'sha256': digest(path), 'owner': 'guest-runtime'}
    orchestrator = needs_orchestrator_images(names)
    with tempfile.TemporaryDirectory(prefix='e2e-docker-') as config:
        environment = {**os.environ, 'DOCKER_CONFIG': config, 'PYTHONDONTWRITEBYTECODE': '1'}
        def save(label, reference, owner):
            path = root / 'images' / (label + '.tar')
            record = json.loads(subprocess.check_output(['docker', 'image', 'inspect', reference], env=environment))[0]
            # Keep the named reference in the archive. Cases create/remove
            # temporary registry tags; without the prepared tag, removing the
            # last such tag also deletes the image needed by subsequent cases.
            subprocess.run(['docker', 'image', 'save', '--output', str(path), reference], env=environment, check=True)
            require(image_id(path, go_arch) == record['Id'], 'saved image differs from resolved input')
            images[label] = {'reference': reference, 'platform': target, 'image_id': record['Id'],
                             'archive': str(path.relative_to(root)), 'sha256': digest(path), 'owner': owner,
                             'repo_digests': record.get('RepoDigests', [])}
        for label, request in requests.items():
            if label in local:
                source, record = local[label]
                path = root / 'images' / (label + '.tar')
                shutil.copyfile(source, path)
                require(digest(path) == record['sha256'], 'local image changed while copying')
                path.chmod(0o444)
                images[label] = {**record, 'archive': str(path.relative_to(root)), 'owner': 'platform'}
            else:
                reference = request['reference']
                subprocess.run(['timeout', '3m', 'docker', 'pull', '--platform=' + target, reference], env=environment, check=True)
                save(label, reference, 'platform')
        if orchestrator:
            require(platform.machine() == arch, 'orchestrator image preparation requires the selected native architecture')
            if 'python' in local:
                load_images(root, {'images': {'python': images['python']}}, environment)
            for variant in ('base', 'execute'):
                label = 'orchestrator-' + variant
                reference = 'kuasar-e2e-' + label + ':' + images['python']['image_id'].split(':')[1][:16]
                subprocess.run(['bash', str(fixture_helper(root, 'orchestrator', 'prepare_base_image.sh')),
                                images['python']['image_id'], reference, variant], env=environment, check=True)
                save(label, reference, 'orchestrator')
    after = files(root)
    require(all(after.get(name) == value and (root / name).stat().st_mode & 0o7777 == before_modes[name]
                for name, value in before.items()), 'fixture preparation modified existing inputs')
    allowed = set(fixtures) | {record['archive'] for record in images.values()}
    for record in python.values():
        allowed.update(str(Path(record['directory']) / name) for name in record['files'])
    require(set(after) - set(before) == allowed, 'fixture generator wrote undeclared workspace files')
    return dict(fixtures=fixtures, images=images, python=python)


def load_images(root, provenance, environment):
    for record in provenance.get('images', {}).values():
        path = root / record['archive']
        require(digest(path) == record['sha256'], 'prepared image bytes changed')
        subprocess.run(['docker', 'image', 'load', '--input', str(path)], env=environment, check=True)
        image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', record['image_id']], env=environment))[0]
        require(image['Id'] == record['image_id'] and image['Os'] + '/' + image['Architecture'] == record['platform'],
                'loaded image differs from prepared platform/digest')


def case_environment(root, provenance, case):
    environment = dict(BIN=str(root / 'bin'), E2E_WORKSPACE=str(root), E2E_ARCH=provenance['arch'],
                       E2E_LIB=str(root / 'test/e2e/lib'), PYTHONDONTWRITEBYTECODE='1', KUASAR_ARTIFACT_E2E='1',
                       TARGET_ARCH=provenance['arch'], MANIFEST_FIXTURE_DIR=str(root / 'fixtures/manifest'))
    for name, variable in HELPERS.items():
        if name in provenance.get('helpers', {}):
            environment[variable] = str(root / 'fixtures/bin' / name)
    if provenance.get('embedded', {}).get('init'):
        environment['KUASAR_EXPECTED_RUNTIME_INIT_SHA256'] = provenance['embedded']['init']
    images = provenance.get('images', {})
    for label, variable in IMAGE_VARIABLES.items():
        if label in images:
            environment[variable] = images[label]['image_id']
    label = 'guest-runtime' if case in {'image.flatten.sh', 'image.registry.sh'} else 'python'
    if label in images:
        environment.update(E2E_IMAGE=images[label]['image_id'], IMAGE=images[label]['image_id'])
    return environment
