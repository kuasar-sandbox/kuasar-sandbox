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

    def test_host_and_explicit_namespace_bindings(self):
        host = ctl.bindings(self.rendered(), 'test')
        self.assertEqual((host['network'], host['netns']), ('host', '/proc/self/ns/net'))
        isolated = ctl.bindings(self.rendered().replace('Network=host', 'Network=ns:/run/netns/custom'), 'test')
        self.assertEqual(isolated['netns'], '/run/netns/custom')
        for network in ('none', 'bridge', 'ns:relative', 'ns:/tmp/../net', 'ns:/proc/self/ns/net',
                        'host\nNetwork=host'):
            with self.subTest(network=network), self.assertRaises(ValueError):
                ctl.bindings(self.rendered().replace('Network=host', 'Network=' + network), 'test')

    def check_fixture(self, network, transit, *, up=False, missing=False, wrong_namespace=False):
        selected = {'network': network, 'netns': '/proc/self/ns/net', 'image': 'image', 'volumes': [], 'values': {}}
        descriptor = {'transit': {'name': 'uplink' if transit else ''},
                      'main_netns_inode': Path('/proc/self/ns/net').stat().st_ino + int(wrong_namespace)}
        links = [{'ifname': 'lo', 'flags': ['UP']}, {'ifname': 'arbitrary-ingress', 'flags': ['UP']}]
        if transit and not missing:
            links.append({'ifname': 'uplink', 'flags': ['UP'] if up else [], 'address': '02:00:00:00:00:01'})

        def command(*args, **kwargs):
            if args[:2] == ('podman', 'version'):
                output = '4.9.3'
            elif args[0] == 'nsenter':
                output = json.dumps(links)
            elif args[-1] == 'preflight':
                self.assertIn('--network=' + network, args)
                output = json.dumps(descriptor)
            elif args[-1] == 'budget':
                output = '{}'
            elif args[-1] == 'validate':
                output = ''
            else:
                self.fail('unexpected command: ' + repr(args))
            return types.SimpleNamespace(stdout=output, returncode=0)

        with patch.object(self.node, 'installed', return_value={'tool': '/release/bin/node-infra-ctl', 'image': 'image'}), \
             patch.object(self.node, 'source', return_value=selected), \
             patch.object(self.node, 'owned_container', return_value=None), \
             patch.object(ctl, 'json_file', return_value={}), \
             patch.object(ctl, 'image_identity', return_value='image'), \
             patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'is_file', return_value=True), \
             patch.object(Path, 'is_char_device', return_value=True), \
             patch.object(ctl, 'mount_at', return_value={'type': 'nsfs'}), \
             patch.object(ctl, 'run', side_effect=command):
            return self.node.check()

    def test_check_four_combinations_without_fixed_ingress(self):
        for network in ('host', 'ns:/run/netns/custom'):
            for transit in (False, True):
                with self.subTest(network=network, transit=transit):
                    selected, descriptor, device = self.check_fixture(network, transit)
                    self.assertEqual(selected['primary_inode'], descriptor['main_netns_inode'])
                    self.assertEqual(bool(device), transit)

    def test_check_rejects_missing_up_and_wrong_namespace(self):
        for network in ('host', 'ns:/run/netns/custom'):
            for options in ({'up': True}, {'missing': True}, {'wrong_namespace': True}):
                with self.subTest(network=network, options=options), self.assertRaises(ValueError):
                    self.check_fixture(network, True, **options)

    def test_status_keeps_shape_when_pid_namespace_disappears(self):
        primary = self.node.run / 'primary'
        primary.parent.mkdir(parents=True)
        primary.touch()
        ctl.save(self.node.applied, {'state': 'running', 'boot_id': ctl.boot_id(),
                                    'primary_inode': primary.stat().st_ino})
        # /proc/0 cannot exist: model inspect racing with the container's exit.
        container = {'State': {'Running': True, 'Pid': 0}, 'Image': 'image'}
        with patch.object(self.node, 'installed', return_value={'image': 'image'}), \
             patch.object(self.node, 'owned_container', return_value=container), \
             patch.object(self.node, 'service_state', return_value='deactivating'), \
             patch.object(ctl, 'run', return_value=types.SimpleNamespace(returncode=1, stdout='', stderr='exited')):
            result = self.node.status()
        self.assertFalse(result['ready'])
        self.assertTrue(result['container']['running'])
        self.assertIn('pending', result['recovery'])
        self.assertEqual(result['checks'][0]['status'], 'fail')

    def test_recover_optional_transit_and_legacy_records(self):
        for network in ('host', 'ns:/run/netns/custom', None):
            for transit in (None, {'ifname': 'uplink', 'address': '02:00:00:00:00:01'}):
                with self.subTest(network=network, transit=transit):
                    primary = self.node.run / 'primary'
                    ctl.save(self.node.run / 'owner.json', {'owner': 'ours', 'generation': 'g1'})
                    primary.touch()
                    mounts = {str(primary): {'type': 'nsfs', 'id': '1'}}
                    applied = {'schema': 1, 'state': 'prepared', 'owner': 'ours', 'generation': 'g1',
                               'boot_id': ctl.boot_id(), 'tool': str(Path(ctl.__file__).resolve()),
                               'mounts': dict(mounts), 'primary_inode': primary.stat().st_ino, 'transit': transit}
                    if network is not None:
                        applied['network'] = network
                    ctl.save(self.node.applied, applied)
                    calls = []

                    def command(*args, **kwargs):
                        calls.append(args)
                        if args[0] == 'umount':
                            mounts.pop(str(args[1]))
                        else:
                            self.assertEqual(args[0], 'nsenter')
                            self.assertIsNotNone(transit)
                            self.assertEqual(args[-1], 'uplink')
                        return types.SimpleNamespace(stdout=json.dumps([dict(transit or {}, flags=[])]), returncode=0)

                    with patch.object(self.node, 'installed', return_value={'owner': 'ours'}), \
                         patch.object(self.node, 'owned_container', return_value=None), \
                         patch.object(self.node, 'consumers_gone'), \
                         patch.object(self.node, 'source', side_effect=AssertionError('must use old inputs')), \
                         patch.object(ctl, 'mount_at', side_effect=lambda path: mounts.get(str(path), {})), \
                         patch.object(ctl, 'run', side_effect=command):
                        self.node.recover()
                        self.node.recover()
                    self.assertEqual(ctl.json_file(self.node.applied)['state'], 'cleaned')
                    self.assertEqual(sum(call[0] == 'nsenter' for call in calls), int(transit is not None))
                    self.assertFalse(self.node.run.exists())

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

    def test_reboot_archive_retries_each_interruption(self):
        for boundary in ('directory', 'connector', 'current-record'):
            with self.subTest(boundary=boundary):
                node = ctl.Instance(boundary)
                node.directory.mkdir()
                applied = {'schema': 1, 'state': 'running', 'owner': 'ours', 'generation': 'old',
                           'boot_id': 'old-boot', 'tool': str(Path(ctl.__file__).resolve())}
                ctl.save(node.applied, applied)
                ctl.save(node.connector, {'state': 'running', 'main_netns_inode': 123})
                history = node.directory / 'history/old'
                rename, save = Path.rename, ctl.save

                def interrupted_rename(source, target):
                    result = rename(source, target)
                    if (boundary == 'directory' and target == history or
                            boundary == 'connector' and source == node.connector):
                        raise SystemExit('injected death after rename')
                    return result

                def interrupted_save(path, data):
                    if boundary == 'current-record' and path == node.applied and data['state'] == 'cleaned':
                        raise SystemExit('injected death before final save')
                    return save(path, data)

                with patch.object(node, 'installed', return_value={'owner': 'ours'}), \
                     patch.object(node, 'owned_container', return_value=None), patch.object(node, 'consumers_gone'):
                    with patch.object(Path, 'rename', interrupted_rename), patch.object(ctl, 'save', interrupted_save):
                        with self.assertRaises(SystemExit):
                            node.recover()
                    self.assertEqual(ctl.json_file(node.applied)['state'], 'running')
                    node.recover()
                    node.recover()
                self.assertEqual(ctl.json_file(node.applied)['state'], 'cleaned')
                self.assertEqual(ctl.json_file(history / 'connector.json')['main_netns_inode'], 123)
                self.assertFalse(node.connector.exists())

    def test_foreign_reboot_archive_is_never_overwritten(self):
        applied = {'state': 'running', 'owner': 'ours', 'generation': 'old', 'boot_id': 'old-boot'}
        history = self.node.directory / 'history/old'
        history.mkdir(parents=True)
        for foreign in (None, dict(applied, owner='other', error='')):
            if foreign:
                ctl.save(history / 'applied.json', foreign)
            with self.assertRaises(ValueError):
                self.node.archive_boot(applied)
        self.assertEqual(ctl.json_file(history / 'applied.json')['owner'], 'other')
        linked = self.node.directory / 'history/linked'
        linked.symlink_to(self.root / 'missing')
        with self.assertRaisesRegex(ValueError, 'linked reboot'):
            self.node.archive_boot(dict(applied, generation='linked'))
        self.assertTrue(linked.is_symlink())

    def test_helper_intent_survives_create_start_and_remove_death(self):
        for boundary in ('create', 'start', 'remove'):
            with self.subTest(boundary=boundary):
                applied = {'owner': 'ours', 'generation': 'g1', 'image': 'sha256:' + '1' * 64}
                containers, calls = {}, []
                self.node.lock_fd = 99

                def command(*args, **kwargs):
                    calls.append(args[1])
                    saved = ctl.json_file(self.node.applied)['helper']
                    if args[1] == 'create':
                        self.assertEqual(kwargs['pass_fds'], (99,))
                        self.assertNotIn('id', saved)
                        containers[saved['name']] = {'Id': 'immutable-id', 'Image': applied['image'], 'Config': {'Labels': {
                            ctl.LABEL: 'ours', ctl.LABEL + '.generation': 'g1', ctl.LABEL + '.operation': 'prepare'}}}
                    if args[1] == 'start':
                        self.assertEqual(args[-1], 'immutable-id')
                        self.assertEqual(saved['id'], 'immutable-id')
                    if args[1] == 'rm':
                        self.assertEqual(args[-1], 'immutable-id')
                        containers.clear()
                    if args[1] == {'remove': 'rm'}.get(boundary, boundary):
                        raise SystemExit('injected helper hook death')
                    return types.SimpleNamespace(stdout='', returncode=0)

                with patch.object(ctl, 'inspect', side_effect=lambda kind, name, runtime='podman': containers.get(name)), \
                     patch.object(ctl, 'run', side_effect=command):
                    with self.assertRaises(SystemExit):
                        self.node.run_helper(applied, 'prepare', ['--network=none'])
                    applied = ctl.json_file(self.node.applied)
                    self.assertIn('helper', applied)
                    boundary = 'no-more-deaths'
                    self.node.reap_helper(applied)
                self.assertFalse(containers)
                self.assertNotIn('helper', ctl.json_file(self.node.applied))
                self.assertIn('rm', calls)

    def test_foreign_helper_blocks_recovery_before_consumers_or_mounts(self):
        applied = {'state': 'preparing', 'owner': 'ours', 'generation': 'g1', 'image': 'sha256:' + '1' * 64,
                   'tool': str(Path(ctl.__file__).resolve()),
                   'helper': {'name': self.node.container + '-g1-prepare', 'operation': 'prepare'}}
        ctl.save(self.node.applied, applied)
        with patch.object(self.node, 'installed', return_value={'owner': 'ours'}), \
             patch.object(self.node, 'owned_container', return_value=None), \
             patch.object(ctl, 'inspect', return_value={'Config': {'Labels': {ctl.LABEL: 'other'}}}), \
             patch.object(self.node, 'consumers_gone') as consumers, patch.object(ctl, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'foreign helper'):
                self.node.recover()
            consumers.assert_not_called()
            command.assert_not_called()
        self.assertIn('helper', ctl.json_file(self.node.applied))

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

    def test_visible_mount_distinguishes_hidden_shared_peer_from_stack(self):
        path = self.root / 'namespace'
        path.touch()
        # The peer at ID 13 is hidden below the self-bind at ID 11. ID 12
        # is the mount reached by path lookup, independent of table order.
        table = (f'10 1 0:1 / {self.root} rw shared:1 - tmpfs tmpfs rw\n'
                 f'11 10 0:1 / {self.root} rw shared:1 - tmpfs tmpfs rw\n'
                 f'12 11 0:4 net:[100] {path} rw shared:2 - nsfs nsfs rw\n'
                 f'13 10 0:4 net:[100] {path} rw shared:2 - nsfs nsfs rw\n')

        def inspect(table, visible):
            def read(file):
                return table if file == Path('/proc/self/mountinfo') else f'mnt_id:\t{visible}\n'
            with patch.object(Path, 'read_text', read):
                return ctl.mount_at(path)

        for rows in (table, '\n'.join(reversed(table.splitlines()))):
            self.assertEqual(inspect(rows, '12')['id'], '12')
        # A real overmount has the previous mount as an ancestor, including
        # when both refer to the same nsfs inode. It must still fail closed.
        with self.assertRaisesRegex(ValueError, 'stacked mounts'):
            inspect(table + f'14 12 0:4 net:[100] {path} rw - nsfs nsfs rw\n', '14')
        # A stale entry hidden by a parent is not a mount at the current path.
        self.assertEqual(inspect(table, '11'), {})
        with self.assertRaisesRegex(ValueError, 'disappeared'):
            inspect(table, '99')
        path.unlink()
        self.assertEqual(ctl.mount_at(path), {})

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

    def test_docker_helper_enters_saved_primary_without_podman_network_syntax(self):
        node = ctl.DockerInstance('test')
        primary = node.run / 'primary'
        arguments, executable = node.helper_arguments(['--network=ns:' + str(primary),
                                                       '--volume', '/pins:/run/netns:rw,rshared'])
        self.assertIn('--network=none', arguments)
        self.assertIn(str(primary) + ':/run/node-infra-primary:ro', arguments)
        self.assertFalse(any(x.startswith('--network=ns:') for x in arguments))
        self.assertEqual(executable, ['/usr/bin/nsenter', '--net=/run/node-infra-primary', ctl.RUNTIME])

    def test_docker_lost_create_response_retains_intent(self):
        node = ctl.DockerInstance('test')
        applied = {'generation': 'g1', 'helper': {'name': node.container + '-g1-prepare', 'operation': 'prepare'}}
        ctl.save(node.applied, applied)
        with patch.object(node, 'inspect', return_value=None), patch.object(ctl, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'outcome is unknown'):
                node.reap_helper(applied)
        command.assert_not_called()
        self.assertEqual(ctl.json_file(node.applied), applied)

    def test_docker_empty_placeholders_have_identity_and_reject_foreign_contents(self):
        node = ctl.DockerInstance('test')
        (node.run / 'netns').mkdir(parents=True)
        (node.run / 'bpf').mkdir()
        with patch.object(ctl, 'private_directory'), patch.object(ctl, 'mount_at', return_value={}):
            initial = node.initial_handles()
            self.assertEqual(initial['netns'], [(node.run / 'netns').stat().st_dev,
                                               (node.run / 'netns').stat().st_ino])
            (node.run / 'netns/foreign').touch()
            with self.assertRaisesRegex(ValueError, 'unowned Docker handle placeholder'):
                node.initial_handles()

    def test_docker_context_override_cannot_hide_remote_daemon(self):
        node = ctl.DockerInstance('test')
        with patch.dict(ctl.os.environ, {'DOCKER_CONTEXT': 'remote', 'DOCKER_HOST': 'unix:///run/docker.sock'}), \
             patch.object(ctl, 'run', return_value=types.SimpleNamespace(stdout=json.dumps([
                 {'Endpoints': {'docker': {'Host': 'ssh://remote'}}}]))) as command:
            with self.assertRaisesRegex(ValueError, 'local Unix socket'):
                node.daemon()
        command.assert_called_once_with('docker', 'context', 'inspect')


if __name__ == '__main__':
    unittest.main()
