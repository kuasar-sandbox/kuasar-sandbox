"""Provider-owned namespace fixture and controlled TCP/UDP SNAT targets."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from node_infra import command, require, wait_for, write_json


def target(ready):
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(('192.0.2.1', 18081))

    def serve_udp():
        while True:
            data, peer = udp.recvfrom(4096)
            udp.sendto(json.dumps({'source': peer[0], 'marker': data.decode()}).encode(), peer)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({'source': self.client_address[0], 'marker': self.path[1:]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    http = HTTPServer(('192.0.2.1', 18080), Handler)
    threading.Thread(target=serve_udp, daemon=True).start()
    Path(ready).write_text(str(os.getpid()))
    http.serve_forever()


class ExternalEgress:
    def __init__(self, fixture):
        self.fixture = fixture
        self.name, self.target = fixture.key + '-egress', fixture.key + '-target'
        self.management = fixture.key + '-mgmt'
        self.process = None
        self.created = []
        try:
            for name in (self.name, self.target, self.management):
                require(not (Path('/run/netns') / name).exists(), 'external fixture already exists')
                command('ip', 'netns', 'add', name)
                self.created.append(name)
                command('ip', '-n', name, 'link', 'set', 'lo', 'up')
            command('ip', '-n', self.management, 'link', 'add', 'provider0', 'type', 'dummy')
            command('ip', '-n', self.management, 'address', 'add', '198.51.100.1/32', 'dev', 'provider0')
            command('ip', '-n', self.management, 'link', 'set', 'provider0', 'up')
            command('ip', '-n', self.name, 'link', 'add', 'out0', 'type', 'veth',
                    'peer', 'name', 'target0', 'netns', self.target)
            for name, device, address in ((self.name, 'out0', '192.0.2.2/30'),
                                          (self.target, 'target0', '192.0.2.1/30')):
                command('ip', '-n', name, 'address', 'add', address, 'dev', device)
                command('ip', '-n', name, 'link', 'set', device, 'up')
            command('ip', '-n', self.name, 'route', 'add', 'default', 'via', '192.0.2.1')
            command('ip', 'netns', 'exec', self.name, 'sysctl', '-w', 'net.ipv4.ip_forward=1')
            rules = fixture.work / 'provider-egress.nft'
            rules.write_text('table ip provider {\n chain postrouting {\n'
                             '  type nat hook postrouting priority srcnat;\n'
                             '  oifname "out0" snat to 192.0.2.2;\n }\n}\n')
            command('ip', 'netns', 'exec', self.name, 'nft', '-f', rules)
            ready = fixture.work / 'egress-target.ready'
            self.process = subprocess.Popen(['ip', 'netns', 'exec', self.target, sys.executable,
                                             str(Path(__file__)), str(ready)])
            wait_for('controlled egress target', ready.exists)
            wait_for('provider address duplicate detection', lambda: all(
                not address.get('tentative')
                for namespace in (self.name, self.management)
                for link in json.loads(command('ip', '-n', namespace, '-j', 'address', 'show').stdout)
                for address in link.get('addr_info', [])))
            self.before = self.snapshot()
            write_json(fixture.output / 'external-before.json', self.before)
        except Exception:
            self.close()
            raise

    def snapshot(self):
        return {name: self.snapshot_plane(name) for name in (self.name, self.management)}

    def snapshot_plane(self, namespace):
        result = {'inode': (Path('/run/netns') / namespace).stat().st_ino}
        for label, args in {'links': ['address', 'show'], 'routes': ['-4', 'route', 'show', 'table', 'all']}.items():
            rows = json.loads(command('ip', '-n', namespace, '-j', *args).stdout)
            if label == 'links':
                rows = [{k: row.get(k) for k in ('ifindex', 'ifname', 'address', 'mtu', 'flags', 'addr_info')}
                        for row in rows]
            result[label] = rows
        result['rules'] = command('ip', 'netns', 'exec', namespace, 'nft', '-s', 'list', 'ruleset').stdout
        return result

    def configure(self, node):
        node.netns_inputs['egress'] = Path('/run/netns') / self.name
        node.netns_inputs['mgmt'] = Path('/run/netns') / self.management
        write_json(node.config / 'connector-vswitch/conf.d/95-egress.yaml',
                   {'mgmt_extracts': ['mgmt:mgmt0:169.254.169.254/32', 'egress:egress0:0.0.0.0/0']})

    def traffic(self, node, guest):
        metadata = json.loads(node.guest(guest, '/mmds'))
        require(metadata.get('instanceID') == guest['sandboxID'], 'external MGMT MMDS failed')
        tcp = json.loads(node.guest(guest, '/egress?' + urllib.parse.urlencode(
            {'url': 'http://192.0.2.1:18080/' + node.name})))
        udp = json.loads(node.guest(guest, '/udp?' + urllib.parse.urlencode(
            {'address': '192.0.2.1:18081', 'marker': node.name})))
        require(tcp == udp == {'source': '192.0.2.2', 'marker': node.name}, 'TCP/UDP SNAT or return path failed')
        write_json(self.fixture.output / node.name / 'egress-traffic.json', {'tcp': tcp, 'udp': udp})

    def unchanged(self, phase):
        actual = self.snapshot()
        write_json(self.fixture.output / ('external-' + phase + '.json'), actual)
        require(actual == self.before, 'external namespace, exit, route or provider rules changed: ' + phase)

    def input_changes(self, node, guest):
        """A replaced/missing provider input must never redirect old cleanup."""
        runtime = '/usr/libexec/node-infra/node-infra-runtime'
        path = '/run/node-infra/netns-inputs/egress'
        before = node.state(guest)['token']
        node.execute('umount', path)
        node.execute('rm', path)
        missing = node.execute(runtime, 'check', check=False)
        require(missing.returncode != 0 and 'no such file' in missing.stderr,
                'missing explicit input did not fail native configuration check')
        node.execute('touch', path)
        node.execute('ip', 'netns', 'add', 'replacement')
        try:
            node.execute('mount', '--bind', '/run/netns/replacement', path)
            node.execute('mount', '--make-private', path)
            changed = node.execute(runtime, 'cleanup', '--force', check=False)
            require(changed.returncode != 0 and 'external input egress was replaced' in changed.stderr,
                    'changed input did not fail the saved ownership check')
            require(node.state(guest)['token'] == before, 'input mismatch cleanup stopped the guest')
            node.execute('umount', path)
        finally:
            node.execute('mount', '--bind', '/run/netns/egress', path)
            node.execute('mount', '--make-private', path)
            node.execute('ip', 'netns', 'delete', 'replacement')
        mounts = node.execute('cat', '/proc/self/mountinfo').stdout
        write_json(self.fixture.output / node.name / 'input-mismatch.json',
                   {'missing': missing.stderr, 'changed': changed.stderr, 'guest_preserved': True,
                    'mountinfo_after_restore': mounts})

    def failure_cases(self, node_type):
        for phase in ('post-start', 'partial-netns', 'unrecorded-netns', 'wrong-type', 'foreign-interface',
                      'consumers', 'conductor', 'proxy'):
            node = node_type(self.fixture, 0, bridge=True)
            self.configure(node)
            fault = node.config / 'fault'
            fault.mkdir()
            failed_unit = 'node-infra-network.service'
            foreign = False
            if phase == 'post-start':
                dropin = fault / '90-fail.conf'
                dropin.write_text('[Service]\nRestart=no\nExecStartPost=/bin/false\n')
                node.extra_mounts.append((dropin, '/etc/systemd/system/connector-vswitch.service.d/90-fail.conf'))
                failed_unit = 'connector-vswitch.service'
            elif phase in ('partial-netns', 'unrecorded-netns'):
                wrapper = fault / 'ip'
                wrapper.write_text('#!/bin/sh\nif [ "$1 $2 $3" = "netns add sw0-switch" ]; then\n'
                                   + ('  touch /run/netns/sw0-switch\n' if phase == 'partial-netns'
                                      else '  /usr/sbin/ip "$@" || exit $?\n')
                                   + '  exit 1\nfi\nexec /usr/sbin/ip "$@"\n')
                wrapper.chmod(0o755)
                node.extra_mounts.append((wrapper, '/opt/node-infra/bin/ip'))
            elif phase == 'wrong-type':
                ordinary = fault / 'egress'
                ordinary.write_text('not a namespace\n')
                node.netns_inputs['egress'] = ordinary
            elif phase == 'foreign-interface':
                command('ip', '-n', self.name, 'link', 'add', 'egress0', 'type', 'dummy')
                foreign = True
            else:
                failed_unit = {'consumers': 'node-infra-consumers.service',
                               'conductor': 'node-conductor.service', 'proxy': 'node-proxy.service'}[phase]
                dropin = fault / '90-fail.conf'
                dropin.write_text('[Service]\nRestart=no\nExecStartPre=/bin/false\n')
                node.extra_mounts.append((dropin, '/etc/systemd/system/' + failed_unit + '.d/90-fail.conf'))
            expected = self.snapshot()
            try:
                node.start(failure=failed_unit)
                node.stop()
                actual = self.snapshot()
                require(actual == expected, 'failed setup changed external resources: ' + phase)
                write_json(self.fixture.output / ('external-failure-' + phase + '.json'),
                           {'before': expected, 'after': actual, 'repeated_cleanup': 'passed'})
                node.remove()
            finally:
                if foreign:
                    current = self.snapshot()
                    require(current == expected, 'refusing cleanup of changed foreign fixture')
                    command('ip', '-n', self.name, 'link', 'delete', 'egress0')
            self.unchanged(phase)

    def close(self):
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=10)
        for name in reversed(self.created):
            command('ip', 'netns', 'delete', name)
        self.created = []


if __name__ == '__main__':
    target(sys.argv[1])
