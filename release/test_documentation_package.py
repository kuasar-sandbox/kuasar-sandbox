"""Exercise the delivered documentation layout and independent kernel selection."""
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile
import tarfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'test/e2e'))
from package_inputs import PLATFORM_RUNTIME
FIXTURE_GUIDES = {'platform': ['README',
              'docs/quickstart',
              'docs/download',
              'docs/deployment',
              'docs/kuasar-sandbox',
              'docs/terminology',
              'test/README',
              'test/QUICKSTART',
              'test/demo/DEMO',
              'workbench/README'],
 'accelerator': ['README',
                 'docs/cache',
                 'docs/cache-redis',
                 'docs/store',
                 'docs/manifest',
                 'docs/file-artifacts',
                 'test/e2e/README'],
 'connector': ['README', 'docs/tapfd', 'docs/vswitch-operations'],
 'guest-runtime': ['README',
                   'docs/flatten',
                   'docs/sandbox-runtime',
                   'docs/vmlinux',
                   'test/e2e/README'],
 'sandboxer': ['README', 'docs/sandbox', 'docs/sandbox-init'],
 'orchestrator': ['README',
                  'docs/node',
                  'docs/node-build',
                  'docs/node-proxy',
                  'docs/node-resource',
                  'docs/node-journald',
                  'docs/telemetry'],
 'vmlinux': ['docs/vmlinux']}

OWNERS = ('platform', 'accelerator', 'connector', 'guest-runtime', 'sandboxer', 'orchestrator')
SPEC = importlib.util.spec_from_file_location('check_docs', ROOT / 'ci/check_docs.py')
DOCS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DOCS)


class DocumentationPackageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for owner in (*OWNERS, 'vmlinux'):
            source = self.root / owner
            (source / 'docs').mkdir(parents=True)
            (source / 'test/e2e').mkdir(parents=True)
            (source / 'README.md').write_text(f'# {owner}\n')
            cases = source / ('test/e2e/platform/cases' if owner == 'platform' else 'test/e2e/cases')
            cases.mkdir(parents=True)
            script = cases / f'basic.{owner}-fixture.sh'
            script.write_text('#!/bin/sh\nprintf "owner suite\\n"\n')
            script.chmod(0o755)
        for owner in (*OWNERS, 'vmlinux'):
            source = self.root / owner
            for stem in FIXTURE_GUIDES[owner]:
                path = source / (stem + '.md')
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists(): path.write_text('# User guide\n')
            if owner in {'accelerator', 'guest-runtime'}:
                for suffix in ('', '_zh'):
                    (source / f'test/e2e/README{suffix}.md').write_text('[English](README.md) | [简体中文](README_zh.md)\n# Guide fixture\n')
            if owner in OWNERS:
                suite = source / ('test/e2e/platform' if owner == 'platform' else 'test/e2e')
                path = suite / 'lib' / 'runtime_fixture.py'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('# Runtime fixture\n')
            if owner in OWNERS:
                paths = [stem + '.md' for stem in FIXTURE_GUIDES[owner]]
                if owner in {'accelerator', 'guest-runtime'}:
                    paths.append('test/e2e/README_zh.md')
                # Fixtures own their declarations independently of the assembler.
                # Optional translations use a local pattern, except the explicitly
                # maintained E2E pair whose missing peer must still fail.
                declaration = source / 'release/guide-inputs.txt'
                declaration.parent.mkdir()
                declaration.write_text('\n'.join(paths) + '\n')
        platform = self.root / 'platform'
        for name in PLATFORM_RUNTIME:
            path = platform / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, path)

        (platform / 'test/e2e/lib').mkdir(parents=True, exist_ok=True)
        runner = platform / 'test/e2e/e2e'
        shutil.copyfile(ROOT / 'test/e2e/e2e', runner)
        runner.chmod(0o755)
        (platform / 'test/e2e/lib/common.sh').write_text('# common E2E helpers\\n')
        shutil.copyfile(ROOT / 'test/e2e/lib/workspace.py', platform / 'test/e2e/lib/workspace.py')
        shutil.copyfile(ROOT / 'test/e2e/lib/demo_wheels.py', platform / 'test/e2e/lib/demo_wheels.py')
        (self.root / 'refs.tsv').write_text(''.join(f'{o}\t{o}-revision\n' for o in (*OWNERS, 'vmlinux')))

    def assemble(self, *, kernel=False, success=True):
        for owner in OWNERS:
            declaration = self.root / owner / 'release/guide-inputs.txt'
            entries = declaration.read_text().splitlines()
            for path in tuple(entries):
                translated = path.removesuffix('.md') + '_zh.md'
                if path.endswith('.md') and not path.endswith('_zh.md') and (self.root / owner / translated).exists() and translated not in entries:
                    entries.append(translated)
            declaration.write_text('\n'.join(entries) + '\n')
        output = self.root / 'assembled'
        env = {**os.environ, 'DOCS_SOURCE_REFS': str(self.root / 'refs.tsv')}
        if kernel:
            env['DOCS_VMLINUX_SOURCE'] = str(self.root / 'vmlinux')
        result = subprocess.run(['bash', str(ROOT / 'test/e2e/assemble.sh'), str(output),
                                 *(str(self.root / owner) for owner in OWNERS)],
                                env=env, capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return output, result

    def test_owner_declared_directory_discovers_new_guides_and_preserves_paths(self):
        root = self.root / 'connector'
        directory = root / 'docs/user/nested'
        directory.mkdir(parents=True)
        (directory / 'added.md').write_text('# New owner guide\n')
        (root / 'release/guide-inputs.txt').write_text('README.md\ndocs/user/\n')
        output, _ = self.assemble()
        self.assertEqual((output / 'guide/connector/user/nested/added.md').read_text(),
                         '# New owner guide\n')
        self.assertFalse((output / 'guide/connector/tapfd.md').exists())

    def test_platform_directory_keeps_equal_basenames_in_distinct_subdirectories(self):
        source = self.root / 'platform'
        declaration = source / 'release/guide-inputs.txt'
        declaration.write_text(declaration.read_text() + 'docs/user/\n')
        for name in ('a', 'b'):
            guide = source / 'docs/user' / name / 'README.md'
            guide.parent.mkdir(parents=True)
            guide.write_text(f'# {name} guide\n')
        output, _ = self.assemble()
        for name in ('a', 'b'):
            self.assertEqual((output / f'guide/user/{name}/README.md').read_text(), f'# {name} guide\n')

    def test_recursive_directory_glob_discovers_each_file_once(self):
        source = self.root / 'connector'
        nested = source / 'docs/user/nested/new.md'
        nested.parent.mkdir(parents=True)
        nested.write_text('# Nested guide\n')
        (source / 'release/guide-inputs.txt').write_text('README.md\ndocs/**\n')
        output, _ = self.assemble()
        self.assertEqual((output / 'guide/connector/user/nested/new.md').read_text(), '# Nested guide\n')

    def test_missing_owner_declaration_has_no_cross_repository_fallback(self):
        declaration = self.root / 'connector/release/guide-inputs.txt'
        declaration.write_text('# Empty owner selection\n')
        _, result = self.assemble(success=False)
        self.assertIn('empty documentation declaration', result.stderr)

    def test_owner_declaration_rejects_unsafe_duplicate_and_unmatched_patterns(self):
        declaration = self.root / 'connector/release/guide-inputs.txt'
        for entries, message in [('README.md\nREADME*.md\n', 'duplicate documentation input'),
                                 ('../README.md\n', 'unsafe documentation input pattern'),
                                 ('/etc/passwd\n', 'unsafe documentation input pattern'),
                                 ('docs/absent*.md\n', 'missing documentation input'),
                                 ('README.md\ntest/e2e/lib/runtime_fixture.py\n', 'non-Markdown')]:
            with self.subTest(entries=entries):
                declaration.write_text(entries)
                _, result = self.assemble(success=False)
                self.assertIn(message, result.stderr)
                shutil.rmtree(self.root / 'assembled')

    def test_owner_declaration_symlink_and_selected_directory_symlink_fail(self):
        declaration = self.root / 'connector/release/guide-inputs.txt'
        saved = declaration.read_text()
        external = self.root / 'selection.txt'
        external.write_text(saved)
        declaration.unlink()
        declaration.symlink_to(external)
        # Call the reader directly: the fixture author helper intentionally
        # updates declarations before assembly, so must not follow this symlink.
        from package_inputs import guide_inputs
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            guide_inputs(self.root / 'connector')
        declaration.unlink()
        declaration.write_text('docs/user/\n')
        (self.root / 'connector/docs/user').symlink_to(self.root / 'sandboxer/docs', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            guide_inputs(self.root / 'connector')
        (self.root / 'connector/docs/user').unlink()
        declaration.unlink()
        with self.assertRaisesRegex(ValueError, 'missing package input'):
            guide_inputs(self.root / 'connector')

    def test_bilingual_navigation_and_source_links(self):
        source = self.root / 'connector'
        header = '[English](README.md) | [简体中文](README_zh.md)\n'
        body = ('[spec](docs/tapfd.md#wire)\n[build][native]\n'
                '[native]: native-deps/README.md "Build guide"\n'
                '[`implementation`](cmd/main.go)\n'
                '`[literal](does-not-exist)`\n'
                '```sh\necho "[not-a-link](does-not-exist)"\n```\n')
        (source / 'README.md').write_text(header + body)
        (source / 'README_zh.md').write_text(header + body)
        (source / 'docs/tapfd.md').write_text('# Wire\n[back](../README.md)\n')
        (source / 'native-deps').mkdir()
        (source / 'native-deps/README.md').write_text('# Build\n[spec](../docs/tapfd.md#wire)\n')
        (source / 'cmd').mkdir()
        (source / 'cmd/main.go').write_text('package main\n')
        output, _ = self.assemble()
        text = (output / 'guide/connector/README.md').read_text()
        self.assertIn('[English](README.md) | [简体中文](README_zh.md)', text)
        self.assertIn('[spec](tapfd.md#wire)', text)
        self.assertIn('[native]: https://github.com/kuasar-sandbox/connector/blob/connector-revision/native-deps/README.md "Build guide"', text)
        self.assertIn('https://github.com/kuasar-sandbox/connector/blob/connector-revision/cmd/main.go', text)
        self.assertIn('`[literal](does-not-exist)`', text)
        self.assertIn('echo "[not-a-link](does-not-exist)"', text)
        self.assertFalse((output / 'guide/connector/native-deps').exists())
        for owner in OWNERS:
            source = self.root / owner / ('test/e2e/platform/cases' if owner == 'platform' else 'test/e2e/cases') / f'basic.{owner}-fixture.sh'
            target = output / 'test/e2e/cases' / source.name
            self.assertEqual(source.read_bytes(), target.read_bytes())
            self.assertTrue(os.access(target, os.X_OK))
            self.assertFalse((output / 'test/e2e' / owner / 'cases').exists())
        self.assertTrue(os.access(output / 'test/e2e/e2e', os.X_OK))
        self.assertTrue((output / 'test/e2e/lib/common.sh').is_file())
        self.assertFalse((output / 'test/e2e/assemble.sh').exists())
        self.assertFalse((output / 'test/e2e/assemble_docs.py').exists())
        self.assertFalse((output / 'test/e2e/run_all.sh').exists())
        self.assertFalse((output / 'test/e2e/generated-compatibility-runners').exists())

    def test_kernel_pair_uses_independently_selected_source(self):
        for owner, prefix in [('guest-runtime', 'runtime'), ('vmlinux', 'selected')]:
            for suffix in ('', '_zh'):
                (self.root / owner / f'docs/vmlinux{suffix}.md').write_text(f'# {prefix} kernel {suffix}\n')
        output, _ = self.assemble(kernel=True)
        for suffix in ('', '_zh'):
            self.assertEqual((output / f'guide/vmlinux/vmlinux{suffix}.md').read_text(), f'# selected kernel {suffix}\n')

    def test_missing_selected_kernel_translation_does_not_leak_runtime_copy(self):
        (self.root / 'guest-runtime/docs/vmlinux_zh.md').write_text('# Wrong revision\n')
        (self.root / 'vmlinux/docs/vmlinux.md').write_text('# Selected older kernel\n')
        output, _ = self.assemble(kernel=True)
        self.assertFalse((output / 'guide/vmlinux/vmlinux_zh.md').exists())

    def test_kernel_legal_links_do_not_replace_runtime_legal_identity(self):
        for owner in ('guest-runtime', 'vmlinux'):
            (self.root / owner / 'LICENSE').write_text(owner + ' legal bytes\n')
        (self.root / 'guest-runtime/README.md').write_text('[License](LICENSE)\n')
        (self.root / 'vmlinux/docs/vmlinux.md').write_text('[License](../LICENSE)\n')
        output, _ = self.assemble(kernel=True)
        for owner, filename in [('guest-runtime', 'README.md'), ('vmlinux', 'vmlinux.md')]:
            self.assertIn(f'../licenses/{owner}/LICENSE', (output / 'guide' / owner / filename).read_text())
            self.assertEqual((output / 'guide/licenses' / owner / 'LICENSE').read_text(), owner + ' legal bytes\n')

    def test_missing_maintained_e2e_translation_is_rejected(self):
        (self.root / 'accelerator/test/e2e/README_zh.md').unlink()
        _, result = self.assemble(success=False)
        self.assertIn('missing documentation input: test/e2e/README_zh.md', result.stderr)

    def test_cross_repository_links_and_historical_revisions(self):
        (self.root / 'connector/docs/tapfd.md').write_text('# Wire\n')
        (self.root / 'connector/README_zh.md').write_text('# Connector Chinese\n')
        (self.root / 'connector/native-deps').mkdir()
        for name in ('README.md', 'README_zh.md'):
            (self.root / 'connector/native-deps' / name).write_text('# Native\n')
        (self.root / 'connector/cmd').mkdir()
        (self.root / 'connector/cmd/main.go').write_text('package main\n')
        (self.root / 'platform/docs/quickstart.md').write_text(
            '[current](https://github.com/kuasar-sandbox/connector/blob/main/docs/tapfd.md#wire)\n'
            '[history](https://github.com/kuasar-sandbox/connector/blob/v0.0.1/docs/tapfd.md#wire)\n'
            '[code](https://github.com/kuasar-sandbox/connector/blob/main/cmd/main.go?plain=1#L1)\n'
            '[directory](https://github.com/kuasar-sandbox/connector/tree/main/cmd)\n'
            '[selected](https://github.com/kuasar-sandbox/connector/blob/connector-revision/cmd/main.go)\n'
            '[old code](https://github.com/kuasar-sandbox/connector/blob/v0.0.1/cmd/main.go)\n'
            '[newer](https://github.com/kuasar-sandbox/connector/blob/main/cmd/newer.go)\n')
        directory_links = (
            '[docs](https://github.com/kuasar-sandbox/connector/tree/main/docs)\n'
            '[native](https://github.com/kuasar-sandbox/connector/tree/main/native-deps)\n'
            '[anchor](https://github.com/kuasar-sandbox/connector/tree/main/docs#connector)\n'
            '[explicit English](https://github.com/kuasar-sandbox/connector/blob/main/README.md)\n'
            '[English-only](https://github.com/kuasar-sandbox/accelerator/tree/main/docs)\n')
        (self.root / 'platform/docs/deployment.md').write_text(directory_links)
        (self.root / 'platform/docs/deployment_zh.md').write_text(directory_links)
        output, _ = self.assemble()
        text = (output / 'guide/quickstart.md').read_text()
        self.assertIn('[current](connector/tapfd.md#wire)', text)
        self.assertIn('/blob/v0.0.1/docs/tapfd.md#wire', text)
        self.assertIn('/blob/connector-revision/cmd/main.go?plain=1#L1', text)
        self.assertIn('/tree/connector-revision/cmd)', text)
        self.assertIn('[selected](https://github.com/kuasar-sandbox/connector/blob/connector-revision/cmd/main.go)', text)
        self.assertIn('/blob/v0.0.1/cmd/main.go', text)
        self.assertIn('/blob/main/cmd/newer.go', text)
        self.assertNotIn('/blob/main/cmd/main.go', text)
        en = (output / 'guide/deployment.md').read_text()
        zh = (output / 'guide/deployment_zh.md').read_text()
        self.assertIn('[docs](connector/README.md)', en)
        self.assertIn('[docs](connector/README_zh.md)', zh)
        self.assertIn('[native](https://github.com/kuasar-sandbox/connector/tree/connector-revision/native-deps)', en)
        self.assertIn('[native](https://github.com/kuasar-sandbox/connector/tree/connector-revision/native-deps)', zh)
        self.assertIn('[anchor](connector/README.md#connector)', zh)
        self.assertIn('[explicit English](connector/README.md)', zh)
        self.assertIn('[English-only](accelerator/README.md)', zh)

    def test_owner_section_navigation_in_source_and_package(self):
        source = self.root / 'sandboxer'
        owners = {'sandbox': ('artifact-model', 'journal-output-targets',
                              'usage-query', 'usage-metrics', 'usage-persistence',
                              'read-recovery'),
                  'sandbox-init': ('usage-observations',),
                  'cloud-hypervisor': ('integration',)}
        for suffix in ('', '_zh'):
            navigation = ''.join(f'[{owner}](docs/{owner}{suffix}.md#{anchors[0]})\n'
                                 for owner, anchors in owners.items())
            for entry in ('README', 'CONTRIBUTING'):
                selector = f'[English]({entry}.md) | [简体中文]({entry}_zh.md)\n'
                (source / f'{entry}{suffix}.md').write_text(selector + navigation)
            for owner, anchors in owners.items():
                selector = f'[English]({owner}.md) | [简体中文]({owner}_zh.md)\n'
                sections = ''.join(f'<a id="{anchor}"></a>\n## {anchor}\n'
                                   for anchor in anchors)
                (source / f'docs/{owner}{suffix}.md').write_text(selector + sections)
            consumer = self.root / 'orchestrator/docs' / f'node{suffix}.md'
            consumer.write_text(
                '[English](node.md) | [简体中文](node_zh.md)\n'
                + ''.join(f'[{anchor}](https://github.com/kuasar-sandbox/sandboxer/'
                          f'blob/main/docs/{owner}{suffix}.md#{anchor})\n'
                          for owner, anchors in owners.items() for anchor in anchors))
        for root in (source, self.root / 'orchestrator'):
            for path in root.rglob('*.md'):
                self.assertEqual(DOCS.check_file(root, path), [], str(path))
        output, _ = self.assemble()
        for path in output.rglob('*.md'):
            self.assertEqual(DOCS.check_file(output, path), [], str(path))
        for suffix in ('', '_zh'):
            readme = (output / f'guide/sandboxer/README{suffix}.md').read_text()
            self.assertFalse((output / f'guide/sandboxer/CONTRIBUTING{suffix}.md').exists())
            consumer = (output / f'guide/orchestrator/node{suffix}.md').read_text()
            for owner, anchors in owners.items():
                if owner == 'cloud-hypervisor':
                    self.assertIn(f'/blob/sandboxer-revision/docs/{owner}{suffix}.md#integration', readme)
                    self.assertFalse((output / f'guide/sandboxer/{owner}{suffix}.md').exists())
                else:
                    self.assertIn(f'({owner}{suffix}.md#{anchors[0]})', readme)
                    for anchor in anchors:
                        self.assertIn(f'(../sandboxer/{owner}{suffix}.md#{anchor})', consumer)

    def test_audience_filtering_and_legal_bytes(self):
        excluded = {
            'platform': ('docs/ci.md', 'docs/release.md', 'docs/perf.md',
                         'test/demo/test_demo_safety.sh', 'test/perf/experimental.sh',
                         'test/e2e/lib/test_hidden.py'),
            'connector': ('docs/vswitch.md', 'test/e2e/lib/test_stats.py'),
            'sandboxer': ('docs/cloud-hypervisor.md',),
            'orchestrator': ('docs/extensions.md', 'docs/cluster-placer.md'),
        }
        for owner, names in excluded.items():
            for name in names:
                path = self.root / owner / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('SOURCE_ONLY_MARKER\n')
        legal = self.root / 'connector/LICENSES/GPL-2.0-only.txt'
        legal.parent.mkdir()
        legal.write_bytes(b'Legal attribution\n')
        output, _ = self.assemble()
        self.assertEqual((output / 'guide/licenses/connector/LICENSES/GPL-2.0-only.txt').read_bytes(), legal.read_bytes())
        for path in output.rglob('*'):
            if path.is_file(): self.assertNotIn(b'SOURCE_ONLY_MARKER', path.read_bytes(), str(path))
        self.assertTrue((output / 'workbench/workbench').is_file())
        self.assertTrue((output / 'test/demo/demo_e2b.sh').is_file())
        self.assertTrue((self.root / 'platform/test/demo/test_demo_safety.sh').is_file())

    def test_missing_runtime_input_is_rejected(self):
        shutil.rmtree(self.root / 'connector/test/e2e/lib')
        _, result = self.assemble(success=False)
        self.assertIn('runtime library directory', result.stderr)

    def test_new_nested_owner_inputs_survive_assembly_and_archive(self):
        # Regression for sandboxer #305: no aggregator filename list is edited
        # when an owner adds a runtime dependency beside its canonical cases.
        suite = self.root / 'sandboxer/test/e2e'
        helper = suite / 'lib/restore_dio_workload.py'
        helper.write_text('DIRECT_IO_WORKLOAD = "new owner dependency"\n')
        helper.chmod(0o640)
        nested = suite / 'lib/data/nested/payload.json'
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b'{"owner":"sandboxer"}\n')
        other = self.root / 'connector/test/e2e/lib/restore_dio_workload.py'
        other.write_text('CONNECTOR_PRIVATE = True\n')
        case = suite / 'cases/snapshot.restore-fixture.sh'
        case.write_text('#!/bin/sh\ncat "$SANDBOXER_LIB/restore_dio_workload.py"\n')
        output, _ = self.assemble()
        delivered = output / 'test/e2e/lib/sandboxer/restore_dio_workload.py'
        result = subprocess.run(['sh', str(output / 'test/e2e/cases' / case.name)],
                                env={**os.environ, 'SANDBOXER_LIB': str(delivered.parent)},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, helper.read_text())
        self.assertEqual(delivered.stat().st_mode & 0o777, 0o640)
        archive = self.root / 'platform.tar.gz'
        with tarfile.open(archive, 'w:gz') as package:
            package.add(output, arcname='.')
        with tarfile.open(archive) as package:
            for source, name in [(helper, 'sandboxer/restore_dio_workload.py'),
                                 (nested, 'sandboxer/data/nested/payload.json'),
                                 (other, 'connector/restore_dio_workload.py')]:
                self.assertEqual(package.extractfile('./test/e2e/lib/' + name).read(),
                                 source.read_bytes())

    def test_excluded_self_tests_and_caches_are_not_runtime_inputs(self):
        library = self.root / 'orchestrator/test/e2e/lib'
        for name in ('test_runtime.py', 'test_fixtures/payload',
                     '__pycache__/runtime.pyc', '.pytest_cache/state', 'stale.pyc'):
            path = library / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('SOURCE_ONLY_MARKER\n')
        output, _ = self.assemble()
        files = [p.relative_to(output / 'test/e2e/lib/orchestrator').as_posix()
                 for p in (output / 'test/e2e/lib/orchestrator').rglob('*') if p.is_file()]
        self.assertEqual(files, ['runtime_fixture.py'])

    def test_nested_and_excluded_library_symlinks_are_rejected(self):
        for name in ('nested/escape.py', 'test_escape.py', '__pycache__/escape'):
            with self.subTest(name=name):
                path = self.root / 'sandboxer/test/e2e/lib' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(self.root / 'sandboxer/README.md')
                _, result = self.assemble(success=False)
                self.assertIn('symbolic link', result.stderr)
                path.unlink()
                shutil.rmtree(self.root / 'assembled')

    def test_special_library_input_is_rejected(self):
        os.mkfifo(self.root / 'sandboxer/test/e2e/lib/pipe')
        _, result = self.assemble(success=False)
        self.assertIn('missing package input', result.stderr)

    def test_duplicate_case_is_still_rejected(self):
        case = self.root / 'sandboxer/test/e2e/cases/basic.connector-fixture.sh'
        case.write_text('exit 0\n')
        _, result = self.assemble(success=False)
        self.assertIn('duplicate E2E case ID', result.stderr)

    def test_source_symlinks_are_rejected(self):
        path = self.root / 'connector/docs/tapfd.md'
        path.unlink()
        path.symlink_to('../README.md')
        _, result = self.assemble(success=False)
        self.assertIn('symbolic link', result.stderr)


if __name__ == '__main__':
    unittest.main()
