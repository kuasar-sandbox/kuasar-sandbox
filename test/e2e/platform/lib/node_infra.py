#!/usr/bin/env python3
"""Exercise the delivered node-infra entry point with private native fixtures.

No component process is started beside the deployed system container. Host
objects are reserved by unique names before use and removed only after verified
instance cleanup. A failed recovery retains its inputs for operator diagnosis.
"""
import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# The outer unit covers internal drain/cleanup and the retained-handle helper.
# Observe the delivered budget before declaring failure, including conmon death.
STOP_TIMEOUT = 600


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def command(*args, check=True, timeout=120, env=None):
    result = subprocess.run([str(x) for x in args], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, env=env)
    if check and result.returncode:
        raise RuntimeError(f'{Path(str(args[0])).name} {args[1] if len(args)>1 else ""} '
                           f'exited {result.returncode}: {result.stderr[-3000:]}')
    return result


def wait_for(description, function, timeout=90):
    deadline = time.monotonic() + timeout
    error = None
    while time.monotonic() < deadline:
        try:
            value = function()
            if value:
                return value
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            error = exc
        time.sleep(0.25)
    raise RuntimeError(f'timed out: {description}' + (f': {error}' if error else ''))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def http(method, url, *, headers=None, body=None, expected=200):
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        headers = {**(headers or {}), 'Content-Type': 'application/json'}
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        data = response.read()
        require(response.status == expected, f'{method} {urllib.parse.urlsplit(url).path}: '
                f'HTTP {response.status}, expected {expected}; {data[:500].decode("utf-8", errors="replace")}')
    return data


def fixture_mac(name):
    """Give a task-owned link a userspace-set, local unicast identity at birth."""
    return '02:' + ':'.join(f'{byte:02x}' for byte in hashlib.sha256(name.encode()).digest()[:5])


