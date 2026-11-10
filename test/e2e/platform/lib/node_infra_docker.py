"""Direct Docker coverage using the existing prepared node-infra fixture."""
import json
import os
from pathlib import Path
import shutil
import time

from node_infra import Node, Fixture, command, require, wait_for, write_json, network_snapshot

RUNTIME = '/usr/libexec/node-infra/node-infra-runtime'


class DockerNode(Node):
    def __init__(self, fixture, index, *, bridge=False, pools=False):
        self.bridge = bridge
        self.netns_inputs = {}
        self.identity = None
        super().__init__(fixture, index, host=True, transit=False, pools=pools)

    def _install(self):
        # Configuration and business data are ordinary run bindings. There is
        # deliberately no install/prepare helper or host node-infra service.
        (self.data / 'data').mkdir(parents=True, mode=0o700)

    def execute(self, *args, check=True, timeout=120):
        return command('docker', 'exec', self.container, *args, check=check, timeout=timeout)

    def status(self):
        result = self.execute(RUNTIME, 'health', check=False, timeout=15)
        return {'ready': result.returncode == 0}

    def start(self):
        require(command('docker', 'inspect', self.container, check=False).returncode != 0,
                'previous Docker container must be removed before a new generation')
        args = ['docker', 'run', '--detach', '--tty', '--name', self.container, '--pull=never', '--privileged',
                '--cgroupns=private', '--cpus=4', '--memory=6g', '--memory-swap=6g',
                '--stop-timeout=420', '--tmpfs=/run', '--tmpfs=/run/lock', '--tmpfs=/tmp:exec',
                '--network=' + ('bridge' if self.bridge else 'host')]
        if self.bridge:
            args += ['--publish=127.0.0.1::3000', '--publish=127.0.0.1::3001']
        for role in ('node-conductor', 'node-proxy', 'node-telemetry', 'connector-vswitch'):
            args += ['--volume', f'{self.config}/{role}/conf.d:/etc/{role}/conf.d:ro']
        args += ['--volume', f'{self.config}/manifest.yaml:/etc/node-infra/manifest.yaml:ro',
                 '--volume', f'{self.data}/data:/var/lib/node-infra:rw',
                 '--volume', f'{self.fixture.inputs}:/run/node-infra/fixtures:ro']
        if self.pools:
            args += ['--volume', f'{self.config}/placement:/etc/node-infra/placement:ro']
        for name, path in self.netns_inputs.items():
            args += ['--mount', f'type=bind,source={path},target=/run/node-infra/netns-inputs/{name},readonly']
        self.identity = command(*args, self.fixture.image).stdout.strip()
        inspected = json.loads(command('docker', 'inspect', self.identity).stdout)[0]
        if self.bridge:
            ports = inspected['NetworkSettings']['Ports']
            self.api_port = int(ports['3000/tcp'][0]['HostPort'])
            self.proxy_port = int(ports['3001/tcp'][0]['HostPort'])
        wait_for('direct Docker internal readiness', lambda: self.status()['ready'], timeout=180)
        self.execute('/opt/node-infra/bin/node-ctl', 'manifest-key', 'add', '--socket',
                     '/run/sandbox/node-ctl.socket', self.manifest_key)
        record = json.loads(self.execute('cat', '/run/node-infra/recovery/connector.json').stdout)
        require(not record.get('retained', False), 'direct Docker unexpectedly needs outer preparation')
        main = int(self.execute('stat', '-Lc', '%i', '/proc/1/ns/net').stdout)
        planes = {ns['name']: ns['inode'] for ns in record['namespaces']}
        require(len({main, planes['mgmt'], planes['sw0-switch']}) == 3, 'network planes are not distinct')
        write_json(self.fixture.output / self.name / 'identity.json',
                   {'container_id': self.identity, 'image_id': inspected['Image'], 'main_netns': main,
                    'record': record, 'docker_driver': command('docker', 'info', '--format', '{{.CgroupDriver}}').stdout.strip()})

    def assert_outer_limits(self):
        inspected = json.loads(command('docker', 'inspect', self.identity).stdout)[0]
        pid = inspected['State']['Pid']
        relative = Path(f'/proc/{pid}/cgroup').read_text().strip().split(':', 2)[2]
        root = Path('/sys/fs/cgroup') / relative.lstrip('/')
        quota, period = (root / 'cpu.max').read_text().split()
        require(quota != 'max' and int(quota) == 4 * int(period), 'Docker CPU limit is not effective')
        require(int((root / 'memory.max').read_text()) == 6 * 1024**3, 'Docker memory limit is not effective')
        vm_pids = self.execute('pgrep', '-x', 'cloud-hyperviso').stdout.split()
        require(vm_pids, 'no actual guest VM')
        bounded = set()
        for procs in root.rglob('cgroup.procs'):
            for outer_pid in procs.read_text().split():
                try:
                    if Path(f'/proc/{outer_pid}/ns/pid').stat().st_ino != Path(f'/proc/{pid}/ns/pid').stat().st_ino:
                        continue
                    for line in Path(f'/proc/{outer_pid}/status').read_text().splitlines():
                        if line.startswith('NSpid:'):
                            bounded.add(line.split()[-1])
                except FileNotFoundError:
                    pass
        require(set(vm_pids) <= bounded, 'VM descendants escaped Docker outer cgroup')
        write_json(self.fixture.output / self.name / 'cgroup.json',
                   {'path': relative, 'cpu_max': [quota, period], 'memory_max': (root / 'memory.max').read_text(),
                    'vm_pids': vm_pids, 'bounded_pids': sorted(bounded)})

    def stop(self, *, hard=False):
        if self.identity is None:
            return
        self.diagnostics()
        if hard:
            command('docker', 'kill', '--signal=KILL', self.identity)
        else:
            command('docker', 'stop', self.identity, timeout=480)
        inspected = json.loads(command('docker', 'inspect', self.identity).stdout)[0]
        require(not inspected['State']['Running'], 'Docker container still runs')
        logs = command('docker', 'logs', self.identity, check=False)
        (self.fixture.output / self.name / ('forced-stop.log' if hard else 'normal-stop.log')).write_text(logs.stdout + logs.stderr)
        if not hard:
            # A privileged PID-namespace halt may terminate init via SIGINT.
            # Require positive cleanup evidence as well as the native exit.
            require(inspected['State']['ExitCode'] in (0, 130), 'normal Docker shutdown was not clean')
            require('node-infra: network cleanup complete' in logs.stdout + logs.stderr,
                    'normal Docker shutdown did not execute internal cleanup')
        command('docker', 'rm', self.identity)
        self.identity = None

    def diagnostics(self):
        output = self.fixture.output / self.name
        output.mkdir(parents=True, exist_ok=True)
        if self.identity is None:
            return
        for name, args in {'inspect.json': ['docker', 'inspect', self.identity],
                           'journal.log': ['docker', 'exec', self.identity, 'journalctl', '--no-pager'],
                           'units.log': ['docker', 'exec', self.identity, 'systemctl', 'list-units', '--all']}.items():
            result = command(*args, check=False, timeout=30)
            (output / name).write_text(result.stdout + result.stderr)

    def remove(self):
        if not self.reserved:
            return
        self.stop()
        for path in (self.config, self.data):
            shutil.rmtree(path)
        self.reserved = False


