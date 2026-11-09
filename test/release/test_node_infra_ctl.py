#!/usr/bin/env python3
"""Deployment journal and ownership regression tests; not runtime acceptance."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
loader = importlib.machinery.SourceFileLoader('node_infra_ctl', str(ROOT / 'node-infra/deploy/bin/node-infra-ctl'))
spec = importlib.util.spec_from_loader(loader.name, loader)
ctl = importlib.util.module_from_spec(spec)
loader.exec_module(ctl)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        for key, value in [('STATE', 'state'), ('RUN', 'run'), ('QUADLETS', 'quadlets'),
                           ('SYSTEMD', 'systemd'), ('RELEASES', 'releases')]:
            p = patch.object(ctl, key, self.root / value)
            p.start()
            self.addCleanup(p.stop)
        self.node = ctl.Instance('test')
        self.node.directory.mkdir(parents=True)

    def rendered(self):
        text = (ROOT / 'node-infra/deploy/templates/node-infra.container').read_text()
        for key, value in {'NAME': 'test', 'IMAGE': 'sha256:' + '1' * 64,
                           'NETNS': '/run/netns/test', 'CONFIG_ROOT': '/etc/node-infra/instances/test',
                           'DATA_ROOT': '/var/lib/node-infra/instances/test/data',
                           'RUN_ROOT': str(ctl.RUN / 'test')}.items():
            text = text.replace('@' + key + '@', value)
        return text.replace('/var/lib/node-infra/instances/test/recovery', str(self.node.recovery))

    def test_native_repeated_sections_and_reset(self):
        values = ctl.unit_values('[Container]\nVolume=/a:/a:ro\n[Service]\nCPUQuota=100%\n'
                                 '[Container]\nVolume=\nVolume=/b:/b:ro\n')
        self.assertEqual(values['Container', 'Volume'], ['/b:/b:ro'])
        self.assertEqual(ctl.one(values, 'Service', 'CPUQuota'), '100%')

    def test_template_contract_and_immutable_identity(self):
        self.assertEqual(ctl.bindings(self.rendered(), 'test')['image'], 'sha256:' + '1' * 64)
        for replacement in ('latest', 'sha256:123', 'repo:version'):
            with self.assertRaisesRegex(ValueError, 'immutable|config ID'):
                ctl.bindings(self.rendered().replace('sha256:' + '1' * 64, replacement), 'test')

    def test_rejects_overlapping_or_foreign_recovery_mount(self):
        for volume in ('/tmp:/run:rw', '/tmp:/run/netns/unrelated:rw', '/tmp:/sys/fs/bpf:rw'):
            with self.assertRaises(ValueError):
                ctl.bindings(self.rendered() + '\n[Container]\nVolume=' + volume, 'test')
        with self.assertRaises(ValueError):
            ctl.bindings(self.rendered().replace(str(self.node.recovery), '/var/lib/foreign'), 'test')
        with self.assertRaisesRegex(ValueError, 'recovery volume'):
            ctl.bindings(self.rendered().replace('rw,rslave', 'rw,rshared'), 'test')

    def test_names_and_paths_cannot_escape_owner(self):
        for name in ('../x', '', 'x.service', '-x', 'x y', 'x' * 41):
            with self.assertRaises(ValueError):
                ctl.Instance(name)
        for value in ('/tmp/../etc', '/tmp/%n', '/tmp/$HOME', 'relative', '/tmp/a:b'):
            with self.assertRaises(ValueError):
                ctl.safe_path(value)
        link = self.root / 'link'
        link.symlink_to(self.root)
        with self.assertRaises(ValueError):
            ctl.safe_path(str(link / 'child'))

    def test_foreign_container_never_adopted(self):
        with patch.object(ctl, 'inspect', return_value={'Config': {'Labels': {ctl.LABEL: 'foreign'}}}):
            with self.assertRaisesRegex(ValueError, 'foreign container'):
                self.node.owned_container({'owner': 'ours'})

    def test_recovery_uses_pinned_old_tool(self):
        applied = {'schema': 1, 'state': 'running', 'owner': 'ours', 'tool': '/opt/old/bin/node-infra-ctl'}
        ctl.save(self.node.applied, applied)
        with patch.object(self.node, 'installed', return_value={'owner': 'ours'}), \
             patch.object(ctl.os, 'execv', side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):
                self.node.recover()
            execute.assert_called_once_with(applied['tool'], [applied['tool'], 'recover', '--name', 'test'])

    def test_live_consumer_failure_retains_every_recovery_input(self):
        applied = {'schema': 1, 'state': 'running', 'owner': 'ours', 'tool': str(Path(ctl.__file__).resolve())}
        ctl.save(self.node.applied, applied)
        self.node.recovery.mkdir()
        ctl.save(self.node.connector, {'state': 'running'})
        with patch.object(self.node, 'installed', return_value={'owner': 'ours'}), \
             patch.object(self.node, 'owned_container', return_value=None), \
             patch.object(self.node, 'consumers_gone', side_effect=ValueError('still populated')), \
             patch.object(ctl, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'populated'):
                self.node.recover()
            command.assert_not_called()
        self.assertEqual(ctl.json_file(self.node.connector)['state'], 'running')
        self.assertIn('populated', ctl.json_file(self.node.applied)['error'])
        self.assertEqual(ctl.json_file(self.node.applied)['state'], 'running')

    def test_reboot_does_not_apply_old_record_to_new_namespace(self):
        applied = {'schema': 1, 'state': 'running', 'owner': 'ours', 'generation': 'old',
                   'boot_id': 'old-boot', 'tool': str(Path(ctl.__file__).resolve())}
        ctl.save(self.node.applied, applied)
        self.node.recovery.mkdir()
        ctl.save(self.node.connector, {'state': 'running', 'main_netns_inode': 123})
        with patch.object(self.node, 'installed', return_value={'owner': 'ours'}), \
             patch.object(self.node, 'owned_container', return_value=None), \
             patch.object(self.node, 'consumers_gone'), patch.object(ctl, 'run') as command:
            self.node.recover()
            command.assert_not_called()
        self.assertEqual(ctl.json_file(self.node.applied)['state'], 'cleaned')
        self.assertTrue((self.node.directory / 'history/old/connector.json').is_file())

    def test_reboot_with_retained_handles_refuses_cleanup(self):
        applied = {'schema': 1, 'state': 'running', 'owner': 'ours', 'generation': 'old',
                   'boot_id': 'old-boot', 'tool': str(Path(ctl.__file__).resolve())}
        ctl.save(self.node.applied, applied)
        self.node.run.mkdir(parents=True)
        with patch.object(self.node, 'installed', return_value={'owner': 'ours'}), \
             patch.object(self.node, 'owned_container', return_value=None), patch.object(self.node, 'consumers_gone'):
            with self.assertRaisesRegex(ValueError, 'after reboot'):
                self.node.recover()
        self.assertEqual(ctl.json_file(self.node.applied)['state'], 'running')

    def test_retry_after_unmount_or_final_owner_removal(self):
        for owner_exists in (True, False):
            with self.subTest(owner_exists=owner_exists):
                applied = {'schema': 1, 'state': 'releasing', 'owner': 'ours', 'generation': 'old',
                           'boot_id': ctl.boot_id(), 'tool': str(Path(ctl.__file__).resolve()),
                           'mounts': {str(self.node.run / 'bpf'): {'id': 'old'}}}
                ctl.save(self.node.applied, applied)
                if owner_exists:
                    ctl.save(self.node.run / 'owner.json', {'owner': 'ours', 'generation': 'old'})
                    (self.node.run / 'bpf').mkdir()
                with patch.object(self.node, 'installed', return_value={'owner': 'ours'}), \
                     patch.object(self.node, 'owned_container', return_value=None), \
                     patch.object(self.node, 'consumers_gone'), patch.object(ctl, 'mount_at', return_value={}), \
                     patch.object(ctl, 'run') as command:
                    self.node.recover()
                    command.assert_not_called()
                self.assertEqual(ctl.json_file(self.node.applied)['state'], 'cleaned')
                self.assertFalse(self.node.run.exists())

    def test_mount_intent_survives_exit_before_identity_update(self):
        applied = {'mounts': {}}
        path = self.node.run / 'primary'
        with patch.object(ctl, 'run', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.node.create_mount(applied, path, 'nsfs', 123, '--bind', '/original', path)
        saved = ctl.json_file(self.node.applied)
        self.assertEqual(saved['mount_intents'][str(path)], {'type': 'nsfs', 'inode': 123})
        self.assertEqual(saved['mounts'], {})

    def test_version_payload_cannot_be_replaced(self):
        package = self.root / 'package'
        (package / 'bin').mkdir(parents=True)
        (package / 'templates').mkdir()
        (package / 'bin/node-infra-ctl').write_text('original')
        (package / 'templates/node-infra.container').write_text('unit')
        (package / 'release.json').write_text('{}')
        with patch.object(ctl, 'private_directory', side_effect=lambda p: p.mkdir(parents=True, exist_ok=True)):
            destination = ctl.pin_release(package, {'version': 'v1'})
            self.assertEqual(ctl.pin_release(package, {'version': 'v1'}), destination)
            (package / 'bin/node-infra-ctl').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'different bytes'):
                ctl.pin_release(package, {'version': 'v1'})
        self.assertEqual((destination / 'bin/node-infra-ctl').read_text(), 'original')


if __name__ == '__main__':
    unittest.main()
