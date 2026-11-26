#!/usr/bin/env python3
"""Exercise static VPP delivery through the real product and archive readers."""
import copy
import hashlib
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
from test_fixtures import native_egress_elf, native_egress_files

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
                self.assertFalse((aggregate / 'bin/lib').exists())

    def test_legacy_connector_without_vpp_still_extracts(self):
        archive = self.archive({'bin/connector-ctl': (elf('x86_64', 'legacy'), 0o755)})
        output = self.root / 'legacy'
        artifacts.unpack(archive, output, 'connector', {})
        self.assertIsNone(native.validate(output))
        archive_paths.extract([archive], self.root / 'legacy-aggregate')

    def test_incomplete_modified_extra_and_wrong_architecture_payloads_fail_before_extraction(self):
        rows = self.fixture()
        program = 'bin/' + native.PROGRAM
        variants = []
        for name in ('bin/' + native.MANIFEST, program, native.LICENSES + '/VPP-LICENSE'):
            altered = copy.deepcopy(rows)
            del altered[name]
            variants.append(altered)
        altered = copy.deepcopy(rows)
        altered[program] = (rows[program][0] + b'changed', 0o755)
        variants.append(altered)
        altered = copy.deepcopy(rows)
        altered['bin/lib/x86_64-linux-gnu/extra.so'] = (elf('x86_64', 'extra'), 0o644)
        variants.append(altered)
        altered = copy.deepcopy(rows)
        altered['bin/connector-vpp'] = (elf('aarch64', 'wrong architecture'), 0o755)
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

    def test_static_system_notices_must_be_regular_nonempty_license_files(self):
        notice = native.LICENSES + '/system/libc6-dev/copyright'
        for variant in ('missing', 'empty', 'other-name'):
            with self.subTest(variant=variant):
                rows = self.fixture()
                content = rows.pop(notice)
                if variant == 'empty':
                    rows[notice] = (b'', 0o644)
                elif variant == 'other-name':
                    rows[native.LICENSES + '/system/libc6-dev/README'] = content
                with self.assertRaisesRegex(ValueError, 'system library license materials'):
                    artifacts.unpack(self.archive(rows), self.root / 'rejected', 'connector', {})
                self.assertFalse((self.root / 'rejected').exists())
                source = self.tree(rows, variant)
                with self.assertRaisesRegex(ValueError, 'system library license materials'):
                    native.collect(source / 'bin', source / native.LICENSES,
                                   self.root / (variant + '-overlay'), 'x86_64')
        for name in ('LICENSE.txt', 'COPYING.LIB', 'vendor-COPYRIGHT.txt'):
            with self.subTest(name=name):
                rows = self.fixture()
                rows[native.LICENSES + '/system/libc6-dev/' + name] = rows.pop(notice)
                self.assertIsNotNone(native.validate(self.tree(rows, name), 'x86_64'))
        source = self.tree(self.fixture(), 'symlink')
        path = source / notice
        path.unlink()
        path.symlink_to(source / native.LICENSES / 'VPP-LICENSE')
        with self.assertRaisesRegex(ValueError, 'may not contain links'):
            native.validate(source, 'x86_64')

    def test_static_elf_rejects_interpreter_needed_and_malformed_segments(self):
        for arch in native.ARCHES:
            variants = [native_egress_elf(arch, kind) for kind in ('interpreter', 'needed')]
            for offset, format_, value in ((56, '<H', 0), (32, '<Q', 4096), (96, '<Q', 4096)):
                data = bytearray(native_egress_elf(arch))
                struct.pack_into(format_, data, offset, value)
                variants.append(bytes(data))
            data = bytearray(native_egress_elf(arch, 'needed'))
            struct.pack_into('<Q', data, 176, 5)
            struct.pack_into('<Q', data, 192, 5)
            variants.append(bytes(data))
            for index, data in enumerate(variants):
                with self.subTest(arch=arch, index=index):
                    rows = self.fixture(arch)
                    rows['bin/' + native.PROGRAM] = (data, 0o755)
                    manifest = json.loads(rows['bin/' + native.MANIFEST][0])
                    manifest['files'][native.PROGRAM] = hashlib.sha256(data).hexdigest()
                    rows['bin/' + native.MANIFEST] = (json.dumps(manifest).encode(), 0o644)
                    archive = self.archive(rows)
                    with self.assertRaises(ValueError):
                        artifacts.unpack(archive, self.root / 'rejected', 'connector', {})
                    self.assertFalse((self.root / 'rejected').exists())

    def test_static_pie_is_admitted_without_system_libraries(self):
        rows = self.fixture()
        data = bytearray(rows['bin/' + native.PROGRAM][0])
        struct.pack_into('<H', data, 16, 3)
        rows['bin/' + native.PROGRAM] = (bytes(data), 0o755)
        manifest = json.loads(rows['bin/' + native.MANIFEST][0])
        manifest['files'][native.PROGRAM] = hashlib.sha256(data).hexdigest()
        rows['bin/' + native.MANIFEST] = (json.dumps(manifest).encode(), 0o644)
        root = self.tree(rows)
        self.assertEqual(native.inspect(root / 'bin')['required_system_libraries'], [])

    def test_manifest_requires_one_executable_and_no_system_dependencies(self):
        for libraries in (None, ['libc.so.6'], {}):
            with self.subTest(libraries=libraries):
                rows = self.fixture()
                manifest = json.loads(rows['bin/' + native.MANIFEST][0])
                manifest['required_system_libraries'] = libraries
                rows['bin/' + native.MANIFEST] = (json.dumps(manifest).encode(), 0o644)
                with self.assertRaisesRegex(ValueError, 'system libraries'):
                    artifacts.unpack(self.archive(rows), self.root / 'rejected', 'connector', {})
        rows = self.fixture()
        manifest = json.loads(rows['bin/' + native.MANIFEST][0])
        manifest['files']['lib/libvppinfra.so'] = 'a' * 64
        rows['bin/' + native.MANIFEST] = (json.dumps(manifest).encode(), 0o644)
        with self.assertRaisesRegex(ValueError, 'file manifest'):
            artifacts.unpack(self.archive(rows), self.root / 'rejected', 'connector', {})

    def test_overlay_receipt_binds_executable_licenses_and_source(self):
        root = self.tree(self.fixture())
        source = {'kuasar-sandbox/connector': 'a' * 40}
        metadata = {'products': {'connector-vpp': {}},
                    'egress_native': {'files': native.validate(root, 'x86_64'), 'sources': source}}
        files = native.verify_delta(root, 'x86_64', metadata, {'connector-vpp': source})
        self.assertIn('bin/vpp-runtime.json', files)
        self.assertIn(native.LICENSES + '/VPP-LICENSE', files)
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            native.verify_delta(root, 'x86_64', metadata, {'connector-vpp': {'kuasar-sandbox/connector': 'b' * 40}})
        (root / native.LICENSES / 'VPP-LICENSE').write_text('changed license')
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            native.verify_delta(root, 'x86_64', metadata, {'connector-vpp': source})

    def test_collect_and_replace_preserve_sibling_cli(self):
        source = self.tree(self.fixture(), 'build')
        output = self.root / 'delta'
        native.collect(source / 'bin', source / native.LICENSES, output, 'x86_64')
        selected = self.root / 'selected'
        (selected / 'bin').mkdir(parents=True)
        sibling = selected / 'bin/connector-ctl'
        sibling.write_bytes(b'selected connector')
        unrelated = selected / 'bin/lib/other-product/library.so'
        unrelated.parent.mkdir(parents=True)
        unrelated.write_bytes(b'owned by another product')
        native.copy(output, selected, 'x86_64')
        self.assertEqual(native.validate(selected), native.validate(output))
        self.assertFalse((selected / 'bin/share').exists())
        native.remove(selected)
        self.assertEqual(sibling.read_bytes(), b'selected connector')
        self.assertEqual(unrelated.read_bytes(), b'owned by another product')
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
        self.assertFalse((context / 'bin/lib').exists())


if __name__ == '__main__':
    unittest.main()