class Node:
    def __init__(self, fixture, index, *, extension=False, pools=False, host=False, transit=True, gateway=False):
        self.fixture, self.index = fixture, index
        self.name = fixture.key + str(index)
        self.container = self.service = 'node-infra-' + self.name
        self.service += '.service'
        self.netns = 'node-infra-' + self.name
        self.host, self.with_transit = host, transit
        self.mmds_vip = '169.254.169.254'
        self.network = ['ip'] if host else ['ip', '-n', self.netns]
        self.transit_name = self.name + 't' if host else 'uplink0'
        self.gateway = self.netns + '-gw' if gateway else None
        self.gateway_process = self.gateway_log = None
        self.config = Path('/etc/node-infra/instances') / self.name
        self.data = Path('/var/lib/node-infra/instances') / self.name
        self.run = Path('/run/node-infra/instances') / self.name
        self.unit = Path('/etc/containers/systemd') / (self.container + '.container')
        self.dropin = Path('/etc/systemd/system') / (self.service + '.d')
        self.peers, self.installed, self.reserved = [], False, False
        self.extension, self.pools = extension, pools
        self.ingress = self.transit = self.transit_host = None
        self.original_transit = None
        self.sandboxes = []
        paths = [self.config, self.data, self.run, self.unit, self.dropin, Path('/run/netns') / self.netns]
        if self.gateway:
            paths.append(Path('/run/netns') / self.gateway)
        for path in paths:
            require(not path.exists() and not path.is_symlink(), 'refusing existing instance path: ' + str(path))
        self.config.mkdir(parents=True, mode=0o700)
        self.reserved = True
        fixture.nodes.append(self)
        self._network()
        self._configuration()
        self._install()

    def execute(self, *args, check=True, timeout=120):
        return command('podman', 'exec', self.container, *args, check=check, timeout=timeout)

    def _network(self):
        if self.host:
            self.ingress = '127.0.0.1'
        else:
            command('ip', 'netns', 'add', self.netns)
            command(*self.network, 'link', 'set', 'lo', 'up')
        if self.gateway:
            command('ip', 'netns', 'add', self.gateway)
            command('ip', '-n', self.gateway, 'link', 'set', 'lo', 'up')
        for plane in ([0] if not self.host else []) + ([1] if self.with_transit else []):
            peer = f'{self.name}{plane}h'
            inside = f'{self.name}{plane}p'
            require(len(peer) <= 15, 'test interface name exceeds IFNAMSIZ')
            require(command('ip', 'link', 'show', peer, check=False).returncode != 0, 'interface already exists')
            block = self.fixture.subnet + self.index * 8 + plane * 4
            host, guest = str(ipaddress.IPv4Address(block + 1)), str(ipaddress.IPv4Address(block + 2))
            # Set both identities in the creation request. A kernel-random MAC
            # can otherwise be rewritten by host udev after our first snapshot.
            # A later link-set command would still race the initial add event.
            inside_mac = fixture_mac(inside)
            command('ip', 'link', 'add', peer, 'address', fixture_mac(peer),
                    'type', 'veth', 'peer', 'name', inside, 'address', inside_mac)
            self.peers.append(peer)
            if not self.host:
                command('ip', 'link', 'set', inside, 'netns', self.netns)
            command(*self.network, 'link', 'set', inside, 'name', 'ingress0' if plane == 0 else self.transit_name)
            peer_ip = ['ip']
            if plane == 1 and self.gateway:
                command('ip', 'link', 'set', peer, 'netns', self.gateway)
                peer_ip += ['-n', self.gateway]
                command(*peer_ip, 'link', 'set', peer, 'mtu', '1600')
                command(*self.network, 'link', 'set', self.transit_name, 'mtu', '1600')
            command(*peer_ip, 'addr', 'add', host + '/30', 'dev', peer)
            command(*peer_ip, 'link', 'set', peer, 'up')
            if plane == 0:
                self.ingress = guest
                command(*self.network, 'addr', 'add', guest + '/30', 'dev', 'ingress0')
                command(*self.network, 'link', 'set', 'ingress0', 'up')
                command(*self.network, 'route', 'add', 'default', 'via', host, 'dev', 'ingress0')
            else:
                self.transit, self.transit_host = guest, host
                self.original_transit = inside_mac
                actual = json.loads(command(*self.network, '-j', 'link', 'show', self.transit_name).stdout)[0]['address']
                require(actual == self.original_transit,
                        f'fixture transit MAC changed before handoff: expected {self.original_transit}, got {actual}')

    def _configuration(self):
        for component in ('node-conductor', 'node-proxy', 'node-telemetry', 'connector-vswitch'):
            (self.config / component / 'conf.d').mkdir(parents=True)
        manifest_key = command(self.fixture.bin / 'e2b-key-ctl', 'gen-key').stdout.strip()
        secret = command(self.fixture.bin / 'e2b-key-ctl', 'derive-api-secret', manifest_key).stdout.strip()
        self.api_port, self.proxy_port = 3000, 3001
        self.api_key = command(self.fixture.bin / 'e2b-key-ctl', 'gen-apikey', secret).stdout.strip()
        self.manifest_key = manifest_key
        conductor = {
            'api': {'domain': 'node-infra.test', 'listen': '0.0.0.0:3000'},
            'encryption_key': command(self.fixture.bin / 'e2b-key-ctl', 'gen-key').stdout.strip(),
            'sandbox': {'boot': {'overlay_diff_template': '/run/node-infra/fixtures/overlay.ext4'},
                        'resources': {'capacity': {'cpu': 1, 'memory': '512MiB'}}},
            'checkpoint': {'mode': 'local', 'remote': {'ref_location_parent': 'file:///run/node-infra/fixtures/publications'}},
            'resource_listen': {'enabled': True, 'pools': [{'name': 'default', 'allocatable': {'cpu': 2, 'memory': '3GiB'}}]},
            'units': {'runner_pools': [{'name': 'r0', 'resource_pool': 'default', 'unit': 'sandbox-runner@.service', 'size': 1, 'capacity': 4}],
                      'builder_pools': [{'name': 'b0', 'resource_pool': 'default', 'unit': 'sandbox-builder@.service', 'size': 0, 'capacity': 1}]},
        }
        # A lower layer is deliberately overridden by the instance layer.
        write_json(self.config / 'node-conductor/conf.d/50-platform.yaml', {'api': {'listen': '127.0.0.1:2999'}})
        if self.pools:
            conductor['resource_listen']['pools'] = [
                {'name': f'p{i}', 'allocatable': {'cpu': 1, 'memory': '1536MiB'},
                 'unit_dropins': [f'/etc/node-infra/placement/p{i}.conf']} for i in (0, 1)]
            conductor['units'] = {kind + '_pools': [
                {'name': kind[0] + str(i), 'resource_pool': f'p{i}', 'unit': f'{kind}-p{i}@.service',
                 'size': 1, 'capacity': 1} for i in (0, 1)]
                for kind in ('runner', 'builder')}
            (self.config / 'placement').mkdir()
            for i in (0, 1):
                (self.config / f'placement/p{i}.conf').write_text('[Service]\nCPUWeight=100\n')
        if self.extension:
            # Prepared inputs belong to the ordinary CI runner. Install private
            # root-owned copies for the native SDK's executable trust check,
            # keeping the immutable prepared binaries and their owners intact.
            executables = self.config / 'executables'
            executables.mkdir(mode=0o700)
            for name in ('xproxy', 'xconductor'):
                target = executables / name
                shutil.copyfile(os.environ['NODE_INFRA_' + name.upper() + '_BIN'], target)
                target.chmod(0o755)
            conductor['paths'] = {'conductor_executable': '/opt/node-infra/extensions/xconductor'}
            write_json(self.config / 'node-proxy/conf.d/50-platform.yaml',
                       {'paths': {'proxy_executable': '/opt/node-infra/extensions/xproxy'},
                        # Configure moves this off Conductor's occupied port.
                        'data_listen': '0.0.0.0:3000'})
            private = self.config / 'extensions/r42'
            private.mkdir(parents=True)
            (private / 'marker').write_text(self.name + '-private-r42\n')
            (self.config / 'extensions/service.env').write_text('NODE_INFRA_TEST_CONFIG=/etc/node-infra/extensions/r42/marker\n')
            for component in ('node-conductor', 'node-proxy'):
                dropin = self.config / (component + '.service.d')
                dropin.mkdir()
                (dropin / '20-platform.conf').write_text('[Service]\nEnvironmentFile=/etc/node-infra/extensions/service.env\n')
        write_json(self.config / 'node-conductor/conf.d/90-instance.yaml', conductor)
        connector = {'num_ports': 8}
        if self.with_transit:
            connector.update(transit_dev=self.transit_name, transit_dev_addr=self.transit + '/30:' + self.transit_host,
                             transit_dev_mtu='auto')
        write_json(self.config / 'connector-vswitch/conf.d/90-instance.yaml', connector)
        if self.gateway:
            write_json(self.config / 'connector-vswitch/conf.d/95-transit-test.yaml',
                       {'geneve_encap_eth': True, 'geneve_locator': 'port', 'geneve_port_base': 50000})
        write_json(self.config / 'manifest.yaml', {'manifest': {'key': ''}, 'store': {'endpoint': ''},
                                                  'cache': {'endpoint': ''}, 'crypto': {'local': 'off', 'chunk': 'aes', 'manifest': 'aes'},
                                                  'chunker': {'mode': 'cdc', 'cdc': {'min': '128KiB', 'avg': '512KiB', 'max': '1MiB'}}})

    def _install(self):
        template = (self.fixture.deploy / 'templates/node-infra.container').read_text()
        values = {'NAME': self.name, 'IMAGE': self.fixture.image,
                  'CONFIG_ROOT': str(self.config), 'DATA_ROOT': str(self.data / 'data'), 'RUN_ROOT': str(self.run)}
        for key, value in values.items():
            template = template.replace('@' + key + '@', value)
        if not self.host:
            template = template.replace('Network=host', 'Network=ns:/run/netns/' + self.netns)
        require(not re.search(r'@[A-Z_]+@', template), 'unresolved delivered Quadlet token')
        mounts = [f'{self.fixture.inputs}:/run/node-infra/fixtures:ro']
        if self.pools:
            mounts.append(f'{self.config}/placement:/etc/node-infra/placement:ro')
        if self.extension:
            mounts += [f'{self.config}/executables/xproxy:/opt/node-infra/extensions/xproxy:ro',
                       f'{self.config}/executables/xconductor:/opt/node-infra/extensions/xconductor:ro',
                       f'{self.config}/extensions:/etc/node-infra/extensions:ro']
            for component in ('node-conductor', 'node-proxy'):
                mounts.append(f'{self.config}/{component}.service.d:/etc/systemd/system/{component}.service.d:ro')
        # Native Quadlet settings; production template hooks/privileges remain.
        template += '\n[Container]\n' + ''.join('Volume=' + volume + '\n' for volume in mounts)
        template += '\n[Service]\nCPUQuota=400%\nMemoryMax=6G\nRestart=no\n'
        source = self.fixture.work / (self.container + '.container')
        source.write_text(template)
        # A failed install may already have persisted recovery inputs. Mark the
        # attempt before invoking it so cleanup never discards those bindings.
        self.installed = True
        command(self.fixture.ctl, 'install', '--name', self.name, '--unit', source)
        command('systemctl', 'daemon-reload')

    def status(self):
        result = command(self.fixture.ctl, 'status', '--name', self.name, '--json', check=False)
        require(result.returncode in (0, 1), 'status failed outside readiness contract')
        return json.loads(result.stdout)

    def start(self):
        command(self.fixture.ctl, 'check', '--name', self.name)
        command('systemctl', 'reset-failed', self.service, check=False)
        command('systemctl', 'start', self.service)
        wait_for(self.name + ' readiness', lambda: self.status().get('ready'))
        self.execute('/opt/node-infra/bin/node-ctl', 'manifest-key', 'add', '--socket',
                     '/run/sandbox/node-ctl.socket', self.manifest_key)

    def api(self, method, path, body=None, expected=200, *, headers=None):
        return http(method, f'http://{self.ingress}:{self.api_port}' + path, body=body, expected=expected,
                    headers={'Host': 'api.node-infra.test', 'X-API-KEY': self.api_key, **(headers or {})})

    def assert_units(self):
        units = ('node-infra.target', 'node-infra-network.service', 'node-infra-consumers.service',
                 'connector-vswitch.service', 'node-conductor.service', 'node-proxy.service', 'node-telemetry.service')
        result = self.execute('systemd-analyze', 'verify', *units, check=False)
        write_json(self.fixture.output / self.name / 'systemd-verify.json',
                   {'units': units, 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
        require(result.returncode == 0, 'native systemd unit verification failed: ' + result.stderr)

    def guest(self, sandbox, path='/state', *, method='GET', body=None):
        return http(method, f'http://{self.ingress}:{self.proxy_port}' + path, body=body,
                    headers={'Host': f'8000-{sandbox["sandboxID"]}.node-infra.test',
                             'X-Access-Token': sandbox['forwardAccessToken']})

    def create(self, template=None):
        self.wait_capacity('sandbox')
        request = {'templateID': template or self.fixture.template, 'timeout': 600}
        if self.gateway:
            request['metadata'] = {'kuasar-sandbox.network': json.dumps({
                'inner_ip': '10.231.0.2/24', 'nexthop': '10.231.0.1',
                'transit_gateway_ip': self.transit_host, 'transit_geneve_vni': 100,
                'transit_mac': '02:00:00:00:fd:01'})}
        sandbox = json.loads(self.api('POST', '/sandboxes', request, 201))
        self.sandboxes.append(sandbox)
        wait_for('real guest response', lambda: self.guest(sandbox))
        return sandbox

    def assert_build(self):
        self.wait_capacity('build')
        registration = json.loads(self.api('POST', '/v3/templates',
            {'name': self.name + '-build', 'profile': 'bare', 'cpuCount': 1, 'memoryMB': 512}, 202,
            headers={'X-Kuasar-Sandbox-Builder': json.dumps({'target': {'kind': 'sandbox', 'memory': True}})}))
        tid, bid = registration['templateID'], registration['buildID']
        self.api('POST', f'/v2/templates/{tid}/builds/{bid}', {'fromTemplate': self.fixture.template}, 202)

        def completed():
            status = json.loads(self.api('GET', f'/templates/{tid}/builds/{bid}/status'))
            write_json(self.fixture.output / self.name / 'build-status.json', status)
            return status if status['status'] in ('ready', 'error') else None

        status = wait_for('real Build transaction', completed, timeout=180)
        require(status['status'] == 'ready', 'native Build failed: ' + json.dumps(status))
        require(status['kind'] == 'snp' and status['templateID'].startswith('bare-snp-'),
                'Build did not publish its requested memory snapshot')
        guest = self.create(status['templateID'])
        self.write_marker(guest)
        self.assert_native_exec(guest)
        self.api('DELETE', '/sandboxes/' + guest['sandboxID'], expected=204)

    def wait_capacity(self, kind, *, resume=False):
        def available():
            result = self.execute('curl', '--fail', '--silent', '--unix-socket', '/run/sandbox/node-ctl.socket',
                                  'http://localhost/internal/admin/resource-pressure')
            status = json.loads(result.stdout)
            write_json(self.fixture.output / self.name / ('capacity-' + kind + '.json'), status)
            zones = ('green', 'yellow', 'red') if resume else ('green', 'yellow')
            pools = {p['name'] for p in status.get('pools', []) if p['pressure']['zone'] in zones and
                     all(p['capacity'][dimension] == 0 or p['available'][dimension] >= amount
                         for dimension, amount in (('cpu_milli', 1000), ('memory_bytes', 512 * 1024**2)))}
            return any(p['kind'] == kind and p['resource_pool'] in pools and p['states'].get('idle', 0) > 0
                       for p in status.get('runner_pools', []))
        # Native pressure hysteresis can retain critical after a VM exits.
        # Wait for authoritative placement capacity; an HTTP-200 health read
        # alone does not establish admission or an available execution slot.
        wait_for('native ' + kind + ' placement capacity', available)

    def state(self, sandbox):
        return json.loads(self.guest(sandbox))

    def write_marker(self, sandbox):
        return json.loads(self.guest(sandbox, method='POST', body=(self.name + '-disk').encode()))

    def assert_native_exec(self, sandbox, expected=None):
        token = json.loads(self.api('POST', f'/sandboxes/{sandbox["sandboxID"]}/exec-sessions', {}, 201))['execAccessToken']
        result = command(self.fixture.bin / 'sandbox-ctl', 'exec', '--proxy', f'http://{self.ingress}:{self.proxy_port}',
                         '--proxy-header', 'E2b-Sandbox-Id: ' + sandbox['sandboxID'],
                         '--proxy-header', 'E2b-Sandbox-Service: exec', '--proxy-header', 'X-Access-Token: ' + token,
                         '--', '/node-infra-guest', 'read')
        require(result.stdout == (expected or self.name + '-disk'), 'native exec did not read guest disk state')

    def assert_files(self, sandbox):
        self.guest(sandbox, '/start-envd', method='POST')
        base = f'http://{self.ingress}:{self.proxy_port}'
        headers = {'Host': f'49983-{sandbox["sandboxID"]}.node-infra.test',
                   'X-Access-Token': sandbox['forwardAccessToken'],
                   'Authorization': 'Basic ' + base64.b64encode(b'root:').decode()}
        wait_for('real runtime envd Files service', lambda: http('GET', base + '/health', headers=headers, expected=204) or True)
        http('POST', base + '/init', headers=headers, expected=204,
             body={'envVars': {}, 'defaultUser': 'root', 'defaultWorkdir': '/state',
                   'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
        payload, boundary = self.name + '-files', 'node-infra-files'
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="node-infra-marker"\r\n'
                f'Content-Type: application/octet-stream\r\n\r\n{payload}\r\n--{boundary}--\r\n').encode()
        path = '/files?path=/state/node-infra-marker'
        http('POST', base + path, headers={**headers, 'Content-Type': 'multipart/form-data; boundary=' + boundary}, body=body)
        require(http('GET', base + path, headers=headers).decode() == payload, 'real Files round trip differs')
        self.assert_native_exec(sandbox, payload)
        self.write_marker(sandbox)
        write_json(self.fixture.output / self.name / 'files.json', {'service': 'bundled envd', 'upload_download_exec': 'passed'})

    def assert_transit(self, sandbox):
        require(self.gateway is not None, 'transit assertion requires an owned GENEVE gateway')
        # Same native port-locator contract as connector's GENEVE Ethernet
        # case, with a kernel tunnel endpoint instead of a second data plane.
        slots = json.loads(self.execute('/opt/node-infra/bin/connector-ctl', 'vswitch',
                                        'show', 'slots', 'sw0').stdout)
        active = [slot for slot in slots if slot['allocated'] and slot['inner_ip'] == '10.231.0.2']
        require(len(active) == 1 and active[0]['transit_geneve_vni'] == 100,
                'sandbox transit identity did not reach the native slot')
        slot = active[0]
        network = ['ip', '-n', self.gateway]
        command(*network, 'link', 'add', 'guest0', 'type', 'geneve', 'id', '100',
                'remote', self.transit, 'dstport', 50000 + slot['slot_id'])
        command(*network, 'link', 'set', 'guest0', 'address', '02:00:00:00:fd:01')
        command(*network, 'addr', 'add', '10.231.0.1/24', 'dev', 'guest0')
        command(*network, 'link', 'set', 'guest0', 'up')
        command(*network, 'neigh', 'replace', '10.231.0.2', 'lladdr', slot['port_mac'],
                'nud', 'permanent', 'dev', 'guest0')
        ready = self.fixture.work / (self.name + '-gateway.ready')
        self.gateway_log = (self.fixture.output / (self.name + '-gateway.log')).open('w')
        self.gateway_process = subprocess.Popen(
            ['ip', 'netns', 'exec', self.gateway, sys.executable, '-B',
             str(Path(__file__).with_name('node_infra_gateway.py')), str(ready), self.fixture.key],
            stdout=self.gateway_log, stderr=subprocess.STDOUT)
        wait_for('owned GENEVE gateway listener',
                 lambda: ready.is_file() and self.gateway_process.poll() is None)
        target = 'http://10.231.0.1:18080/'
        actual = self.guest(sandbox, '/egress?' + urllib.parse.urlencode({'url': target}))
        require(actual.decode() == self.fixture.key, 'guest transit traffic did not reach the owned host listener')
        stats = json.loads(self.execute('/opt/node-infra/bin/connector-ctl', 'vswitch', 'stats', 'sw0').stdout)
        require(any(port.get('transit_tx_packets', 0) > 0 and port.get('transit_rx_packets', 0) > 0 for port in stats['ports']),
                'real transit request did not increment native TX/RX counters')

    def assert_outer_limits(self):
        group = command('systemctl', 'show', self.service, '-p', 'ControlGroup', '--value').stdout.strip()
        require(group.startswith('/') and group != '/', 'missing outer instance cgroup')
        root = Path('/sys/fs/cgroup') / group.lstrip('/')
        require((root / 'memory.max').read_text().strip() == str(6 * 1024 ** 3), 'outer memory limit is not effective')
        quota, period = (root / 'cpu.max').read_text().split()
        require(quota != 'max' and int(quota) == 4 * int(period), 'outer CPU limit is not effective')
        processes = command('podman', 'top', self.container, 'pid', 'args').stdout.splitlines()
        pids = [line.split()[0] for line in processes[1:] if 'cloud-hypervisor' in line]
        require(pids, 'no real VMM process beneath the delivered instance')
        init = json.loads(command('podman', 'inspect', self.container).stdout)[0]['State']['Pid']
        namespace = Path('/proc', str(init), 'ns/pid').stat().st_ino
        bounded = set()
        # Podman's hpid formatter can return '?' when it is itself nested.
        # Match the actual namespace/PID through the outer cgroup instead.
        for procs in root.rglob('cgroup.procs'):
            for pid in procs.read_text().split():
                try:
                    if Path('/proc', pid, 'ns/pid').stat().st_ino != namespace:
                        continue
                    status = Path('/proc', pid, 'status').read_text()
                except FileNotFoundError:
                    continue
                bounded.update(line.split()[-1] for line in status.splitlines() if line.startswith('NSpid:'))
        require(set(pids) <= bounded, 'VMM escaped the instance resource boundary')

    def assert_restored(self, sandbox, *, memory):
        before = self.state(sandbox)
        sid = sandbox['sandboxID']
        self.api('POST', f'/sandboxes/{sid}/pause', {'memory': memory}, 204)
        self.wait_capacity('sandbox', resume=True)
        pressure = self.execute('curl', '--fail', '--silent', '--unix-socket', '/run/sandbox/node-ctl.socket',
                                'http://localhost/internal/admin/resource-pressure')
        write_json(self.fixture.output / self.name / ('before-connect-' + str(memory) + '.json'),
                   json.loads(pressure.stdout))
        self.api('POST', f'/sandboxes/{sid}/connect', {'memory': memory, 'timeout': 600}, 200)
        after = wait_for('restored guest response', lambda: self.state(sandbox))
        require(after['disk'] == before['disk'] == self.name + '-disk', 'pause/resume lost persistent disk content')
        if memory:
            require((after['token'], after['boot_id']) == (before['token'], before['boot_id']) and
                    after['count'] > before['count'], 'memory restore lost process state')
        else:
            require(after['token'] != before['token'] and after['boot_id'] != before['boot_id'],
                    'E-only restore did not cold-start the guest')

    def assert_returned(self):
        if self.with_transit:
            link = json.loads(command(*self.network, '-j', 'link', 'show', self.transit_name).stdout)[0]
            require(link['address'] == self.original_transit,
                    f'original transit did not return to the main namespace: expected {self.original_transit}, '
                    f"got {link['address']}")
            require('UP' not in link['flags'], 'returned transit must be DOWN for the next native start')
        else:
            applied = json.loads((self.data / 'applied.json').read_text()) if (self.data / 'applied.json').exists() else {}
            require(applied.get('transit') is None, 'no-transit instance recorded a fictitious device')
        status = self.status()
        require(not status['container']['running'] and not status['recovery']['pending'], 'old instance still runs or needs recovery')

    def stop(self, *, hard=False, verify_order=False):
        if verify_order:
            require(not hard, 'ordered shutdown requires normal stop')
            inspected = json.loads(command('podman', 'inspect', self.container).stdout)[0]
            applied = json.loads((self.data / 'applied.json').read_text())
            identity = inspected['Id']
            output = self.fixture.output / self.name / ('normal-stop-' + identity + '.log')
            output.parent.mkdir(parents=True, exist_ok=True)
            # Follow the actual inner journal before the outer unit removes the
            # container. The unique marker confirms the reader is attached.
            marker = 'node-infra-stop-observer-' + uuid.uuid4().hex
            with output.open('w') as stream:
                reader = subprocess.Popen(['podman', 'exec', identity, 'journalctl', '--follow',
                                           '--no-pager', '-o', 'cat'], stdout=stream, stderr=subprocess.STDOUT)
                try:
                    self.execute('logger', '-t', 'node-infra-e2e', marker)
                    wait_for('inner shutdown journal observer', lambda: marker in output.read_text())
                    self.stop()
                finally:
                    try:
                        reader.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        reader.terminate()
                        reader.wait(timeout=5)
            log = output.read_text()
            position = marker_offset = log.index(marker)
            positions = {}
            for entry in ('node-infra-consumers.service: Deactivated successfully.',
                          'Stopped node-infra-consumers.service',
                          'connector-vswitch.service: Deactivated successfully.',
                          'Stopped connector-vswitch.service',
                          'node-infra: network cleanup complete (outer handles retained)',
                          'node-infra-network.service: Deactivated successfully.',
                          'Stopped node-infra-network.service'):
                position = log.find(entry, position)
                require(position >= 0, 'normal outer stop missed ordered inner shutdown: ' + entry)
                positions[entry] = position
            write_json(output.with_suffix('.json'), {'container_id': identity, 'generation': applied['generation'],
                       'image': inspected['Image'], 'observer_marker': marker, 'marker_offset': marker_offset,
                       'ordered_markers': positions,
                       'log_sha256': hashlib.sha256(output.read_bytes()).hexdigest()})
            return
        if hard:
            command('podman', 'kill', '--signal', 'KILL', self.container)
        else:
            command('systemctl', 'stop', self.service, timeout=STOP_TIMEOUT)
        wait_for('container exit and host recovery', lambda: self._stopped(), timeout=STOP_TIMEOUT)
        self.assert_returned()

    def _stopped(self):
        status = self.status()
        if 'error' in status:
            write_json(self.fixture.output / self.name / f'status-error-{time.time_ns()}.json', status)
            raise RuntimeError('status during recovery: ' + json.dumps(status))
        stopped = (not status['container']['running'] and not status['recovery']['pending'] and
                   status['service']['active'] in ('inactive', 'failed'))
        if not stopped:
            state = command('systemctl', 'show', self.service,
                            '--property=SubState,ControlPID,TimeoutStopUSec,ExecStopPost').stdout.strip()
            control = dict(line.split('=', 1) for line in state.splitlines()).get('ControlPID', '0')
            if control.isdigit() and int(control) > 1:
                try:
                    process = Path('/proc') / control
                    state += '\ncontrol_command=' + process.joinpath('cmdline').read_text().replace('\0', ' ')
                    state += '\ncontrol_wait=' + process.joinpath('wchan').read_text().strip()
                    state += '\ncontrol_children=' + process.joinpath('task', control, 'children').read_text().strip()
                except FileNotFoundError:
                    pass  # The real stop job can finish between these reads.
            raise RuntimeError('stop still pending: ' + json.dumps(status) + '; ' + state)
        return True

    def diagnostics(self):
        if not self.reserved:
            return
        output = self.fixture.output / self.name
        output.mkdir(parents=True, exist_ok=True)
        for name, args in {
            'outer-journal.log': ['journalctl', '-u', self.service, '--no-pager', '-n', '800'],
            'inner-journal.log': ['podman', 'exec', self.container, 'journalctl', '--no-pager', '-n', '1200'],
            'main-network.json': [*self.network, '-j', 'link', 'show'],
        }.items():
            result = command(*args, check=False, timeout=30)
            (output / name).write_text(result.stdout + result.stderr)
        if self.installed:
            write_json(output / 'status.json', self.status())

    def remove(self):
        if not self.reserved:
            return
        if self.installed:
            command('systemctl', 'stop', self.service, timeout=STOP_TIMEOUT)
            command(self.fixture.ctl, 'recover', '--name', self.name, timeout=180)
            self.assert_returned()
            self.unit.unlink(missing_ok=True)
            if self.dropin.exists():
                shutil.rmtree(self.dropin)
            command('systemctl', 'daemon-reload')
            command('systemctl', 'reset-failed', self.service, check=False)
        if self.gateway_process is not None:
            self.gateway_process.terminate()
            try:
                self.gateway_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.gateway_process.kill()
                self.gateway_process.wait(timeout=10)
            self.gateway_log.close()
        for peer in self.peers:
            scope = ['ip', '-n', self.gateway] if self.gateway and peer.endswith('1h') else ['ip']
            command(*scope, 'link', 'delete', peer)
        if self.gateway and (Path('/run/netns') / self.gateway).exists():
            command('ip', 'netns', 'delete', self.gateway)
        if (Path('/run/netns') / self.netns).exists():
            command('ip', 'netns', 'delete', self.netns)
        for path in (self.config, self.data, self.run):
            if path.exists():
                shutil.rmtree(path)
        self.reserved = False


class Fixture:
    def __init__(self):
        require(os.geteuid() == 0, 'node-infra E2E requires root')
        self.deploy = Path(os.environ['E2E_NODE_INFRA_DEPLOY'])
        self.image = os.environ['E2E_NODE_INFRA_IMAGE']
        self.bin = Path(os.environ['BIN'])
        self.ctl = self.deploy / 'bin/node-infra-ctl'
        require(self.ctl.is_file(), 'missing prepared deployment tool')
        self.work, self.output = Path(os.environ['WORK']), Path(os.environ['OUT'])
        self.output.mkdir(parents=True, exist_ok=True)
        self.key = 'ni' + uuid.uuid4().hex[:7]
        self.nodes = []
        self.inputs = self.work / 'node-infra-fixtures'
        self.inputs.mkdir(mode=0o700)
        self.subnet = self._subnet()
        self._image()

    def _subnet(self):
        routes = json.loads(command('ip', '-j', 'route', 'show', 'table', 'all').stdout)
        occupied = [ipaddress.ip_network(row['dst'], strict=False) for row in routes
                    if row.get('dst') and row['dst'] != 'default' and ':' not in row['dst']]
        for _ in range(100):
            offset = int(uuid.uuid4().hex[:4], 16) % 8192 * 16
            candidate = ipaddress.ip_network((int(ipaddress.IPv4Address('198.18.0.0')) + offset, 28))
            if not any(candidate.overlaps(network) for network in occupied):
                return int(candidate.network_address)
        raise RuntimeError('no private test subnet available')

    def _image(self):
        guest = Path(os.environ['NODE_INFRA_GUEST_BIN'])
        require(guest.is_file() and os.access(guest, os.X_OK), 'missing prepared native guest helper')
        rootfs = self.inputs / 'rootfs'
        rootfs.mkdir()
        for name in ('etc', 'proc', 'sys', 'dev', 'tmp', 'state'):
            (rootfs / name).mkdir()
        shutil.copy2(guest, rootfs / 'node-infra-guest')
        (rootfs / 'etc/passwd').write_text('root:x:0:0:root:/root:/node-infra-guest\n')
        (rootfs / 'etc/group').write_text('root:x:0:\n')
        runtime = self.inputs / 'runtime.json'
        write_json(runtime, {'os': 'linux', 'architecture': {'x86_64': 'amd64', 'aarch64': 'arm64'}[os.environ['E2E_ARCH']],
                             'config': {'Entrypoint': ['/node-infra-guest'], 'User': '0:0'}})
        artifact = self.inputs / 'image.tar'
        command(self.bin / 'flatten-ctl', 'export', '--no-progress', '--runtime-config', runtime,
                '--output', artifact, rootfs)
        # Python tarfile mispositions the trailer after this GNU sparse 1.0
        # carrier. GNU tar understands the emitted sparse map and marker.
        members = command('tar', '-tf', artifact).stdout.splitlines()
        markers = [name for name in members if re.fullmatch(r'\.kuasar\.digest\.[0-9a-f]{64}', name)]
        require(len(markers) == 1, 'native Image lacks one canonical digest marker')
        digest = markers[0].rsplit('.', 1)[1]
        location = self.key
        bucket = hashlib.sha256(location.encode()).hexdigest()
        directory = self.inputs / 'publications' / bucket[:2] / bucket[2:4] / location
        directory.mkdir(parents=True)
        shutil.copy2(artifact, directory / (digest + '.image'))
        reference = f'file://{digest}.image@digest:{digest}@location:{location}'
        self.template = 'bare-img-' + base64.urlsafe_b64encode(reference.encode()).decode().rstrip('=')
        overlay = self.inputs / 'overlay.ext4'
        with overlay.open('xb') as stream:
            stream.truncate(256 * 1024 * 1024)
        command('mkfs.ext4', '-F', '-q', '-b', '4096', '-O', '^has_journal', overlay)

    def close(self):
        errors = []
        for node in reversed(self.nodes):
            try:
                node.diagnostics()
            except Exception as error:
                errors.append(f'{node.name} diagnostics: {error}')
            try:
                node.remove()
            except Exception as error:
                errors.append(f'{node.name} cleanup retained owned inputs: {error}')
        require(not errors, '; '.join(errors))


def listener_snapshot():
    listeners = []
    for line in command('ss', '-H', '-lntue').stdout.splitlines():
        fields = line.split()
        inodes = re.findall(r'\bino:(\d+)\b', line)
        require(len(fields) >= 7 and len(inodes) == 1 and int(inodes[0]) > 0,
                'listener lacks an unambiguous socket identity: ' + line)
        # Recv-Q changes with ordinary traffic. Preserve every listening socket,
        # its identity and configuration, including Send-Q's listen backlog.
        listeners.append({'protocol': fields[0], 'state': fields[1], 'backlog': int(fields[3]),
                          'local': fields[4], 'peer': fields[5], 'inode': int(inodes[0])})
    return sorted(listeners, key=lambda item: json.dumps(item, sort_keys=True))


def network_snapshot():
    links = json.loads(command('ip', '-j', 'address', 'show').stdout)
    result = {'links': [{key: row.get(key) for key in ('ifindex', 'ifname', 'address', 'mtu', 'flags', 'link_index')}
                        | {'addresses': [{key: address.get(key) for key in ('family', 'local', 'prefixlen', 'scope', 'label')}
                                         for address in row.get('addr_info', [])]} for row in links],
              'routes': json.loads(command('ip', '-j', '-4', 'route', 'show', 'table', 'all').stdout),
              'routes6': json.loads(command('ip', '-j', '-6', 'route', 'show', 'table', 'all').stdout),
              'listeners': listener_snapshot(),
              'bpf_pins': {str(path): path.stat().st_ino for path in Path('/sys/fs/bpf').rglob('*')}}
    for tool, arguments in (('iptables', ['-S']), ('ip6tables', ['-S']), ('nft', ['-s', 'list', 'ruleset'])):
        if shutil.which(tool):
            result[tool] = command(tool, *arguments).stdout
    return result


def network_unchanged(before):
    after = network_snapshot()
    changes = {key: {'before': value, 'after': after.get(key)}
               for key, value in before.items() if after.get(key) != value}
    require(after == before, 'host network changed: ' + json.dumps(changes, sort_keys=True))
    return True


def host_case(fixture):
    with metadata_sentinel(fixture), socket.socket() as sentinel:
        sentinel.bind(('127.0.0.1', 0))
        sentinel.listen()
        _host_case(fixture, sentinel)
        with socket.create_connection(sentinel.getsockname(), timeout=2):
            connection, _ = sentinel.accept()
            connection.close()


@contextmanager
def metadata_sentinel(fixture):
    name = fixture.key + 'meta'
    require(command('ip', 'link', 'show', name, check=False).returncode != 0, 'metadata fixture name occupied')
    command('ip', 'link', 'add', name, 'type', 'dummy')
    index = json.loads(command('ip', '-j', 'link', 'show', name).stdout)[0]['ifindex']
    try:
        command('ip', 'link', 'set', name, 'up')
        command('ip', 'address', 'add', '169.254.169.254/32', 'dev', name)
        command('ip', 'route', 'add', '169.254.169.0/24', 'dev', name, 'metric', '4096')
        command('nft', 'add', 'table', 'inet', name)
        wait_for('metadata sentinel address duplicate detection', lambda: all(
            not address.get('tentative')
            for link in json.loads(command('ip', '-j', 'address', 'show', name).stdout)
            for address in link.get('addr_info', [])))
        token = ('node-infra-host-metadata-' + fixture.key).encode()

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(2)
                super().setup()

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(token)))
                self.end_headers()
                self.wfile.write(token)

            def log_message(self, *_):
                pass

        class Server(ThreadingHTTPServer):
            daemon_threads = False
            request_queue_size = 128

        with Server(('169.254.169.254', 80), Handler) as server:
            listener = server.socket
            identity = {'inode': os.fstat(listener.fileno()).st_ino, 'address': listener.getsockname()}

            def probe():
                require(os.fstat(listener.fileno()).st_ino == identity['inode'] and
                        listener.getsockname() == identity['address'] and
                        listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1,
                        'metadata listening socket was replaced or stopped')
                require(http('GET', 'http://169.254.169.254/') == token,
                        'host metadata listener response changed')

            worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            worker.start()
            try:
                probe()
                before = network_snapshot()
                write_json(fixture.output / 'metadata-before.json', before)
                (fixture.output / 'metadata-listeners-before.txt').write_text(command('ss', '-H', '-lntue').stdout)
                try:
                    yield
                    probe()
                    # A bridge's carrier/linkdown flags settle after its last
                    # veth disappears. Poll every preserved resource category.
                    wait_for('original host network state', lambda: network_unchanged(before), timeout=30)
                finally:
                    after = network_snapshot()
                    write_json(fixture.output / 'metadata-after.json', after)
                    (fixture.output / 'metadata-listeners-after.txt').write_text(command('ss', '-H', '-lntue').stdout)
                    changes = {key: {'before': value, 'after': after.get(key)}
                               for key, value in before.items() if after.get(key) != value}
                    if changes:
                        print('metadata fixture network difference: ' + json.dumps(changes, sort_keys=True),
                              file=sys.stderr, flush=True)
                require(after == before, 'host metadata resources changed')
                write_json(fixture.output / 'metadata-sentinel.json',
                           {'before': before, 'after': after, 'listener_survived': True,
                            'listener_identity': identity, 'response': token.decode()})
            finally:
                server.shutdown()
                worker.join(timeout=2)
                require(not worker.is_alive(), 'metadata HTTP accept worker did not stop')
    finally:
        current = json.loads(command('ip', '-j', 'link', 'show', name).stdout)[0]
        require(current['ifindex'] == index, 'metadata fixture device was replaced')
        command('ip', 'link', 'delete', name)
        if command('nft', 'list', 'table', 'inet', name, check=False).returncode == 0:
            command('nft', 'delete', 'table', 'inet', name)


def _host_case(fixture, sentinel):
    # The Workbench is the tested host context. No primary netns or ingress
    # device is created: the delivered Quadlet must actually remain host mode.
    before = network_snapshot()
    write_json(fixture.output / 'host-before.json', before)
    node = Node(fixture, 0, host=True, transit=False)
    require(not (Path('/run/netns') / node.netns).exists() and not node.peers,
            'default host fixture created an external namespace/device')
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 3000))
        occupied.listen()
        result = command(fixture.ctl, 'check', '--name', node.name, check=False)
        write_json(fixture.output / 'host-occupied-listener.json',
                   {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
        require(result.returncode != 0 and 'listener' in result.stderr,
                f'occupied native listener check: rc={result.returncode}, '
                f'stdout={result.stdout!r}, stderr={result.stderr!r}')
        with socket.create_connection(occupied.getsockname(), timeout=2) as client:
            connection, _ = occupied.accept()
            # Let the client own TIME_WAIT; the fixture must release the native
            # service port immediately without changing production bind checks.
            client.shutdown(socket.SHUT_WR)
            require(connection.recv(1) == b'', 'listener sentinel did not receive client EOF')
            connection.close()
    # Default management resources now live in mgmt. Conflicting host names,
    # VIPs and floating return routes must remain untouched and allow startup.
    before = network_snapshot()
    node.start()
    inspected = json.loads(command('podman', 'inspect', node.container).stdout)[0]
    applied = json.loads((node.data / 'applied.json').read_text())
    require(inspected['HostConfig']['NetworkMode'] == 'host', 'host case silently used namespace join')
    require(Path('/proc/self/ns/net').stat().st_ino == applied['primary_inode'] ==
            Path(f'/proc/{inspected["State"]["Pid"]}/ns/net').stat().st_ino, 'host namespace identity differs')
    write_json(fixture.output / 'host-identity.json', {'image': inspected['Image'], 'network': 'host',
               'primary_inode': applied['primary_inode'], 'container_id': inspected['Id'],
               'transit': applied['transit'], 'boot_id': applied['boot_id'], 'mmds_vip': node.mmds_vip})
    sandbox = node.create()
    node.write_marker(sandbox)
    node.assert_native_exec(sandbox)
    node.assert_files(sandbox)
    metadata = json.loads(node.guest(sandbox, '/mmds?' + urllib.parse.urlencode({'ip': node.mmds_vip})))
    require(metadata.get('instanceID') == sandbox['sandboxID'], 'MMDS did not identify the real guest')
    node.assert_outer_limits()
    node.assert_restored(sandbox, memory=True)
    node.assert_restored(sandbox, memory=False)
    node.stop(verify_order=True)
    node.start()
    node.create()
    node.stop(hard=True)
    node.start()
    node.create()
    overlay = node.config / 'connector-vswitch/conf.d/90-instance.yaml'
    saved = overlay.read_bytes()
    write_json(overlay, {'switch_name': 'replacement', 'switch_netns': 'replacement-switch', 'transit_dev': 'missing'})
    saved_unit = node.unit.read_bytes()
    node.unit.write_text(re.sub(r'^Image=.*$', 'Image=sha256:' + '0' * 64,
                               saved_unit.decode(), flags=re.MULTILINE))
    inspected = json.loads(command('podman', 'inspect', node.container).stdout)[0]
    os.kill(inspected['State']['ConmonPid'], signal.SIGKILL)
    wait_for('host conmon recovery with old no-transit inputs', node._stopped, timeout=STOP_TIMEOUT)
    node.assert_returned()
    overlay.write_bytes(saved)
    node.unit.write_bytes(saved_unit)
    marker, dropin = interrupt_hook_after_syscall(node, 'mount')
    command('systemctl', 'start', node.service, check=False)
    require(not marker.exists(), 'host partial preparation did not reach mount syscall')
    wait_for('host partial preparation recovery', node._stopped)
    dropin.unlink()
    command('systemctl', 'daemon-reload')
    interrupt_native_helpers(node)
    node.remove()
    # Orthogonal combinations use the same delivered lifecycle and real guest.
    for index, host, transit in ((0, False, False), (0, True, True)):
        extra = Node(fixture, index, host=host, transit=transit)
        extra.start()
        guest = extra.create()
        extra.write_marker(guest)
        extra.assert_native_exec(guest)
        extra.stop()
        extra.remove()
    after = network_snapshot()
    write_json(fixture.output / 'host-after.json', after)
    require(before == after, 'host lifecycle changed pre-existing host network resources')
    write_json(fixture.output / 'host-result.json', {'host_no_transit': 'passed', 'ns_no_transit': 'passed',
               'host_transit': 'passed', 'preserved_host_network': before == after,
               'exec_proxy_mmds_memory_and_disk_restore': 'passed', 'pid1_conmon_partial_prepare_and_helpers': 'passed'})


def deploy_case(fixture):
    host_case(fixture)
    node = Node(fixture, 0, gateway=True)
    node.start()
    sandbox = node.create()
    node.write_marker(sandbox)
    node.assert_native_exec(sandbox)
    node.assert_files(sandbox)
    node.assert_transit(sandbox)
    node.assert_outer_limits()
    node.assert_restored(sandbox, memory=True)
    node.assert_restored(sandbox, memory=False)
    node.api('DELETE', '/sandboxes/' + sandbox['sandboxID'], expected=204)
    node.stop()


def config_case(fixture):
    node = Node(fixture, 0, extension=True, pools=True)
    node.start()
    node.assert_units()
    expected = (node.name + '-private-r42').encode()
    require(node.api('GET', '/node-infra-extension') == expected, 'xconductor private configuration not applied')
    endpoint = f'http://{node.ingress}:3001/node-infra-extension'
    require(http('GET', endpoint) == expected, 'xproxy private configuration not applied')
    node.execute('test', '-S', '/run/sandbox/customer-proxy-stats.sock')
    node.execute('test', '!', '-e', '/run/sandbox/proxy-stats.sock')
    first, second = node.create(), node.create()
    node.write_marker(first)
    node.assert_native_exec(first)
    units = node.execute('systemctl', 'list-units', '--plain', '--no-legend', '--state=active', 'runner-p*@*.service').stdout
    require('runner-p0@' in units and 'runner-p1@' in units, 'real guests did not occupy both native execution pools')
    builders = node.execute('systemctl', 'list-units', '--plain', '--no-legend', '--state=active', 'builder-p*@*.service').stdout
    require('builder-p0@' in builders and 'builder-p1@' in builders, 'native Builder pools did not start')
    node.assert_outer_limits()
    tokens = [node.state(sandbox)['token'] for sandbox in (first, second)]
    node.execute('systemctl', 'restart', 'node-conductor.service')
    wait_for('Conductor restart readiness', lambda: node.status()['ready'])
    require([node.state(sandbox)['token'] for sandbox in (first, second)] == tokens,
            'component restart ended active pool guests')
    master = node.execute('systemctl', 'show', 'node-proxy.service', '-p', 'MainPID', '--value').stdout.strip()
    # A Go master may fork from any runtime thread; /task/<main>/children
    # omits children forked by its other threads.
    children = node.execute('pgrep', '-P', master).stdout.split()
    require(len(children) == 1, 'expected one actual Proxy worker')
    node.execute('kill', '-KILL', children[0])
    wait_for('Proxy worker rebuilt with private configuration', lambda: http('GET', endpoint) == expected)
    wait_for('custom StatsSocket readiness after worker rebuild', lambda: node.status()['ready'])
    after = node.execute('pgrep', '-P', master).stdout.split()
    require(len(after) == 1 and after != children, 'Proxy worker did not restart beneath the same master')
    require(node.state(second)['token'], 'second pool guest did not survive worker restart')
    node.execute('systemd-run', '--unit=node-infra-ignore-term.service', '--slice=sandbox-runner.slice',
                 '--property=TimeoutStopSec=2s', '--property=KillMode=control-group', '/bin/sh', '-c',
                 'trap "echo TERM >>/var/lib/node-infra/term-seen" TERM; '
                 'touch /run/ignore-term-ready; while :; do sleep 1; done')
    wait_for('controlled TERM-ignoring consumer', lambda: node.execute('test', '-e', '/run/ignore-term-ready', check=False).returncode == 0)
    node.stop(verify_order=True)
    require('TERM' in (node.data / 'data/term-seen').read_text(), 'drain did not signal the controlled consumer')
    write_json(fixture.output / node.name / 'consumer-timeout.json',
               {'term_observed': True, 'native_kill_budget_seconds': 2, 'outer_recovery_confirmed_no_consumers': True})
    # Static native validation intentionally does not execute custom Configure.
    # Its real startup failure occurs after the network unit has been prepared.
    marker = node.config / 'extensions/r42/marker'
    saved = marker.read_bytes()
    marker.unlink()
    command(fixture.ctl, 'check', '--name', node.name, '--json')
    command('systemctl', 'start', node.service)
    # Restart=on-failure may leave the unit in auto-restart instead of failed.
    # Inspect its real exit result while requiring the prepared network alive.
    wait_for('custom Configure startup rejection', lambda: node.execute(
        'systemctl', 'show', 'node-conductor.service', '-p', 'ExecMainStatus', '--value',
        check=False).stdout.strip() not in ('', '0'))
    node.execute('systemctl', 'is-active', 'connector-vswitch.service')
    require(not node.status()['ready'], 'partial component startup was reported ready')
    node.stop()
    marker.write_bytes(saved)


def interrupt_hook_after_syscall(node, operation):
    """Execute the real mount syscall, then kill only this instance's host hook."""
    directory = node.fixture.work / (node.name + '-' + operation)
    directory.mkdir(mode=0o700)
    marker = directory / 'armed'
    marker.touch()
    wrapper = directory / operation
    wrapper.write_text('#!/usr/bin/python3\n'
                       'import os,signal,subprocess,sys\nfrom pathlib import Path\n'
                       'parent=os.getppid()\n'
                       f'subprocess.run([{str(Path("/usr/bin") / operation)!r},*sys.argv[1:]],check=True)\n'
                       f'marker=Path({str(marker)!r})\n'
                       f'if sys.argv[-1]=={str(node.run / "bpf")!r} and marker.exists():\n'
                       ' command=Path(f"/proc/{parent}/cmdline").read_bytes()\n'
                       f' assert parent>1 and b"node-infra-ctl" in command and {node.name.encode()!r} in command\n'
                       ' marker.unlink()\n os.kill(parent,signal.SIGKILL)\n')
    wrapper.chmod(0o755)
    dropin = node.dropin / '99-test-fault.conf'
    dropin.write_text('[Service]\nEnvironment=PATH=' + str(directory) +
                      ':/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n')
    command('systemctl', 'daemon-reload')
    return marker, dropin


def lifecycle_case(fixture):
    first, second = Node(fixture, 0), Node(fixture, 1)
    first.start()
    second.start()
    guest_a, guest_b = first.create(), second.create()
    first.write_marker(guest_a)
    before = second.write_marker(guest_b)
    first.stop(hard=True)
    after = second.state(guest_b)
    require(after['token'] == before['token'] and after['disk'] == before['disk'] and after['boot_id'] == before['boot_id'],
            'one instance recovery disrupted the other guest')
    first.start()
    replacement = first.create()
    first.write_marker(replacement)
    first.assert_native_exec(replacement)
    first.stop()
    require(second.state(guest_b)['token'] == before['token'], 'normal stop disrupted the second instance')
    first.start()
    first.create()
    # Recovery must use recorded identities even when the current YAML changes.
    overlay = first.config / 'connector-vswitch/conf.d/90-instance.yaml'
    saved = overlay.read_bytes()
    write_json(overlay, {'switch_name': 'replacement', 'switch_netns': 'replacement-switch'})
    inspected = json.loads(command('podman', 'inspect', first.container).stdout)[0]
    conmon = inspected['State']['ConmonPid']
    require(isinstance(conmon, int) and conmon > 1, 'missing actual conmon PID')
    os.kill(conmon, signal.SIGKILL)
    wait_for('conmon exit and old-generation recovery', first._stopped, timeout=STOP_TIMEOUT)
    first.assert_returned()
    require(second.state(guest_b)['token'] == before['token'], 'conmon failure disrupted the second instance')
    overlay.write_bytes(saved)
    first.start()
    first.create()
    # A conflicting input must preserve the original device and all recovery
    # handles. After consumers end, remove BPF metadata to exercise the journal
    # fallback instead of relying on a successful native Stop's metadata.
    network = first.network
    command(*network, 'link', 'add', first.transit_name, 'type', 'dummy')
    conflict = json.loads(command(*network, '-j', 'link', 'show', first.transit_name).stdout)[0]['address']
    require(conflict != first.original_transit, 'failure fixture reused original transit identity')
    command('systemctl', 'stop', first.service, check=False, timeout=180)
    wait_for('failed cleanup retained inputs', lambda: first.status()['recovery']['pending'])
    require((first.run / 'netns/sw0-switch').stat().st_ino ==
            next(ns['inode'] for ns in json.loads((first.data / 'recovery/connector.json').read_text())['namespaces'] if ns['name'] == 'sw0-switch'),
            'failed cleanup lost the original namespace')
    require(command(fixture.ctl, 'check', '--name', first.name, check=False).returncode != 0,
            'unfinished recovery allowed a new generation')
    require(second.state(guest_b)['token'] == before['token'], 'cleanup failure disrupted the second instance')
    shutil.rmtree(first.run / 'bpf/sw0', ignore_errors=False)
    current = json.loads(command(*network, '-j', 'link', 'show', first.transit_name).stdout)[0]['address']
    require(current == conflict, 'conflicting fixture device was replaced')
    command(*network, 'link', 'delete', first.transit_name)
    command(fixture.ctl, 'recover', '--name', first.name)
    first.assert_returned()
    marker, dropin = interrupt_hook_after_syscall(first, 'mount')
    command('systemctl', 'start', first.service, check=False)
    require(not marker.exists(), 'mount interruption did not execute the real syscall')
    wait_for('startup journal recovery after SIGKILL', first._stopped)
    first.assert_returned()
    dropin.unlink()
    command('systemctl', 'daemon-reload')
    first.start()
    first.create()
    marker, dropin = interrupt_hook_after_syscall(first, 'umount')
    command('systemctl', 'stop', first.service, check=False, timeout=180)
    require(not marker.exists(), 'unmount interruption did not execute the real syscall')
    require(json.loads((first.data / 'applied.json').read_text())['state'] == 'releasing',
            'unmount did not persist its retryable phase')
    dropin.unlink()
    command('systemctl', 'daemon-reload')
    command(fixture.ctl, 'recover', '--name', first.name)
    first.assert_returned()
    interrupt_native_helpers(first)
    require(second.state(guest_b)['token'] == before['token'], 'hook interruption disrupted the second instance')
    second.stop()


def interrupt_native_helpers(node, operations=(('prepare', 'hook'), ('prepare', 'cli'), ('cleanup', 'hook'))):
    evidence = []
    for operation, victim in operations:
        if operation == 'cleanup':
            node.start()
            sandbox = node.create()
            node.write_marker(sandbox)
            node.assert_native_exec(sandbox)
            node.assert_files(sandbox)
            # Leave a genuine failed cleanup for an operator's manual retry.
            # The retry runs outside the unit cgroup: systemd cannot reap its
            # orphan helper for us when we subsequently kill the host CLI.
            if node.with_transit:
                command(*node.network, 'link', 'add', node.transit_name, 'type', 'dummy')
            else:
                # Fail this instance's outer stop hook before recovery. The
                # stopped container and applied handles must survive for the
                # manual helper test, without inventing a transit device.
                deferred = node.dropin / '99-test-defer-recovery.conf'
                deferred.write_text('[Service]\nExecStopPost=\nExecStopPost=/usr/bin/false\n')
                command('systemctl', 'daemon-reload')
            command('systemctl', 'stop', node.service, check=False, timeout=180)
            wait_for('retained cleanup for manual retry', lambda: node.status()['recovery']['pending'])
            if node.with_transit:
                command(*node.network, 'link', 'delete', node.transit_name)
            else:
                deferred.unlink()
                command('systemctl', 'daemon-reload')
        recovery = node.data / 'recovery'
        recovery.mkdir(mode=0o700, exist_ok=True)
        with (recovery / '.lock').open('a') as lock:
            # The delivered runtime acquires this native journal lock before
            # its first record/write or cleanup effect. No test product shim.
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if operation == 'prepare':
                command('systemctl', 'reset-failed', node.service, check=False)
                command('systemctl', 'start', '--no-block', node.service)
            else:
                interrupted_log = (node.fixture.output / 'interrupted-manual-helper.log').open('w')
                interrupted = subprocess.Popen([str(node.fixture.ctl), 'recover', '--name', node.name],
                                               stdout=interrupted_log, stderr=subprocess.STDOUT)
                interrupted_log.close()

            def blocked_helper():
                applied = json.loads((node.data / 'applied.json').read_text())
                helper = applied.get('helper', {})
                if helper.get('operation') != operation or not helper.get('id'):
                    return False
                container = json.loads(command('podman', 'inspect', helper['id']).stdout)[0]
                return container if container['State']['Running'] else False

            helper = wait_for('native ' + operation + ' helper blocked before journal', blocked_helper)
            identity, pid = helper['Id'], helper['State']['Pid']
            require(pid > 1, 'helper has no real process')
            if operation == 'prepare':
                require(not (recovery / 'connector.json').exists(), 'prepare pause missed first-record window')
            hook = (interrupted.pid if operation == 'cleanup' else
                    int(command('systemctl', 'show', node.service, '-p', 'ControlPID', '--value').stdout))
            require(hook > 1 and b'node-infra-ctl' in Path(f'/proc/{hook}/cmdline').read_bytes(),
                    'expected the actual host hook')
            target = hook
            if victim == 'cli':
                children = command('pgrep', '-P', str(hook)).stdout.split()
                matches = [int(child) for child in children if
                           b'podman\x00start\x00' in Path(f'/proc/{child}/cmdline').read_bytes()]
                require(len(matches) == 1, 'expected one attached Podman start CLI')
                target = matches[0]
            os.kill(target, signal.SIGKILL)

            def reaped():
                result = command('podman', 'container', 'exists', identity, check=False)
                require(result.returncode in (0, 1), 'cannot inspect interrupted helper')
                return result.returncode == 1 and not Path(f'/proc/{pid}').exists()

            if operation == 'prepare':
                wait_for('prepare helper reaped before releasing journal lock', reaped)
                wait_for('interrupted startup recovered', node._stopped)
                require(not (recovery / 'connector.json').exists(), 'old helper wrote after recovery')
            else:
                require(interrupted.wait(timeout=15) == -signal.SIGKILL, 'manual recovery was not interrupted')
                require(json.loads(command('podman', 'inspect', identity).stdout)[0]['State']['Running'],
                        'manual helper did not survive its host hook; missed the orphan window')
                # Exercise manual recovery independently of ExecStopPost.
                with (node.fixture.output / 'manual-helper-recover.log').open('w') as output:
                    process = subprocess.Popen([str(node.fixture.ctl), 'recover', '--name', node.name],
                                               stdout=output, stderr=subprocess.STDOUT)
                    try:
                        wait_for('manual recovery reaped interrupted cleanup helper', reaped)
                    finally:
                        fcntl.flock(lock, fcntl.LOCK_UN)
                    require(process.wait(timeout=180) == 0, 'manual helper recovery failed; see retained log')
            evidence.append({'operation': operation, 'killed': victim, 'helper_id': identity,
                             'helper_pid': pid, 'reaped_before_unlock': True})
        node.assert_returned()
        node.start()
        sandbox = node.create()
        node.write_marker(sandbox)
        node.assert_native_exec(sandbox)
        node.assert_files(sandbox)
        node.stop()
    write_json(node.fixture.output / (node.name + '-helper-interruptions.json'), evidence)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('case', choices=('deploy', 'config', 'lifecycle'))
    args = parser.parse_args()
    fixture = Fixture()
    try:
        {'deploy': deploy_case, 'config': config_case, 'lifecycle': lifecycle_case}[args.case](fixture)
    finally:
        fixture.close()
    print('PASS: node-infra ' + args.case + ' through delivered image, tool and Quadlet')


if __name__ == '__main__':
    main()
