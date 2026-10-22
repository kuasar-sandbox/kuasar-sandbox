#!/usr/bin/env python3
"""Classic and containerd identities must prove the same admitted config bytes."""
import argparse
import copy
from email.message import Message
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location('identity_probe', Path(__file__).with_name('image-store-probe.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
ci = probe.workbench
identity = ci.image_identity


def fixture(arch='x86_64'):
    # Nonce and returned shape from real Docker 29.4.2/containerd run 39782914665.
    _, selected, facts = probe.fixture(arch, '15d8f92ce0e54dc0a87c428739e60636')
    image = {'Id': facts['manifest_digest'], 'Os': 'linux', 'Architecture': ci.PLATFORMS[arch],
             'Config': json.loads(facts['config_raw'])['config'], 'Size': 681,
             'RootFS': {'Type': 'layers'}, 'Descriptor': {'digest': facts['manifest_digest'],
                 'size': len(facts['manifest_raw']), 'mediaType': probe.MANIFEST}}
    return selected, image, (facts['manifest_raw'].encode(), facts['config_raw'].encode())


class IdentityTests(unittest.TestCase):
    def test_observed_containerd_manifest_and_original_config_are_distinct(self):
        selected, image, metadata = fixture()
        row = selected['architectures']['x86_64']
        self.assertEqual(row['image_id'], 'sha256:a932047c22d405261e20697dd79abc1c267f9d72017576fef85d4105057db7af')
        self.assertEqual(image['Id'], 'sha256:0785189c25d20de927a0aad7ec7d1fe004923f961e659bdb54779e5e6fc05ae0')
        self.assertNotEqual(image['Id'], row['image_id'])
        self.assertEqual(ci.image_check(selected, 'x86_64', image, metadata=metadata), image['Id'])

    def test_both_native_platforms_and_classic_store(self):
        for arch in ci.PLATFORMS:
            selected, image, metadata = fixture(arch)
            self.assertEqual(ci.image_check(selected, arch, image, metadata=metadata), image['Id'])
            image['Id'] = selected['architectures'][arch]['image_id']
            del image['Descriptor']
            with patch.object(identity, 'registry_metadata') as fetch:
                self.assertEqual(ci.image_check(selected, arch, image), image['Id'])
            fetch.assert_not_called()

    def test_tampered_raw_bytes_and_config_size_are_rejected(self):
        for changed in ('manifest', 'config', 'size', 'binding', 'index'):
            selected, image, (raw, config) = fixture()
            if changed == 'manifest':
                raw += b' '
            elif changed == 'config':
                config += b' '
            else:
                manifest = json.loads(raw)
                if changed == 'size':
                    manifest['config']['size'] += 1
                elif changed == 'binding':
                    manifest['config']['digest'] = 'sha256:' + 'f' * 64
                else:
                    manifest['manifests'] = []
                raw = probe.canonical(manifest)
                target = identity.digest(raw)
                selected['architectures']['x86_64']['reference'] = ci.REGISTRY + '@' + target
                image['Id'] = image['Descriptor']['digest'] = target
                image['Descriptor']['size'] = len(raw)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                ci.image_check(selected, 'x86_64', image, metadata=(raw, config))

    def test_local_descriptor_and_config_conflicts_are_rejected(self):
        changes = [lambda i: i.update(Id='sha256:' + 'e' * 64),
                   lambda i: i['Descriptor'].update(digest='sha256:' + 'e' * 64),
                   lambda i: i['Descriptor'].update(size=1),
                   lambda i: i['Descriptor'].update(mediaType='application/vnd.oci.image.index.v1+json'),
                   lambda i: i.update(Os='windows'), lambda i: i.update(Architecture='arm64'),
                   lambda i: i['RootFS'].update(Layers=['sha256:' + 'e' * 64]),
                   lambda i: i['Config']['Labels'].update({'unexpected': 'changed'})]
        for field in ('version', 'revision', 'source'):
            changes.append(lambda i, field=field: i['Config']['Labels'].update(
                {'org.opencontainers.image.' + field: 'foreign'}))
        for change in changes:
            selected, image, metadata = fixture()
            change(image)
            with self.subTest(change=change), self.assertRaises(ValueError):
                ci.image_check(selected, 'x86_64', image, metadata=metadata)

    def test_remote_config_platform_and_labels_must_match_local_inspection(self):
        for field in ('architecture', 'labels', 'rootfs'):
            selected, image, (raw, config) = fixture()
            value = json.loads(config)
            if field == 'architecture':
                value['architecture'] = 'arm64'
            elif field == 'labels':
                value['config']['Labels']['org.opencontainers.image.source'] = 'foreign'
            else:
                value['rootfs']['diff_ids'] = ['sha256:' + 'e' * 64]
            config = probe.canonical(value)
            row = selected['architectures']['x86_64']
            row['image_id'] = identity.digest(config)
            manifest = json.loads(raw)
            manifest['config'].update(digest=row['image_id'], size=len(config))
            raw = probe.canonical(manifest)
            target = identity.digest(raw)
            row['reference'] = ci.REGISTRY + '@' + target
            image['Id'] = image['Descriptor']['digest'] = target
            image['Descriptor']['size'] = len(raw)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'local config'):
                ci.image_check(selected, 'x86_64', image, metadata=(raw, config))

    def test_cold_and_warm_containerd_acquisition_keep_config_and_execution_ids_separate(self):
        for cold in (False, True):
            selected, image, metadata = fixture()
            row = selected['architectures']['x86_64']
            evidence = {}
            with patch.object(ci.platform, 'machine', return_value='x86_64'), \
                    patch.object(ci.launcher, 'inspect', side_effect=[None, None, image] if cold else [None, image]) as inspect, \
                    patch.object(ci.image_pull, 'pull_image') as pull, \
                    patch.object(identity, 'registry_metadata', return_value=metadata) as fetch:
                result = ci.acquire(selected, 'x86_64', evidence=evidence)
            self.assertEqual(result['image_id'], row['image_id'])
            self.assertEqual(result['docker_image_id'], image['Id'])
            self.assertEqual(result['image_present_before'], not cold)
            self.assertEqual(inspect.call_args.args, ('image', row['reference']))
            self.assertEqual(evidence['observed']['Id'], image['Id'])
            fetch.assert_called_once_with(row)
            if cold:
                pull.assert_called_once_with(row['reference'])
            else:
                pull.assert_not_called()

    def test_conflicting_config_lookup_does_not_fall_back_or_pull(self):
        selected, image, _ = fixture()
        image['Id'] = 'sha256:' + 'e' * 64
        with patch.object(ci.platform, 'machine', return_value='x86_64'), \
                patch.object(ci.launcher, 'inspect', return_value=image) as inspect, \
                patch.object(ci.image_pull, 'pull_image') as pull, self.assertRaises(ValueError):
            ci.acquire(selected, 'x86_64')
        inspect.assert_called_once()
        pull.assert_not_called()

    def test_launcher_receives_verified_execution_id_and_receipt_retains_config(self):
        selected, image, _ = fixture()
        row = selected['architectures']['x86_64']
        selected['framework_sha'] = 'a' * 40
        acquired = {**row, 'docker_image_id': image['Id']}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sources').mkdir()
            args = argparse.Namespace(selection=root / 'selection.json', sources=root / 'sources',
                root=root / 'state', arch='x86_64', cpus=2, memory_gib=8, mode='build', network='bridge', output=None)
            args.selection.write_text(json.dumps(selected))
            with patch.object(ci, 'framework_sha', return_value=selected['framework_sha']), \
                    patch.object(ci, 'check_selection', return_value=selected), \
                    patch.object(ci.os, 'getuid', return_value=1001), \
                    patch.object(ci, 'acquire', return_value=acquired), patch.object(ci.subprocess, 'run') as run:
                ci.start(args)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('--image') + 1], image['Id'])
            receipt = json.loads((args.root / 'receipt.json').read_text())
            self.assertEqual(receipt['image']['image_id'], row['image_id'])
            self.assertEqual(receipt['image']['docker_image_id'], image['Id'])

    def test_container_ownership_requires_the_recorded_docker_execution_id(self):
        selected, image, _ = fixture()
        data = {'id': 'owned', 'owner_uid': 1001, 'container_name': 'private',
                'container_id': 'container', 'image_id': image['Id']}
        container = {'Id': 'container', 'Image': image['Id'], 'Config': {'Labels': {
            ci.launcher.LABEL: 'owned', 'org.kuasar.workbench.uid': '1001'}}}
        with patch.object(ci.launcher, 'inspect', return_value=container):
            self.assertEqual(ci.launcher.owned_container(data), container)
            container['Image'] = selected['architectures']['x86_64']['image_id']
            with self.assertRaisesRegex(ValueError, 'foreign container collision'):
                ci.launcher.owned_container(data)


