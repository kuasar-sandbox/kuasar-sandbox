import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

LIB = Path(__file__).parent / 'lib'
sys.path.insert(0, str(LIB))
import node_infra_inputs
import workspace
import capability_probe


def archive_fixture(root, arch='x86_64'):
    oci_arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}[arch]
    blobs = {}
    def blob(value):
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        identity = 'sha256:' + hashlib.sha256(data).hexdigest()
        blobs['blobs/sha256/' + identity[7:]] = data
        return {'digest': identity, 'size': len(data)}
    layer = blob(b'fixture-layer')
    config = blob({'os': 'linux', 'architecture': oci_arch, 'rootfs': {'type': 'layers', 'diff_ids': [layer['digest']]}})
    manifest = blob({'schemaVersion': 2, 'config': config, 'layers': [layer]})
    version = 'v1.2.3'
    name = f'node-infra-{version}-linux-{oci_arch}.oci.tar.gz'
    path = root / 'images' / name
    path.parent.mkdir()
    blobs.update({'oci-layout': b'{"imageLayoutVersion":"1.0.0"}',
                  'index.json': json.dumps({'schemaVersion': 2, 'manifests': [manifest]}).encode()})
    with tarfile.open(path, 'w:gz') as archive:
        for name_, data in blobs.items():
            info = tarfile.TarInfo(name_)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    image = {'id': config['digest'], 'config_digest': config['digest'],
             'manifest_digest': manifest['digest'], 'manifest_size': manifest['size'],
             'reference': 'ghcr.io/kuasar-sandbox/node-infra@' + manifest['digest'],
             'archive': {'name': name, 'size': path.stat().st_size, 'sha256': node_infra_inputs.checksum(path)}}
    release = {'schema': 1, 'version': version, 'source_revision': 'a' * 40,
               'repository': 'ghcr.io/kuasar-sandbox/node-infra', 'index_digest': 'sha256:' + 'b' * 64,
               'images': {arch: image}}
    deploy = root / 'deploy/node-infra'
    (deploy / 'bin').mkdir(parents=True)
    (deploy / 'bin/node-infra-ctl').write_text('#!/usr/bin/env python3\n')
    (deploy / 'templates').mkdir()
    (deploy / 'templates/node-infra.container').write_text('[Container]\nImage=@IMAGE@\n')
    (deploy / 'release.json').write_text(json.dumps(release))
    return release, path


class NodeInfraInputs(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.release, self.archive = archive_fixture(self.root)

    def write_release(self):
        (self.root / 'deploy/node-infra/release.json').write_text(json.dumps(self.release))

    def test_prepare_preserves_native_identity_and_runtime(self):
        record, deployment = node_infra_inputs.prepare(self.root, 'x86_64')
        self.assertEqual(record['runtime'], 'podman')
        self.assertEqual(record['image_id'], self.release['images']['x86_64']['id'])
        self.assertEqual(deployment, {'deploy': 'deploy/node-infra', 'release_version': 'v1.2.3'})
        self.assertEqual(workspace.runtime_image_record(record), record)

    def test_selected_case_fails_for_missing_inputs(self):
        (self.root / 'deploy/node-infra/release.json').unlink()
        with self.assertRaisesRegex(ValueError, 'missing input'):
            workspace.prepare_fixtures(self.root, 'x86_64', ['basic.node-infra-deploy.sh'], offline=True)
        self.assertEqual(workspace.prepare_fixtures(self.root, 'x86_64', ['storage.cache.sh'], offline=True)['images'], {})

    def test_changed_archive_and_manifest_cannot_be_loaded(self):
        self.release['images']['x86_64']['manifest_digest'] = 'sha256:' + 'f' * 64
        self.release['images']['x86_64']['reference'] = self.release['repository'] + '@sha256:' + 'f' * 64
        self.write_release()
        with self.assertRaisesRegex(ValueError, 'release/OCI manifest mismatch'):
            node_infra_inputs.prepare(self.root, 'x86_64')
        self.archive.write_bytes(self.archive.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'archive size mismatch'):
            node_infra_inputs.prepare(self.root, 'x86_64')

    def test_missing_template_fails_before_runtime_activation(self):
        (self.root / 'deploy/node-infra/templates/node-infra.container').unlink()
        with self.assertRaisesRegex(ValueError, 'missing input.*node-infra.container'):
            workspace.prepare_fixtures(self.root, 'x86_64', ['basic.node-infra-deploy.sh'], offline=True)

    def test_config_identity_must_match_archive(self):
        record = self.release['images']['x86_64']
        record['id'] = record['config_digest'] = 'sha256:' + 'e' * 64
        self.write_release()
        with self.assertRaisesRegex(ValueError, 'release/OCI config mismatch'):
            node_infra_inputs.prepare(self.root, 'x86_64')

    def test_linked_or_wrong_architecture_inputs_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'selected architecture'):
            node_infra_inputs.prepare(self.root, 'aarch64')
        destination = self.archive.with_suffix('.saved')
        self.archive.rename(destination)
        self.archive.symlink_to(destination.name)
        with self.assertRaisesRegex(ValueError, 'linked input'):
            node_infra_inputs.prepare(self.root, 'x86_64')

    def test_runtime_selection_uses_podman_only_for_node_infra(self):
        record, deployment = node_infra_inputs.prepare(self.root, 'x86_64')
        provenance = {'arch': 'x86_64', 'images': {'node-infra': record}, 'node_infra': deployment}
        inspect = [{'Id': record['image_id'][7:], 'Os': 'linux', 'Architecture': 'amd64'}]
        with patch.object(workspace.subprocess, 'run') as run, patch.object(
                workspace.subprocess, 'check_output', return_value=json.dumps(inspect)):
            workspace.load_images(self.root, provenance, {}, ['basic.node-infra-lifecycle.sh'])
        self.assertEqual(run.call_args.args[0][:3], ['podman', 'image', 'load'])
        with patch.object(workspace.subprocess, 'run') as run:
            workspace.load_images(self.root, provenance, {}, ['storage.cache.sh'])
        run.assert_not_called()

    def test_rootless_podman_is_not_a_node_infra_capability(self):
        info = {'host': {'cgroupVersion': 'v2', 'os': 'linux', 'security': {'rootless': True}}}
        with patch.object(capability_probe, 'run', return_value=json.dumps(info)), patch.object(
                capability_probe.os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(ValueError, 'rootful Podman'):
                capability_probe.check_one('podman')


if __name__ == '__main__':
    unittest.main()
