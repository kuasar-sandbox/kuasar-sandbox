#!/usr/bin/env python3
"""Exercise flat VPP delivery through the real product and archive readers."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import shutil
import struct
import tarfile
import tempfile
import unittest

import artifacts
from test_fixtures import native_egress_files

native = artifacts.egress_native_module()
import archive_paths


def elf(arch, marker):
    header = bytearray(64)
    header[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<H', header, 18, native.ARCHES[arch][0])
    return bytes(header) + marker.encode()


class EgressNativeContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='vpp-delivery-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fixture(self, arch='x86_64'):
        return native_egress_files(arch)

    def tree(self, rows, name='products'):
        root = self.root / name
        for name, (data, mode) in rows.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(mode)
        return root

    def archive(self, rows, name='connector.tar.gz'):
        path = self.root / name
        with tarfile.open(path, 'w:gz') as archive:
            for name, (data, mode) in rows.items():
                entry = tarfile.TarInfo('./' + name)
                entry.size, entry.mode = len(data), mode
                archive.addfile(entry, io.BytesIO(data))
        return path

    def test_both_architectures_keep_direct_files_through_component_and_aggregate_readers(self):
        for arch in native.ARCHES:
            with self.subTest(arch=arch):
                rows = self.fixture(arch)
                rows['bin/connector-ctl'] = (elf(arch, 'control'), 0o755)
                archive = self.archive(rows, arch + '.tar.gz')
                output = self.root / arch
                ownership = {}
                artifacts.unpack(archive, output, 'connector', ownership)
                self.assertEqual(set(artifacts.tree_files(output)), set(rows))
                self.assertEqual(set(ownership.values()), {'connector'})
                self.assertEqual(set(native.validate(output, arch)), set(rows) - {'bin/connector-ctl'})
                aggregate = self.root / (arch + '-aggregate')
                archive_paths.extract([archive], aggregate)
                self.assertEqual(artifacts.tree_files(aggregate), artifacts.tree_files(output))
                self.assertFalse((aggregate / 'lib/connector-egress').exists())

    def test_legacy_connector_without_vpp_still_extracts(self):
        archive = self.archive({'bin/connector-ctl': (elf('x86_64', 'legacy'), 0o755)})
        output = self.root / 'legacy'
        artifacts.unpack(archive, output, 'connector', {})
        self.assertIsNone(native.validate(output))
        archive_paths.extract([archive], self.root / 'legacy-aggregate')

    def test_incomplete_modified_extra_and_wrong_architecture_payloads_fail_before_extraction(self):
        rows = self.fixture()
        plugin = next(name for name in rows if name.endswith('_plugin.so'))
        variants = []
        for name in ('bin/' + native.MANIFEST, plugin, native.LICENSES + '/VPP-LICENSE'):
            altered = copy.deepcopy(rows)
            del altered[name]
            variants.append(altered)
        altered = copy.deepcopy(rows)
        altered[plugin] = (rows[plugin][0] + b'changed', 0o644)
        variants.append(altered)
        altered = copy.deepcopy(rows)
        altered['bin/lib/x86_64-linux-gnu/extra.so'] = (elf('x86_64', 'extra'), 0o644)
        variants.append(altered)
        altered = copy.deepcopy(rows)
        altered['bin/vpp'] = (elf('aarch64', 'wrong architecture'), 0o755)
        variants.append(altered)
        for index, altered in enumerate(variants):
            with self.subTest(index=index):
                archive = self.archive(altered)
                destination = self.root / 'rejected'
                with self.assertRaises(ValueError):
                    artifacts.unpack(archive, destination, 'connector', {})
                self.assertFalse(destination.exists())

    def test_only_connector_can_own_native_paths(self):
        archive = self.archive(self.fixture())
        for owner in ('sandboxer', 'orchestrator', 'runtime', 'platform'):
            with self.subTest(owner=owner), self.assertRaisesRegex(ValueError, 'cannot own'):
                artifacts.unpack(archive, self.root / 'rejected', owner, {})
        with self.assertRaisesRegex(ValueError, 'cannot own'):
            archive_paths.validate_aggregate([('sandboxer', archive)])

    def test_overlay_receipt_binds_libraries_licenses_and_source(self):
        root = self.tree(self.fixture())
        source = {'kuasar-sandbox/connector': 'a' * 40}
        metadata = {'products': {'vpp': {}},
                    'egress_native': {'files': native.validate(root, 'x86_64'), 'sources': source}}
        files = native.verify_delta(root, 'x86_64', metadata, {'vpp': source})
        self.assertIn('bin/vpp-runtime.json', files)
        self.assertIn(native.LICENSES + '/VPP-LICENSE', files)
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            native.verify_delta(root, 'x86_64', metadata, {'vpp': {'kuasar-sandbox/connector': 'b' * 40}})
        (root / native.LICENSES / 'VPP-LICENSE').write_text('changed license')
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            native.verify_delta(root, 'x86_64', metadata, {'vpp': source})

    def test_collect_and_replace_preserve_sibling_cli(self):
        source = self.tree(self.fixture(), 'build')
        output = self.root / 'delta'
        native.collect(source / 'bin', source / native.LICENSES, output, 'x86_64')
        selected = self.root / 'selected'
        (selected / 'bin').mkdir(parents=True)
        sibling = selected / 'bin/connector-ctl'
        sibling.write_bytes(b'selected connector')
        native.copy(output, selected, 'x86_64')
        self.assertEqual(native.validate(selected), native.validate(output))
        self.assertFalse((selected / 'bin/share').exists())
        native.remove(selected)
        self.assertEqual(sibling.read_bytes(), b'selected connector')
        self.assertIsNone(native.validate(selected))

    def test_node_image_keeps_vpp_in_the_existing_bin_copy(self):
        root = self.tree(self.fixture())
        spec = importlib.util.spec_from_file_location('vpp_node_image', artifacts.ROOT / 'node-infra/image/build.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        context = self.root / 'image'
        shutil.copytree(root / 'bin', context / 'bin')
        module.copy_private_runtime(root, context, 'x86_64')
        self.assertEqual(artifacts.tree_files(context / 'bin'), artifacts.tree_files(root / 'bin'))
        self.assertFalse((context / 'lib/connector-egress').exists())


if __name__ == '__main__':
    unittest.main()
