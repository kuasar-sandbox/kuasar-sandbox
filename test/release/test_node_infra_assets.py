"""Node-infra delivery identity, offline input and historical-contract checks."""
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'release'))
import node_infra_assets as subject
import selection

VERSION = 'release-v1.2.3-preview.20990101'
REVISION = '1' * 40
COMPONENTS = {unit: {'version': 'selected-' + unit, 'repository': 'kuasar-sandbox/' + (
    'guest-runtime' if unit in ('runtime', 'vmlinux') else unit), 'sha': '2' * 40}
    for unit in selection.UNITS}


def archive(root, arch='x86_64', mutation=None):
    blobs = {}
    def blob(value, media_type):
        identity = 'sha256:' + hashlib.sha256(value).hexdigest()
        blobs['blobs/sha256/' + identity[7:]] = value
        return {'mediaType': media_type, 'digest': identity, 'size': len(value)}
    layer = gzip.compress(b'fixture image layer', mtime=0)
    config = subject.canonical({'os': 'linux', 'architecture': subject.ARCHES[arch],
        'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + hashlib.sha256(b'fixture image layer').hexdigest()]},
        'config': {'Labels': {'org.opencontainers.image.version': VERSION,
                              'org.opencontainers.image.revision': REVISION}}})
    config_descriptor = blob(config, 'application/vnd.oci.image.config.v1+json')
    manifest = subject.canonical({'schemaVersion': 2, 'mediaType': subject.OCI_MANIFEST,
        'config': config_descriptor,
        'layers': [blob(layer, 'application/vnd.oci.image.layer.v1.tar+gzip')]})
    manifest_descriptor = blob(manifest, subject.OCI_MANIFEST)
    blobs['oci-layout'] = b'{"imageLayoutVersion":"1.0.0"}'
    blobs['index.json'] = subject.canonical({'schemaVersion': 2, 'manifests': [manifest_descriptor]})
    if mutation:
        mutation(blobs)
    name = selection.node_infra_archive(VERSION, arch)
    with tarfile.open(root / name, 'w:gz') as stream:
        for name_in_tar, data in blobs.items():
            entry = tarfile.TarInfo(name_in_tar)
            entry.size, entry.mode = len(data), 0o644
            stream.addfile(entry, io.BytesIO(data))
    receipt = {'aggregate_version': VERSION, 'source_revision': REVISION, 'arch': arch,
               'archive': name, 'sha256': subject.digest(root / name), 'size': (root / name).stat().st_size,
               'image_id': config_descriptor['digest'], 'manifest_digest': manifest_descriptor['digest'],
               'manifest_size': len(manifest), 'media_type': subject.OCI_MANIFEST, 'components': COMPONENTS}
    (root / f'node-infra-{arch}.json').write_text(json.dumps(receipt))
    return receipt


