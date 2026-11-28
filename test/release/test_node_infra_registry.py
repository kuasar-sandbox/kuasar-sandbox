"""Registry transfer uses the accepted OCI bytes and preserves immutable tags."""
import importlib.util
import json
import threading
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'release'))
import node_infra_assets as node
import node_infra_registry as subject
import selection
from test_node_infra_assets import archive, VERSION, REVISION


class RegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assets = self.root / 'assets'
        self.assets.mkdir()
        records = {arch: archive(self.assets, arch) for arch in node.ARCHES}
        self.document = node.release_document(VERSION, REVISION, records)
        self.blobs = {}
        for record in records.values():
            with tarfile.open(self.assets / record['archive']) as stream:
                for member in stream:
                    if member.name.startswith('blobs/sha256/'):
                        self.blobs['sha256:' + member.name.rsplit('/', 1)[1]] = stream.extractfile(member).read()
        self.index = node.index_bytes(self.document['images'])

    def test_layout_combines_verified_blobs_and_uses_the_deployment_index_bytes(self):
        layout = self.root / 'layout'
        subject.layout_from_archives(self.root, self.document, layout)
        descriptors = json.loads((layout / 'index.json').read_text())['manifests']
        self.assertEqual({row['annotations']['org.opencontainers.image.ref.name'] for row in descriptors},
                         {'x86_64', 'aarch64', 'aggregate'})
        identity = self.document['index_digest'].removeprefix('sha256:')
        self.assertEqual((layout / 'blobs/sha256' / identity).read_bytes(), self.index)
        for path in (layout / 'blobs/sha256').iterdir():
            self.assertEqual(node.digest(path), path.name)

    def publish(self, inspect, run, *, read_only=False):
        with patch.object(subject, 'validate_stage', return_value=self.document), \
                patch.object(subject.registry, 'inspect_raw', side_effect=inspect), \
                patch.object(subject.subprocess, 'run', side_effect=run), \
                patch.dict(os.environ, {'GH_TOKEN': 'fixture-only', 'GITHUB_ACTOR': 'fixture'}):
            return subject.publish(self.root, VERSION, REVISION, {}, {}, {}, read_only=read_only)

    def test_different_existing_native_tag_fails_without_any_transfer(self):
        transfers = []
        with self.assertRaisesRegex(ValueError, 'native tag has different bytes'):
            self.publish(lambda *args, **kwargs: b'different existing manifest', transfers.append)
        self.assertFalse(transfers)

    def test_completed_release_with_missing_image_never_backfills_the_registry(self):
        transfers = []
        with self.assertRaisesRegex(ValueError, 'missing its node-infra image'):
            self.publish(lambda *args, **kwargs: None, transfers.append, read_only=True)
        self.assertFalse(transfers)

    def transfer_fixture(self, fresh=False, changed_index=False):
        reference = node.REPOSITORY + ':' + VERSION
        tags = {} if fresh else {reference: self.index}
        images = self.document['images']
        for arch, image in images.items():
            if not fresh:
                tags[reference + '-' + node.ARCHES[arch]] = self.blobs[image['manifest_digest']]
        if changed_index:
            tags[reference] = self.index + b'\n'
        transfers = []
        def inspect(ref, auth, *, config=False, missing=False):
            if '@' in ref:
                identity = ref.split('@')[1]
                raw = self.blobs[identity]
                return self.blobs[json.loads(raw)['config']['digest']] if config else raw
            if missing:
                return tags.get(ref)
            return tags[ref]
        def run(command, **kwargs):
            self.assertIn('--preserve-digests', command)
            transfers.append(command)
            source, target = command[-2:]
            if target.startswith('oci-archive:'):
                identity = source.split('@')[1]
                image = next(item for item in images.values() if item['manifest_digest'] == identity)
                shutil.copyfile(self.assets / image['archive']['name'], target.removeprefix('oci-archive:'))
                return
            name = source.rsplit(':', 1)[1]
            if name == 'aggregate':
                self.assertIn('--all', command)
                tags[target.removeprefix('docker://')] = self.index
            else:
                self.assertNotIn('--all', command)
                tags[target.removeprefix('docker://')] = self.blobs[images[name]['manifest_digest']]
        return inspect, run, transfers

    def test_publish_transfers_native_bytes_then_exact_index_and_reads_every_native_layer(self):
        inspect, run, transfers = self.transfer_fixture(fresh=True)
        result = self.publish(inspect, run)
        self.assertEqual(result['digest'], self.document['index_digest'])
        pushes = [command for command in transfers if command[-1].startswith('docker://')]
        pulls = [command for command in transfers if command[-1].startswith('oci-archive:')]
        self.assertEqual(len(pushes), 3)
        self.assertEqual(len(pulls), 2)
        self.assertEqual(pushes[-1][-1], 'docker://' + node.REPOSITORY + ':' + VERSION)

    def test_read_only_verifies_public_content_without_registry_writes(self):
        inspect, run, transfers = self.transfer_fixture()
        self.publish(inspect, run, read_only=True)
        self.assertEqual(len(transfers), 2)
        self.assertTrue(all(command[-1].startswith('oci-archive:') for command in transfers))

    def test_even_semantically_equal_but_different_index_bytes_are_rejected(self):
        inspect, run, transfers = self.transfer_fixture(changed_index=True)
        with self.assertRaisesRegex(ValueError, 'aggregate tag differs'):
            self.publish(inspect, run)
        self.assertTrue(all(command[-1].startswith('oci-archive:') for command in transfers))

    def test_published_download_preserves_legacy_set_and_rejects_partial_new_set(self):
        spec = importlib.util.spec_from_file_location('node_published_download', ROOT / 'ci/integration/check-published-download.py')
        download = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(download)
        with patch.object(download, 'download_asset') as transfer:
            download.check_node_infra({'assets': []}, self.root, VERSION, 'x86_64', {})
            with self.assertRaisesRegex(ValueError, 'asset set is incomplete'):
                download.check_node_infra({'assets': [{'name': selection.node_infra_archive(VERSION, 'x86_64')}]},
                                         self.root, VERSION, 'x86_64', {})
            transfer.assert_not_called()


    def test_parallel_native_io_uses_frozen_layout_and_private_readbacks(self):
        inspect, transfer, transfers = self.transfer_fixture(fresh=True)
        uploads, readbacks = threading.Barrier(2, timeout=5), threading.Barrier(2, timeout=5)
        lock = threading.Lock()
        completed, paths, layouts = set(), {}, []
        original_inspect = node.inspect_oci

        def inspect_archive(path, arch, **kwargs):
            result = original_inspect(path, arch, **kwargs)
            with lock:
                completed.add(arch)
            return result

        def run(command, **kwargs):
            source, target = command[-2:]
            if target.startswith('oci-archive:'):
                path = Path(target.removeprefix('oci-archive:'))
                self.assertIn(path.parent.name, node.ARCHES)
                self.assertFalse(path.exists())
                transfer(command, **kwargs)
                paths[path.parent.name] = path
                readbacks.wait()
                self.assertTrue(path.is_file())
            elif source.endswith(':aggregate'):
                self.assertEqual(completed, set(node.ARCHES))
                transfer(command, **kwargs)
            else:
                layout = Path(source.removeprefix('oci:').rsplit(':', 1)[0])
                before = {p.relative_to(layout).as_posix(): node.digest(p)
                          for p in layout.rglob('*') if p.is_file()}
                with lock:
                    layouts.append((layout, before))
                uploads.wait()  # Both native uploads must overlap.
                transfer(command, **kwargs)
                self.assertEqual(before, {p.relative_to(layout).as_posix(): node.digest(p)
                                          for p in layout.rglob('*') if p.is_file()})

        with patch.object(node, 'inspect_oci', side_effect=inspect_archive):
            result = self.publish(inspect, run)
        self.assertEqual(result['digest'], self.document['index_digest'])
        self.assertEqual(len(layouts), 2)
        self.assertEqual(layouts[0], layouts[1])
        self.assertEqual(set(paths), set(node.ARCHES))
        self.assertEqual(len(set(paths.values())), 2)
        self.assertTrue(all(not path.exists() for path in paths.values()))

    def test_failed_readback_waits_for_other_architecture_and_never_writes_index(self):
        inspect, transfer, transfers = self.transfer_fixture()
        held, failed, release, finished, returned = (threading.Event() for _ in range(5))
        errors, roots, index_reads = [], [], []
        original_inspect = node.inspect_oci
        reference = node.REPOSITORY + ':' + VERSION

        def inspect_reference(ref, auth, **kwargs):
            if ref == reference:
                index_reads.append(ref)
                self.fail('index inspected after a failed architecture')
            return inspect(ref, auth, **kwargs)

        def inspect_archive(path, arch, **kwargs):
            result = original_inspect(path, arch, **kwargs)
            finished.set()
            return result

        def run(command, **kwargs):
            self.assertTrue(command[-1].startswith('oci-archive:'))
            path = Path(command[-1].removeprefix('oci-archive:'))
            if path.parent.name == 'x86_64':
                self.assertTrue(held.wait(5))
                failed.set()
                raise ValueError('controlled node readback failure')
            roots.append(path.parent)
            held.set()
            self.assertTrue(release.wait(5))
            auth = Path(command[command.index('--authfile') + 1])
            self.assertEqual(json.loads(auth.read_text()), {'auths': {}})
            self.assertTrue(path.parent.is_dir())
            transfer(command, **kwargs)

        def publish():
            try:
                self.publish(inspect_reference, run)
            except BaseException as error:
                errors.append(error)
            finally:
                returned.set()

        with patch.object(node, 'inspect_oci', side_effect=inspect_archive):
            worker = threading.Thread(target=publish)
            worker.start()
            try:
                self.assertTrue(failed.wait(5))
                self.assertFalse(returned.wait(0.1))
                self.assertTrue(roots[0].is_dir())
            finally:
                release.set()
                worker.join(6)
            self.assertFalse(worker.is_alive())
        self.assertTrue(finished.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertIn('controlled node readback failure', str(errors[0]))
        self.assertFalse(index_reads)
        self.assertTrue(all(not path.exists() for path in roots))

    def test_failed_complete_preflight_never_builds_layout_or_starts_transport(self):
        with patch.object(subject, 'validate_stage', side_effect=ValueError('stage rejected')), \
             patch.object(subject, 'layout_from_archives') as layout, \
             patch.object(subject, 'ThreadPoolExecutor') as pool, \
             patch.object(subject.registry, 'inspect_raw') as inspect, \
             patch.object(subject.subprocess, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'stage rejected'):
                subject.publish(self.root, VERSION, REVISION, {}, {}, {})
            layout.assert_not_called()
            pool.assert_not_called()
            inspect.assert_not_called()
            command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
