"""Exercise the delivered native Egress service through the node deployment."""
import json
from pathlib import Path

from node_infra import command, require, wait_for, write_json
from node_infra_egress import ExternalEgress


def native_case(fixture, node_type):
    def write_policy(path, value):
        path.write_text('# Egress YAML configuration\n'+''.join(key+': '+json.dumps(item)+'\n' for key,item in value.items()))
    provider = ExternalEgress(fixture, native=True)
    node = None
    clean = False
    try:
        node = node_type(fixture, 0, bridge=True)
        node.netns_inputs['egress'] = Path('/run/netns') / provider.name
        config = node.config / 'connector-egress'
        config.mkdir()
        node.extra_mounts.append((config, '/etc/connector-egress'))
        area = {'vni': 100, 'pools': [{'name': 'public', 'interface': 'out0', 'next_hop': '192.0.2.1', 'addresses': ['192.0.2.2'],
                                    'max_connections': 1024}],
                'acl': [{'destination': '192.0.2.1/32', 'protocol': 'any', 'action': 'allow'}],
                'routes': [{'destination': '0.0.0.0/0', 'pool': 'public'}]}
        write_policy(config / 'area.yaml', area)
        write_policy(config / 'gateway.yaml', {
            'name': 'local', 'runtime_dir': '/run/connector-egress/local',
            'interfaces': [{'name': 'egress-rx', 'address': '198.18.0.1/24', 'mtu': 9000},
                           {'name': 'out0', 'address': '192.0.2.2/30', 'mtu': 1500}],
            'attachments': [{'name': 'sw0', 'interface': 'egress-rx', 'peer': ['198.18.0.2'],
                             'locator': 'port', 'inner_mtu': 1500}],
            'area_files': ['area.yaml']})
        write_json(config / 'link.json', {
            'name': 'local', 'owner_netns': '', 'gateway_netns': 'egress',
            'switch_netns': 'sw0-switch', 'switch': 'sw0', 'transit': 'egress-tx',
            'gateway': 'egress-rx', 'mtu': 9000,
            'state_dir': '/run/node-infra/recovery/egress-link',
            'gateway_runtime_dir': '/run/connector-egress/local'})
        write_json(node.config / 'connector-vswitch/conf.d/95-native-egress.yaml', {
            'transit_dev': 'egress-tx', 'transit_dev_addr': '198.18.0.2/24:198.18.0.1',
            'transit_dev_mtu': 'auto', 'generation_bits': 4,
            'geneve_encap_eth': True, 'geneve_locator': 'port', 'geneve_port_base': 50000})
        # Use the existing sandbox's Geneve metadata contract, without a new
        # gateway lifecycle interface. The gateway observes its first packet.
        node.gateway, node.transit_host = 'egress', '198.18.0.1'
        node.start()
        guest = node.create()
        provider.traffic(node, guest)
        runtime = '/run/connector-egress/local/status.json'
        def status():
            return json.loads(node.execute('cat', runtime).stdout)
        def links():
            return json.loads(node.execute('/opt/node-infra/bin/connector-ctl', 'egress', 'link', 'check',
                                           '--config', '/etc/connector-egress/link.json').stdout)
        pair = json.loads(command('ip', '-n', provider.name, '-j', 'link', 'show', 'egress-rx').stdout)[0]
        initial = status()
        require(initial['configuration_generation'] > 0 and links()['pair_valid'], 'native gateway was not ready')
        command('ip', '-n', provider.name, 'link', 'set', 'out0', 'mtu', '1490')
        wait_for('gateway disables invalid Pool interface', lambda: status()['disabled_interfaces'].get('out0'))
        # AF_PACKET admin-down also brings its Linux device down. Restore
        # the provider-owned link explicitly before controlled service recovery.
        command('ip', '-n', provider.name, 'link', 'set', 'out0', 'mtu', '1500', 'up')
        require(not node.status()['ready'], 'restored Linux link hid a disabled native interface')
        node.execute('systemctl', 'restart', 'connector-egress.service')
        wait_for('controlled gateway recovery', lambda: node.status()['ready'])
        provider.traffic(node, guest)
        initial = status()
        area['pools'][0]['max_connections'] = 2048
        write_policy(config / 'area.yaml', area)
        node.execute('systemctl', 'reload', 'connector-egress.service')
        wait_for('Area-file SIGHUP reload', lambda: status()['configuration_generation'] > initial['configuration_generation'])
        require(status()['pid'] == initial['pid'], 'reload replaced the gateway process')
        provider.traffic(node, guest)
        node.execute('systemctl', 'restart', 'connector-egress.service')
        wait_for('native gateway restart readiness', lambda: node.status()['ready'])
        require(status()['pid'] != initial['pid'], 'gateway restart did not replace its process')
        provider.traffic(node, guest)
        actual = json.loads(command('ip', '-n', provider.name, '-j', 'link', 'show', 'egress-rx').stdout)[0]
        require(actual['ifindex'] == pair['ifindex'] and actual['address'] == pair['address'],
                'gateway process restart replaced the shared pair')
        node.api('DELETE', '/sandboxes/' + guest['sandboxID'], expected=204)
        new_guest = node.create()
        provider.traffic(node, new_guest)
        write_json(fixture.output / 'native-egress.json', {
            'traffic': 'TCP/UDP SNAT and return passed', 'reload': 'Area file without process replacement',
            'restart': 'shared link retained; new traffic entered automatically',
            'sandbox_reuse': 'automatic entry without gateway registration', 'status': status(), 'link': links()})
        node.stop()
        provider.unchanged('native-stop')
        node.remove()
        clean = True
    finally:
        if node is not None:
            node.remove()
        if clean:
            provider.close()