def docker_archive(root, record, *, corrupt=False, alias='../outside'):
    with tarfile.open(root / record['archive']) as source:
        manifest = json.load(source.extractfile('blobs/sha256/' + record['manifest_digest'][7:]))
        config = source.extractfile('blobs/sha256/' + record['image_id'][7:]).read()
        layer = gzip.decompress(source.extractfile('blobs/sha256/' + manifest['layers'][0]['digest'][7:]).read())
    layer_name = hashlib.sha256(layer).hexdigest() + '.tar'
    files = {'config.json': config, layer_name: layer + (b'corruption' if corrupt else b''),
             'manifest.json': subject.canonical([{'Config': 'config.json', 'Layers': [layer_name]}])}
    path = root / selection.node_infra_docker_archive(VERSION, record['arch'])
    with tarfile.open(path, 'w:gz') as stream:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(data), 0o644
            stream.addfile(entry, io.BytesIO(data))
        entry = tarfile.TarInfo('a' * 64 + '/layer.tar')
        entry.type, entry.linkname = tarfile.SYMTYPE, ('../' + layer_name if alias == 'valid' else alias)
        stream.addfile(entry)
    record['docker_archive'] = {'name': path.name, 'sha256': subject.digest(path), 'size': path.stat().st_size}
    (root / ('node-infra-' + record['arch'] + '.json')).write_text(json.dumps(record))
    return path


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.images = self.root / 'images'
        self.images.mkdir()
        self.platform = self.root / 'platform'
        for name in ('node-infra/deploy/bin/node-infra-ctl', 'node-infra/deploy/templates/node-infra.container',
                     'node-infra/examples/connector-vswitch/vswitch.yaml', 'docs/node-infra.md', 'docs/node-infra_zh.md'):
            path = self.platform / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# fixture\n')
        (self.platform / 'node-infra/deploy/bin/node-infra-ctl').chmod(0o755)

    def test_new_contract_extends_assets_without_changing_old_release(self):
        old = selection.delivery_assets(VERSION, selection.DELIVERY)
        new = selection.delivery_assets(VERSION, selection.NODE_INFRA_DELIVERY)
        self.assertEqual(len(old), 2)
        self.assertEqual(len(selection.delivery_assets(VERSION, 'node-infra-v1')), 5)
        self.assertEqual(len(new), 7)
        self.assertTrue(old < new)
        self.assertEqual(selection.delivery_assets(VERSION, 'historical'), set())
        self.assertEqual(selection.delivery({'delivery': selection.NODE_INFRA_DELIVERY}, 'new'), selection.NODE_INFRA_DELIVERY)
        self.assertEqual(set(selection.UNITS), set(COMPONENTS))

    def test_saved_oci_config_manifest_and_layers_are_verified(self):
        record = archive(self.images)
        self.assertEqual(subject.validate(self.images, VERSION, 'x86_64', REVISION), record)
        with self.assertRaisesRegex(ValueError, 'platform'):
            subject.inspect_oci(self.images / record['archive'], 'aarch64')
        with self.assertRaisesRegex(ValueError, 'source'):
            subject.inspect_oci(self.images / record['archive'], 'x86_64', revision='3' * 40)

    def test_docker_archive_retains_oci_identity_and_safe_legacy_aliases(self):
        for arch in subject.ARCHES:
            record = archive(self.images, arch)
            docker_archive(self.images, record, alias='valid')
            self.assertEqual(subject.validate(self.images, VERSION, arch, REVISION), record)
        document = subject.package(self.platform, self.images, self.images, VERSION, REVISION)
        self.assertEqual(document['schema'], 2)
        self.assertEqual(subject.validate_set(self.images, VERSION, REVISION), document)

    def test_docker_corruption_cannot_be_hidden_by_rewriting_archive_receipt(self):
        record = archive(self.images)
        docker_archive(self.images, record, alias='valid', corrupt=True)
        with self.assertRaisesRegex(ValueError, 'layer differs'):
            subject.validate(self.images, VERSION, 'x86_64', REVISION)

    def test_docker_alias_cannot_escape_to_an_unverified_member(self):
        record = archive(self.images)
        docker_archive(self.images, record)
        with self.assertRaisesRegex(ValueError, 'unsafe image archive member'):
            subject.validate(self.images, VERSION, 'x86_64', REVISION)

    def test_layer_corruption_is_detected_even_with_a_new_outer_checksum(self):
        def mutate(blobs):
            name = next(name for name, data in blobs.items() if data.startswith(b'\x1f\x8b'))
            blobs[name] = b'x' + blobs[name][1:]
        record = archive(self.images, mutation=mutate)
        with self.assertRaisesRegex(ValueError, 'blob digest'):
            subject.validate(self.images, VERSION, 'x86_64', REVISION)

    def test_oci_archive_cannot_smuggle_an_unreferenced_file(self):
        archive(self.images, mutation=lambda blobs: blobs.update({'unowned': b'payload'}))
        with self.assertRaisesRegex(ValueError, 'unreferenced'):
            subject.validate(self.images, VERSION, 'x86_64', REVISION)

    def test_unsafe_archive_path_is_rejected_without_extraction(self):
        archive(self.images, mutation=lambda blobs: blobs.update({'../outside': b'payload'}))
        with self.assertRaisesRegex(ValueError, 'unsafe archive path'):
            subject.validate(self.images, VERSION, 'x86_64', REVISION)
        self.assertFalse((self.root / 'outside').exists())

    def test_dual_arch_deploy_document_binds_the_exact_index_and_archives(self):
        for arch in subject.ARCHES:
            archive(self.images, arch)
        document = subject.package(self.platform, self.images, self.images, VERSION, REVISION)
        self.assertEqual(subject.validate_set(self.images, VERSION, REVISION), document)
        self.assertEqual(document['index_digest'], 'sha256:' + hashlib.sha256(
            (self.images / 'node-infra-index.json').read_bytes()).hexdigest())
        self.assertEqual(document['images']['x86_64']['reference'], subject.REPOSITORY + '@' +
                         document['images']['x86_64']['manifest_digest'])
        deploy = self.images / selection.node_infra_deploy_archive(VERSION)
        with tarfile.open(deploy) as stream:
            self.assertEqual(stream.getmember('bin/node-infra-ctl').mode, 0o755)
            self.assertTrue(all(member.uid == member.gid == 0 for member in stream))
            self.assertNotIn(deploy.name, json.dumps(document))

    def test_preparation_copies_only_the_requested_native_image_and_deploy(self):
        for arch in subject.ARCHES:
            archive(self.images, arch)
        subject.package(self.platform, self.images, self.images, VERSION, REVISION)
        destination = self.root / 'prepared'
        subject.install_inputs(self.images, destination, VERSION, 'aarch64', revision=REVISION, components=COMPONENTS)
        self.assertEqual([p.name for p in (destination / 'images').iterdir()],
                         [selection.node_infra_archive(VERSION, 'aarch64')])
        self.assertTrue((destination / 'deploy/node-infra/bin/node-infra-ctl').is_file())
        altered = copy.deepcopy(COMPONENTS)
        altered['connector']['sha'] = '3' * 40
        with self.assertRaisesRegex(ValueError, 'component selection'):
            subject.install_inputs(self.images, self.root / 'other', VERSION, 'x86_64', components=altered)

    def test_single_arch_candidate_does_not_satisfy_dual_release_gate(self):
        archive(self.images)
        subject.package(self.platform, self.images, self.images, VERSION, REVISION, arches=('x86_64',))
        document = subject.read_deploy(self.images / selection.node_infra_deploy_archive(VERSION), VERSION)
        self.assertEqual(set(document['images']), {'x86_64'})
        with self.assertRaisesRegex(ValueError, 'architecture set'):
            subject.read_deploy(self.images / selection.node_infra_deploy_archive(VERSION), VERSION, arches=subject.ARCHES)

    def test_selected_old_sources_fail_before_image_build(self):
        with self.assertRaisesRegex(ValueError, 'predates'):
            subject.require_config_sources(self.platform)
        for relative, source in [('orchestrator/config/files.go', 'func mergeYAMLFiles('),
                                 ('connector/pkg/vswitch/config_file.go', 'func parseConfigYAML(')]:
            path = self.platform / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
        subject.require_config_sources(self.platform)

    def test_package_rejects_a_linked_deployment_input(self):
        archive(self.images)
        target = self.platform / 'node-infra/deploy/bin/node-infra-ctl'
        target.unlink()
        target.symlink_to(self.platform / 'docs/node-infra.md')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            subject.package(self.platform, self.images, self.images, VERSION, REVISION, arches=('x86_64',))


if __name__ == '__main__':
    unittest.main()
