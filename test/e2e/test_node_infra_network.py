"""Regress task-owned veth identities without claiming a privileged Docker run."""
import importlib.util
import ipaddress
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'node_infra_network_fixture', Path(__file__).parent / 'platform/lib/node_infra.py')
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)


class FixtureNetworkTests(unittest.TestCase):
    def node(self, *, host=True, transit=True, gateway=False, index=0):
        node = subject.Node.__new__(subject.Node)
        node.fixture = SimpleNamespace(subnet=int(ipaddress.IPv4Address('198.18.10.0')))
        node.name, node.index = 'niabc123' + str(index), index
        node.netns = 'node-infra-' + node.name
        node.host, node.with_transit = host, transit
        node.gateway = node.netns + '-gw' if gateway else None
        node.network = ['ip'] if host else ['ip', '-n', node.netns]
        node.transit_name = node.name + 't' if host else 'uplink0'
        node.peers = []
        node.original_transit = None
        return node

    def network(self, node, *, corrupt=False, occupied=False):
        calls, links = [], {}
        def command(*args, **kwargs):
            calls.append(args)
            if args[:4] == ('ip', 'link', 'show', node.name + '1h') and occupied:
                return SimpleNamespace(returncode=0, stdout='')
            if args[:3] == ('ip', 'link', 'show'):
                return SimpleNamespace(returncode=1, stdout='')
            if args[:3] == ('ip', 'link', 'add'):
                # Both addresses must be present at creation, not assigned by a
                # later link-set after the host's initial udev event.
                self.assertEqual(args[4], 'address')
                self.assertEqual(args[6:10], ('type', 'veth', 'peer', 'name'))
                self.assertEqual(args[11], 'address')
                self.assertEqual(len(args), 13)
                self.assertNotEqual(args[5], args[12])
                links[args[3]], links[args[10]] = args[5], args[12]
            if 'name' in args and 'set' in args:
                i = args.index('set')
                links[args[args.index('name') + 1]] = links.pop(args[i + 1])
            if '-j' in args:
                mac = '02:ff:ff:ff:ff:ff' if corrupt else links[args[-1]]
                return SimpleNamespace(returncode=0, stdout=json.dumps([{'address': mac}]))
            self.assertNotIn('address', args[args.index('set') + 1:] if 'set' in args else ())
            return SimpleNamespace(returncode=0, stdout='')
        with patch.object(subject, 'command', side_effect=command):
            node._network()
        return calls, links

    def test_mac_is_stable_local_unicast_and_distinct_for_all_fixture_ends(self):
        names = [f'ni{key}{index}{plane}{end}' for key in ('abc123', 'def456')
                 for index in range(3) for plane in (0, 1) for end in ('h', 'p')]
        addresses = [subject.fixture_mac(name) for name in names]
        self.assertEqual(len(set(addresses)), len(names))
        for name, address in zip(names, addresses):
            self.assertEqual(subject.fixture_mac(name), address)
            octets = bytes.fromhex(address.replace(':', ''))
            self.assertEqual(len(octets), 6)
            self.assertEqual(octets[0] & 3, 2)

    def test_identity_is_explicit_at_creation_across_topologies(self):
        for host in (False, True):
            for transit in (False, True):
                for gateway in ((False, True) if transit else (False,)):
                    with self.subTest(host=host, transit=transit, gateway=gateway):
                        node = self.node(host=host, transit=transit, gateway=gateway)
                        calls, links = self.network(node)
                        planes = ([0] if not host else []) + ([1] if transit else [])
                        self.assertEqual(node.peers, [node.name + str(p) + 'h' for p in planes])
                        creates = [c for c in calls if c[:3] == ('ip', 'link', 'add')]
                        self.assertEqual(len(creates), len(planes))
                        for call, plane in zip(creates, planes):
                            self.assertEqual(call[5], subject.fixture_mac(node.name + str(plane) + 'h'))
                            self.assertEqual(call[12], subject.fixture_mac(node.name + str(plane) + 'p'))
                        if transit:
                            expected = subject.fixture_mac(node.name + '1p')
                            self.assertEqual(node.original_transit, expected)
                            self.assertEqual(links[node.transit_name], expected)
                        else:
                            self.assertIsNone(node.original_transit)
                        if not host:
                            self.assertIn(('ip', 'link', 'set', node.name + '0p', 'netns', node.netns), calls)
                        if gateway:
                            self.assertIn(('ip', 'link', 'set', node.name + '1h', 'netns', node.gateway), calls)
                            self.assertIn(tuple(node.network + ['link', 'set', node.transit_name, 'mtu', '1600']), calls)

    def test_changed_mac_before_handoff_is_not_adopted_as_expected(self):
        node = self.node()
        with self.assertRaisesRegex(RuntimeError, 'fixture transit MAC changed before handoff'):
            self.network(node, corrupt=True)
        self.assertEqual(node.original_transit, subject.fixture_mac(node.name + '1p'))

    def test_existing_interface_is_never_modified(self):
        node = self.node()
        with self.assertRaisesRegex(RuntimeError, 'interface already exists'):
            self.network(node, occupied=True)
        self.assertEqual(node.peers, [])

    def returned(self, node, *, mac=None, flags=(), running=False, pending=False):
        response = SimpleNamespace(stdout=json.dumps([{
            'address': mac or node.original_transit, 'flags': list(flags)}]))
        with patch.object(subject, 'command', return_value=response), patch.object(
                node, 'status', return_value={'container': {'running': running},
                                              'recovery': {'pending': pending}}):
            node.assert_returned()

    def test_return_requires_exact_original_mac_down_and_completed_recovery(self):
        node = self.node()
        self.network(node)
        self.returned(node)
        for change, message in (({'mac': '02:ff:ff:ff:ff:ff'}, 'original transit did not return'),
                                ({'flags': ['UP']}, 'returned transit must be DOWN'),
                                ({'running': True}, 'old instance still runs or needs recovery'),
                                ({'pending': True}, 'old instance still runs or needs recovery')):
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, message):
                self.returned(node, **change)


if __name__ == '__main__':
    unittest.main()