class MetadataTransportTests(unittest.TestCase):
    def test_registry_fetches_only_the_exact_public_manifest_and_config(self):
        selected, _, metadata = fixture()
        row = selected['architectures']['x86_64']
        with patch.object(identity, 'read_public', side_effect=[b'{"token":"anonymous"}', *metadata]) as read:
            self.assertEqual(identity.registry_metadata(row), metadata)
        self.assertEqual(len(read.call_args_list), 3)
        self.assertTrue(read.call_args_list[1].args[0].endswith('/manifests/' + row['reference'].split('@')[1]))
        self.assertTrue(read.call_args_list[2].args[0].endswith('/blobs/' + row['image_id']))

    def test_manifest_conflict_stops_before_config_fetch(self):
        selected, _, metadata = fixture()
        with patch.object(identity, 'read_public', side_effect=[b'{"token":"anonymous"}', metadata[0] + b' ']) as read, \
                self.assertRaisesRegex(ValueError, 'manifest digest'):
            identity.registry_metadata(selected['architectures']['x86_64'])
        self.assertEqual(read.call_count, 2)

    def test_mutable_or_foreign_reference_never_reads_credentials_or_network(self):
        for reference in (ci.REGISTRY + ':latest', 'registry.invalid/image@sha256:' + 'a' * 64):
            selected, _, _ = fixture()
            selected['architectures']['x86_64']['reference'] = reference
            with patch.object(identity, 'read_public') as read, self.assertRaises(ValueError):
                identity.registry_metadata(selected['architectures']['x86_64'])
            read.assert_not_called()

    def test_blob_redirect_never_forwards_authorization(self):
        headers = Message()
        headers['Location'] = 'https://cdn.example.invalid/blob?signed=private'
        redirect = urllib.error.HTTPError('https://ghcr.io/blob', 307, '', headers, None)
        with patch.object(identity.urllib.request, 'build_opener') as opener:
            opener.return_value.open.side_effect = [redirect, io.BytesIO(b'config')]
            self.assertEqual(identity.read_public('https://ghcr.io/blob', {'Authorization': 'Bearer anonymous'}, blob=True), b'config')
            calls = opener.return_value.open.call_args_list
            self.assertEqual(calls[0].args[0].get_header('Authorization'), 'Bearer anonymous')
            self.assertIsNone(calls[1].args[0].get_header('Authorization'))

    def test_oversize_and_non_https_metadata_are_rejected(self):
        with patch.object(identity.urllib.request, 'build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(b'x' * (identity.MAX_METADATA_BYTES + 1))
            with self.assertRaisesRegex(ValueError, 'size limit'):
                identity.read_public('https://ghcr.io/metadata')
            opener.reset_mock()
            with self.assertRaisesRegex(ValueError, 'invalid public metadata URL'):
                identity.read_public('http://ghcr.io/metadata')
            opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
