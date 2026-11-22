#!/usr/bin/env python3
"""Exercise native payload admission and source identity without running products."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import artifacts
from test_fixtures import native_cluster_elf as elf, native_cluster_fixtures, native_cluster_elf_record

native = artifacts.cluster_native_module()
import archive_paths


class NativePayloadContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='native-contract-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lock, self.fixtures = native_cluster_fixtures()
        self.addCleanup(patch.stopall)
        patch.object(native, 'LOCK', self.lock).start()
        # Native bytes and ELF ABI closure have a separate real-product gate.
        # Here only readelf's data response is replaced; path/hash/mode/source
        # admission and tar extraction all run against actual filesystem bytes.
        self.inspect = patch.object(native, 'inspect_elf', side_effect=self.elf_record).start()

    def elf_record(self, path, arch):
        return native_cluster_elf_record(path, arch)

    def tree(self, arch='x86_64', name='bundle'):
        root = self.root / name
        for path, (data, mode) in self.fixtures[arch].items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(mode)
        return root

    def archive(self, rows, name='products.tar.gz'):
        path = self.root / name
        with tarfile.open(path, 'w:gz') as output:
            for name, (data, mode) in rows.items():
                entry = tarfile.TarInfo('./' + name)
                entry.size, entry.mode = len(data), mode
                output.addfile(entry, io.BytesIO(data))
        return path

    def test_native_release_both_architectures_extract_the_complete_closure(self):
        for arch in ('x86_64', 'aarch64'):
            with self.subTest(arch=arch):
                source = self.archive(self.fixtures[arch], arch + '.tar.gz')
                output = self.root / arch
                artifacts.unpack(source, output, 'orchestrator', {})
                self.assertEqual(set(artifacts.tree_files(output)), set(self.fixtures[arch]))
                self.assertEqual(set(native.validate(output, arch)), set(self.lock['architectures'][arch]['files']))
                generic = self.root / (arch + '-aggregate')
                archive_paths.extract([source], generic)
                self.assertEqual(artifacts.tree_files(generic), artifacts.tree_files(output))

    def test_incomplete_extra_corrupt_or_wrong_mode_archives_fail_before_extraction(self):
        variants = []
        for missing in (native.LAUNCHER, native.MANIFEST, 'lib/cluster-ctl/libfdb_c.so',
                        'share/licenses/orchestrator/native/foundationdb/LICENSE'):
            rows = copy.deepcopy(self.fixtures['x86_64'])
            del rows[missing]
            variants.append(rows)
        rows = copy.deepcopy(self.fixtures['x86_64'])
        rows['lib/cluster-ctl/injected.so'] = (b'extra', 0o644)
        variants.append(rows)
        for altered in (native.LAUNCHER, native.MANIFEST, 'lib/cluster-ctl/libfdb_c.so'):
            rows = copy.deepcopy(self.fixtures['x86_64'])
            rows[altered] = (rows[altered][0] + b'changed', rows[altered][1])
            variants.append(rows)
        rows = copy.deepcopy(self.fixtures['x86_64'])
        rows['lib/cluster-ctl/libc.so.6'] = (rows['lib/cluster-ctl/libc.so.6'][0], 0o755)
        variants.append(rows)
        for number, rows in enumerate(variants):
            with self.subTest(variant=number):
                source = self.archive(rows)
                target = self.root / 'rejected'
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    artifacts.unpack(source, target, 'orchestrator', {})
                self.assertFalse(target.exists())

    def test_attachments_cannot_be_owned_by_another_component(self):
        source = self.archive(self.fixtures['x86_64'])
        for unit in ('sandboxer', 'platform', 'runtime'):
            with self.subTest(unit=unit), self.assertRaisesRegex(ValueError, 'cannot own'):
                artifacts.unpack(source, self.root / 'rejected', unit, {})
        with self.assertRaisesRegex(ValueError, 'cannot own'):
            archive_paths.validate_aggregate([('sandboxer', source)])

    def test_dynamic_native_product_without_attachments_is_rejected(self):
        source = self.archive({native.PROGRAM: (elf('x86_64'), 0o755)})
        with self.assertRaisesRegex(ValueError, 'missing its native runtime'):
            artifacts.unpack(source, self.root / 'rejected', 'orchestrator', {})
        self.assertFalse((self.root / 'rejected').exists())
        with self.assertRaisesRegex(ValueError, 'missing its native runtime'):
            archive_paths.extract([source], self.root / 'rejected')

    def test_legacy_static_and_non_fdb_products_do_not_invent_a_runtime(self):
        for number, data in enumerate((b'legacy', elf('x86_64', 'libc.so.6'))):
            source = self.archive({native.PROGRAM: (data, 0o755)})
            target = self.root / str(number)
            artifacts.unpack(source, target, 'orchestrator', {})
            self.assertIsNone(native.validate(target, 'x86_64'))
            with self.assertRaisesRegex(ValueError, 'receipt has no payload'):
                native.verify_delta(target, 'x86_64', {'products': {'cluster-ctl': {}}, 'cluster_native': None}, {})

    def test_source_receipt_is_bound_to_the_same_selected_product(self):
        root = self.tree()
        sources = {'cluster-ctl': {'kuasar-sandbox/orchestrator': 'a' * 40}}
        metadata = {'products': {'cluster-ctl': {}}, 'cluster_native': {
            'files': native.validate(root, 'x86_64'), 'sources': sources['cluster-ctl']}}
        self.assertEqual(native.verify_delta(root, 'x86_64', metadata, sources),
                         set(self.lock['architectures']['x86_64']['files']))
        for change in ('missing', 'sources', 'digest', 'unplanned'):
            candidate = copy.deepcopy(metadata)
            if change == 'missing':
                del candidate['cluster_native']
            elif change == 'sources':
                candidate['cluster_native']['sources'] = {'kuasar-sandbox/orchestrator': 'b' * 40}
            elif change == 'digest':
                candidate['cluster_native']['files'][native.LAUNCHER] = '0' * 64
            else:
                candidate['products'] = {}
            with self.subTest(change=change), self.assertRaises(ValueError):
                native.verify_delta(root, 'x86_64', candidate, sources)
        with self.assertRaisesRegex(ValueError, 'architecture mismatch'):
            native.verify_delta(root, 'aarch64', metadata, sources)

    def test_source_collection_uses_regular_bundle_and_local_links(self):
        source = self.root / 'source'
        self.tree(name='source/cluster-runtime')
        (source / 'cluster-ctl').symlink_to('cluster-runtime/bin/cluster-ctl')
        destination = self.root / 'collected'
        destination.mkdir()
        native.collect(source, destination, 'x86_64')
        for name in ('cluster-ctl', 'run-cluster-ctl'):
            self.assertEqual((destination / name).readlink().as_posix(), 'cluster-runtime/bin/' + name)
        self.assertEqual(artifacts.tree_files(destination / 'cluster-runtime'),
                         artifacts.tree_files(source / 'cluster-runtime'))
        native.remove(destination / 'cluster-runtime')
        self.assertEqual(set(artifacts.tree_files(destination / 'cluster-runtime')), {native.PROGRAM})

    def test_native_source_cannot_fall_back_to_the_bare_binary(self):
        source = self.root / 'bare'
        source.mkdir()
        (source / 'cluster-ctl').write_bytes(elf('x86_64'))
        with self.assertRaisesRegex(ValueError, 'lacks cluster-runtime'):
            native.collect(source, self.root / 'collected', 'x86_64')

    def test_stale_native_bundle_cannot_replace_the_selected_source_product(self):
        source = self.root / 'source'
        self.tree(name='source/cluster-runtime')
        (source / 'cluster-ctl').write_bytes(b'newly built different product')
        with self.assertRaisesRegex(ValueError, 'differs from the selected'):
            native.source_bundle(source, 'x86_64')

    def test_symbolic_ancestors_and_files_are_rejected(self):
        root = self.tree()
        library = root / 'lib/cluster-ctl/libc.so.6'
        library.unlink()
        library.symlink_to('/etc/passwd')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            native.validate(root, 'x86_64')
        root = self.tree(name='ancestor')
        (root / 'bin').rename(self.root / 'external-bin')
        (root / 'bin').symlink_to(self.root / 'external-bin', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            native.validate(root, 'x86_64')

    def test_binary_dependency_and_symbol_versions_must_fit_the_bundle(self):
        root = self.tree()
        def wrong(path, arch):
            record = self.elf_record(path, arch)
            if path.name == 'cluster-ctl':
                record['needed'].append('libnotbundled.so')
            return record
        self.inspect.side_effect = wrong
        with self.assertRaisesRegex(ValueError, 'unbundled'):
            native.validate(root, 'x86_64')
        def future(path, arch):
            record = self.elf_record(path, arch)
            if path.name == 'cluster-ctl':
                record['requirements']['libc.so.6'] = {'GLIBC_99.0'}
            return record
        self.inspect.side_effect = future
        with self.assertRaisesRegex(ValueError, 'ABI not supplied'):
            native.validate(root, 'x86_64')

    def test_native_inputs_select_only_the_cluster_product(self):
        for name in ('cluster-native.py', 'cluster-native-lock.json', 'run-cluster-ctl',
                     'deb_extract.py', 'native-licenses/LICENSE'):
            self.assertEqual(artifacts.changed_products({'orchestrator': ['scripts/' + name]}), ['cluster-ctl'])
        self.assertNotIn('run-cluster-ctl', artifacts.GO_PRODUCTS)
        self.assertNotIn('run-cluster-ctl', artifacts.PRODUCTS)

    def test_image_context_retains_only_the_admitted_private_libraries(self):
        spec = importlib.util.spec_from_file_location('native_image_builder',
            artifacts.ROOT / 'node-infra/image/build.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        root = self.tree()
        context = self.root / 'image'
        context.mkdir()
        builder.copy_private_runtime(root, context, 'x86_64')
        self.assertEqual(artifacts.tree_files(context / 'lib'), artifacts.tree_files(root / 'lib'))
        self.assertIn('COPY lib/ /opt/node-infra/lib/',
                      (artifacts.ROOT / 'node-infra/image/Containerfile').read_text())


if __name__ == '__main__':
    unittest.main()