def run():
    fixture = Fixture()
    require(os.environ.get('E2E_NODE_INFRA_DOCKER_IMAGE') == fixture.image,
            'Docker and OCI archives must both be prepared from the same image')
    before = network_snapshot()
    try:
        for bridge in (False, True):
            node = DockerNode(fixture, 0, bridge=bridge, pools=True)
            node.start()
            guest = node.create()
            node.write_marker(guest)
            node.assert_native_exec(guest)
            metadata = json.loads(node.guest(guest, '/mmds'))
            require(metadata.get('instanceID') == guest['sandboxID'], 'Docker MMDS failed')
            node.assert_outer_limits()
            for service in ('node-conductor', 'node-proxy', 'connector-vswitch'):
                token = node.state(guest)['token']
                node.execute('systemctl', 'restart', service)
                # Connector restart can stop dependent management services;
                # restart their native units without replacing the network.
                node.execute('systemctl', 'start', 'node-conductor', 'node-proxy')
                wait_for('component restart readiness', lambda: node.status()['ready'])
                require(node.state(guest)['token'] == token, 'component restart killed the guest')
            node.assert_restored(guest, memory=True)
            node.assert_restored(guest, memory=False)
            node.stop()
            node.start()
            node.create()
            node.stop(hard=True)
            node.start()
            node.create()
            node.stop()
            node.remove()
        nodes = [DockerNode(fixture, i, bridge=True) for i in range(3)]
        states = []
        for node in nodes:
            node.start()
            guest = node.create()
            states.append((guest, node.write_marker(guest)))
        nodes[1].stop()
        for i in (0, 2):
            require(nodes[i].state(states[i][0])['token'] == states[i][1]['token'], 'Docker instance stop crossed boundaries')
        for node in nodes:
            node.remove()
        require(network_snapshot() == before, 'Docker lifecycle changed host network state')
        write_json(fixture.output / 'docker-result.json', {'host': 'passed', 'bridge': 'passed', 'three_instances': 'passed'})
    finally:
        for node in fixture.nodes:
            node.diagnostics()
        for node in reversed(fixture.nodes):
            node.remove()


if __name__ == '__main__':
    run()
