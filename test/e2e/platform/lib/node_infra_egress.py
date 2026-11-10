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
        self.process = None
        self.created = []
        try:
            for name in (self.name, self.target):
                require(not (Path('/run/netns') / name).exists(), 'external fixture already exists')
                command('ip', 'netns', 'add', name)
                self.created.append(name)
                command('ip', '-n', name, 'link', 'set', 'lo', 'up')
            command('ip', '-n', self.name, 'link', 'add', 'out0', 'type', 'veth',
                    'peer', 'name', 'target0', 'netns', self.target)
            for name, device, address in ((self.name, 'out0', '192.0.2.2/30'),
                                          (self.target, 'target0', '192.0.2.1/30')):
                command('ip', '-n', name, 'address', 'add', address, 'dev', device)
                command('ip', '-n', name, 'link', 'set', device, 'up')
            command('ip', '-n', self.name, 'route', 'add', 'default', 'via', '192.0.2.1')
            command('ip', 'netns', 'exec', self.name, 'sysctl', '-w', 'net.ipv4.ip_forward=1')
            rules = fixture.work / 'provider-egress.nft'
            rules.write_text('table ip provider { chain snat { type nat hook postrouting priority srcnat; '
                             'oifname "out0" snat to 192.0.2.2; } }\n')
            command('ip', 'netns', 'exec', self.name, 'nft', '-f', rules)
            ready = fixture.work / 'egress-target.ready'
            self.process = subprocess.Popen(['ip', 'netns', 'exec', self.target, sys.executable,
                                             str(Path(__file__)), str(ready)])
            wait_for('controlled egress target', ready.exists)
            self.before = self.snapshot()
            write_json(fixture.output / 'external-before.json', self.before)
        except Exception:
            self.close()
            raise

    def snapshot(self):
        result = {'inode': (Path('/run/netns') / self.name).stat().st_ino}
        for label, args in {'links': ['address', 'show'], 'routes': ['-4', 'route', 'show', 'table', 'all']}.items():
            rows = json.loads(command('ip', '-n', self.name, '-j', *args).stdout)
            if label == 'links':
                rows = [{k: row.get(k) for k in ('ifindex', 'ifname', 'address', 'mtu', 'flags', 'addr_info')}
                        for row in rows]
            result[label] = rows
        result['rules'] = command('ip', 'netns', 'exec', self.name, 'nft', '-s', 'list', 'ruleset').stdout
        return result

    def configure(self, node):
        node.netns_inputs['egress'] = Path('/run/netns') / self.name
        write_json(node.config / 'connector-vswitch/conf.d/95-egress.yaml',
                   {'mgmt_extracts': ['mgmt:mgmt0:169.254.169.254/32', 'egress:egress0:0.0.0.0/0']})

    def traffic(self, node, guest):
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

    def close(self):
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=10)
        for name in reversed(self.created):
            command('ip', 'netns', 'delete', name)
        self.created = []


if __name__ == '__main__':
    target(sys.argv[1])
