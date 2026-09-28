"""Exercise the delivered documentation layout and independent kernel selection."""
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'test/e2e'))
from package_inputs import GUIDES, OWNER_LIBRARIES, PLATFORM_RUNTIME
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
            for stem in GUIDES[owner]:
                path = source / (stem + '.md')
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists(): path.write_text('# User guide\n')
            if owner in OWNER_LIBRARIES:
                suite = source / ('test/e2e/platform' if owner == 'platform' else 'test/e2e')
                for name in OWNER_LIBRARIES[owner]:
                    path = suite / 'lib' / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('# Runtime fixture\n')
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
        (self.root / 'connector/test/e2e/lib/notify_helpers.sh').unlink()
        _, result = self.assemble(success=False)
        self.assertIn('missing package input', result.stderr)

    def test_source_symlinks_are_rejected(self):
        path = self.root / 'connector/docs/tapfd.md'
        path.unlink()
        path.symlink_to('../README.md')
        _, result = self.assemble(success=False)
        self.assertIn('symbolic link', result.stderr)


if __name__ == '__main__':
    unittest.main()
