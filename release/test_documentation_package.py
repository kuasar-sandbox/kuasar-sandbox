"""Exercise the delivered documentation layout and independent kernel selection."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
OWNERS = ('platform', 'accelerator', 'connector', 'guest-runtime', 'sandboxer', 'orchestrator')


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
            script = source / 'test/e2e/run_all.sh'
            script.write_text('#!/bin/sh\nprintf "owner suite\\n"\n')
            script.chmod(0o755)
        platform = self.root / 'platform'
        (platform / 'test/e2e/platform').mkdir()
        (platform / 'test/e2e/platform/run_all.sh').write_bytes((platform / 'test/e2e/run_all.sh').read_bytes())
        (platform / 'test/e2e/platform/run_all.sh').chmod(0o755)
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
        body = ('[spec](docs/switch.md#wire)\n[build][native]\n'
                '[native]: native-deps/README.md "Build guide"\n'
                '[`implementation`](cmd/main.go)\n'
                '`[literal](does-not-exist)`\n'
                '```sh\necho "[not-a-link](does-not-exist)"\n```\n')
        (source / 'README.md').write_text(header + body)
        (source / 'README_zh.md').write_text(header + body)
        (source / 'docs/switch.md').write_text('# Wire\n[back](../README.md)\n')
        (source / 'native-deps').mkdir()
        (source / 'native-deps/README.md').write_text('# Build\n[spec](../docs/switch.md#wire)\n')
        (source / 'cmd').mkdir()
        (source / 'cmd/main.go').write_text('package main\n')
        output, _ = self.assemble()
        text = (output / 'docs/connector.md').read_text()
        self.assertIn('[English](connector.md) | [简体中文](connector_zh.md)', text)
        self.assertIn('[spec](switch.md#wire)', text)
        self.assertIn('[native]: connector/native-deps/README.md "Build guide"', text)
        self.assertIn('https://github.com/kuasar-sandbox/connector/blob/connector-revision/cmd/main.go', text)
        self.assertIn('`[literal](does-not-exist)`', text)
        self.assertIn('echo "[not-a-link](does-not-exist)"', text)
        self.assertIn('[spec](../../switch.md#wire)', (output / 'docs/connector/native-deps/README.md').read_text())
        for owner in OWNERS:
            source = self.root / owner / ('test/e2e/platform' if owner == 'platform' else 'test/e2e') / 'run_all.sh'
            target = output / 'test/e2e' / owner / 'run_all.sh'
            self.assertEqual(source.read_bytes(), target.read_bytes())
            self.assertTrue(os.access(target, os.X_OK))
        self.assertFalse((output / 'test/e2e/assemble.sh').exists())
        self.assertFalse((output / 'test/e2e/assemble_docs.py').exists())

    def test_kernel_pair_uses_independently_selected_source(self):
        for owner, prefix in [('guest-runtime', 'runtime'), ('vmlinux', 'selected')]:
            for suffix in ('', '_zh'):
                (self.root / owner / f'docs/vmlinux{suffix}.md').write_text(f'# {prefix} kernel {suffix}\n')
        output, _ = self.assemble(kernel=True)
        for suffix in ('', '_zh'):
            self.assertEqual((output / f'docs/vmlinux{suffix}.md').read_text(), f'# selected kernel {suffix}\n')

    def test_missing_selected_kernel_translation_does_not_leak_runtime_copy(self):
        (self.root / 'guest-runtime/docs/vmlinux_zh.md').write_text('# Wrong revision\n')
        (self.root / 'vmlinux/docs/vmlinux.md').write_text('# Selected older kernel\n')
        output, _ = self.assemble(kernel=True)
        self.assertFalse((output / 'docs/vmlinux_zh.md').exists())

    def test_cross_repository_links_and_historical_revisions(self):
        (self.root / 'connector/docs/switch.md').write_text('# Wire\n')
        (self.root / 'connector/cmd').mkdir()
        (self.root / 'connector/cmd/main.go').write_text('package main\n')
        (self.root / 'platform/docs/overview.md').write_text(
            '[current](https://github.com/kuasar-sandbox/connector/blob/main/docs/switch.md#wire)\n'
            '[history](https://github.com/kuasar-sandbox/connector/blob/v0.0.1/docs/switch.md#wire)\n'
            '[code](https://github.com/kuasar-sandbox/connector/blob/main/cmd/main.go?plain=1#L1)\n'
            '[directory](https://github.com/kuasar-sandbox/connector/tree/main/cmd)\n'
            '[selected](https://github.com/kuasar-sandbox/connector/blob/connector-revision/cmd/main.go)\n'
            '[old code](https://github.com/kuasar-sandbox/connector/blob/v0.0.1/cmd/main.go)\n'
            '[newer](https://github.com/kuasar-sandbox/connector/blob/main/cmd/newer.go)\n')
        output, _ = self.assemble()
        text = (output / 'docs/overview.md').read_text()
        self.assertIn('[current](switch.md#wire)', text)
        self.assertIn('/blob/v0.0.1/docs/switch.md#wire', text)
        self.assertIn('/blob/connector-revision/cmd/main.go?plain=1#L1', text)
        self.assertIn('/tree/connector-revision/cmd)', text)
        self.assertIn('[selected](https://github.com/kuasar-sandbox/connector/blob/connector-revision/cmd/main.go)', text)
        self.assertIn('/blob/v0.0.1/cmd/main.go', text)
        self.assertIn('/blob/main/cmd/newer.go', text)
        self.assertNotIn('/blob/main/cmd/main.go', text)

    def test_collisions_are_rejected(self):
        for owner in ('accelerator', 'connector'):
            (self.root / owner / 'docs/same.md').write_text('# Same\n')
        _, result = self.assemble(success=False)
        self.assertIn('documentation collision', result.stderr)

    def test_source_symlinks_are_rejected(self):
        (self.root / 'connector/docs/link.md').symlink_to('../README.md')
        _, result = self.assemble(success=False)
        self.assertIn('symbolic link', result.stderr)


if __name__ == '__main__':
    unittest.main()
