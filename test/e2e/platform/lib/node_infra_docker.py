"""Direct Docker coverage using the existing prepared node-infra fixture."""
import json
import hashlib
import ipaddress
import os
from pathlib import Path
import shutil
import subprocess
import time

from node_infra import Node, Fixture, command, require, wait_for, write_json, network_snapshot, metadata_sentinel
from node_infra_egress import ExternalEgress

RUNTIME = '/usr/libexec/node-infra/node-infra-runtime'


class DockerNode(Node):
    retained = False
    def __init__(self, fixture, index, *, bridge=False, pools=False):
        self.engine_environment = os.environ.copy()
        self.bridge = bridge
        self.netns_inputs = {}
        self.extra_mounts = []
        self.failed_start = False
        self.identity = None
        super().__init__(fixture, index, host=True, transit=False, pools=pools)

    def _install(self):
        # Configuration and business data are ordinary run bindings. There is
        # deliberately no install/prepare helper or host node-infra service.
        (self.data / 'data').mkdir(parents=True, mode=0o700)
        if self.pools:
            shutil.copytree(self.fixture.inputs / 'publications', self.data / 'data/publications')
            write_json(self.config / 'node-conductor/conf.d/95-build.yaml',
                       {'builder': {'diff_template': '/run/node-infra/fixtures/overlay.ext4'},
                        'checkpoint': {'remote': {'ref_location_parent': 'file:///var/lib/node-infra/publications',
                                                  'manifest': True}}})
            write_json(self.config / 'connector-vswitch/conf.d/96-otlp.yaml',
                       {'mgmt_services': ['169.254.169.254:80:127.0.0.1:19254',
                                          '169.254.169.254:4318:127.0.0.1:4318']})
            write_json(self.config / 'node-telemetry/conf.d/90-otlp.yaml', {
                'query': {'backend': 'local', 'handler': 'e2b'},
                'local': {'enabled': True, 'path': '/var/lib/node-infra/telemetry'},
                'collector': {'receivers': {'sandboxotlp': {'http_listen': '127.0.0.1:4318'}},
                    'processors': {'batch': {'timeout': '1s'}}, 'exporters': {'sandboxlocal': {}},
                    'service': {'telemetry': {'metrics': {'level': 'none'}},
                        'pipelines': {'metrics': {'receivers': ['sandboxotlp'], 'processors': ['batch'],
                                                  'exporters': ['sandboxlocal']}}}}})

    def docker(self, *arguments, **options):
        return command('docker', *arguments, env=self.engine_environment, **options)

    def execute(self, *args, check=True, timeout=120):
        return self.docker('exec', self.container, *args, check=check, timeout=timeout)

    def status(self):
        result = self.execute(RUNTIME, 'health', check=False, timeout=15)
        return {'ready': result.returncode == 0}

    def start(self, *, failure=None):
        self.failed_start = failure is not None
        require(self.docker('inspect', self.container, check=False).returncode != 0,
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
        for source, destination in self.extra_mounts:
            args += ['--mount', f'type=bind,source={source},target={destination},readonly']
        self.identity = self.launch(args)
        inspected = json.loads(self.docker('inspect', self.identity).stdout)[0]
        if self.bridge:
            ports = inspected['NetworkSettings']['Ports']
            self.api_port = int(ports['3000/tcp'][0]['HostPort'])
            self.proxy_port = int(ports['3001/tcp'][0]['HostPort'])
        if failure:
            wait_for('expected native startup failure',
                     lambda: self.execute('systemctl', 'is-failed', failure, check=False).returncode == 0)
            require(not self.status()['ready'], 'failed native startup was marked ready')
            self.execute('systemctl', 'stop', 'node-infra.target')
            for _ in range(2):
                self.execute(RUNTIME, 'cleanup', '--internal', '--force')
            require(not self.execute('ip', 'netns', 'list').stdout.strip(), 'partial startup leaked a namespace')
            write_json(self.fixture.output / self.name / 'failed-start.json', {'unit': failure, 'repeated_cleanup': 'passed'})
            return
        wait_for('direct Docker internal readiness', lambda: self.status()['ready'], timeout=180)
        require(self.execute('systemctl', 'is-enabled', 'nftables', check=False).stdout.strip() == 'masked',
                'distribution nftables service must not own the host ruleset')
        if self.pools:
            self.execute('systemctl', 'start', 'node-telemetry')
        self.execute('/opt/node-infra/bin/node-ctl', 'manifest-key', 'add', '--socket',
                     '/run/sandbox/node-ctl.socket', self.manifest_key)
        record = json.loads(self.execute('cat', '/run/node-infra/recovery/connector.json').stdout)
        require(record.get('retained', False) == self.retained, 'unexpected Docker handle ownership')
        main = int(self.execute('stat', '-Lc', '%i', '/proc/1/ns/net').stdout)
        planes = {ns['name']: ns['inode'] for ns in record['namespaces']}
        require(len({main, planes['mgmt'], planes['sw0-switch']}) == 3, 'network planes are not distinct')
        write_json(self.fixture.output / self.name / 'identity.json',
                   {'container_id': self.identity, 'image_id': inspected['Image'], 'main_netns': main,
                    'record': record, 'docker_driver': self.docker('info', '--format', '{{.CgroupDriver}}').stdout.strip()})

    def launch(self, arguments):
        return self.docker(*arguments[1:], self.fixture.image).stdout.strip()

    def assert_outer_limits(self):
        inspected = json.loads(self.docker('inspect', self.identity).stdout)[0]
        pid = inspected['State']['Pid']
        relative = Path(f'/proc/{pid}/cgroup').read_text().strip().split(':', 2)[2]
        init_group = Path('/sys/fs/cgroup') / relative.lstrip('/')
        # systemd moves PID 1 into init.scope. Limits belong to the runtime's
        # outer group, which also contains execution units and their VMs.
        expected = {self.identity, 'docker-' + self.identity + '.scope'}
        root = next((group for group in (init_group, *init_group.parents) if group.name in expected), None)
        require(root is not None, 'cannot identify Docker outer cgroup from actual init ancestry')
        relative = '/' + str(root.relative_to('/sys/fs/cgroup'))
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
            self.docker('kill', '--signal=KILL', self.identity)
        else:
            self.docker('stop', self.identity, timeout=480)
        inspected = json.loads(self.docker('inspect', self.identity).stdout)[0]
        require(not inspected['State']['Running'], 'Docker container still runs')
        logs = self.docker('logs', self.identity, check=False)
        (self.fixture.output / self.name / ('forced-stop.log' if hard else 'normal-stop.log')).write_text(logs.stdout + logs.stderr)
        if not hard:
            # A privileged PID-namespace halt may terminate init via SIGINT.
            # Require positive cleanup evidence as well as the native exit.
            require(inspected['State']['ExitCode'] in (0, 130), 'normal Docker shutdown was not clean')
            require(self.failed_start or 'node-infra: network cleanup complete' in logs.stdout + logs.stderr,
                    'normal Docker shutdown did not execute internal cleanup')
        self.docker('rm', self.identity)
        self.identity = None

    def diagnostics(self):
        output = self.fixture.output / self.name
        output.mkdir(parents=True, exist_ok=True)
        if self.identity is None:
            return
        for name, args in {'inspect.json': ['docker', 'inspect', self.identity],
                           'journal.log': ['docker', 'exec', self.identity, 'journalctl', '--no-pager'],
                           'units.log': ['docker', 'exec', self.identity, 'systemctl', 'list-units', '--all']}.items():
            result = command(*args, check=False, timeout=30, env=self.engine_environment)
            (output / name).write_text(result.stdout + result.stderr)

    def remove(self):
        if not self.reserved:
            return
        self.stop()
        for path in (self.config, self.data):
            shutil.rmtree(path)
        self.reserved = False


class RetainedDockerNode(DockerNode):
    retained = True

    def __init__(self, fixture, index):
        self.engine_environment = os.environ.copy()
        self.bridge, self.netns_inputs, self.identity = False, {}, None
        self.extra_mounts, self.failed_start = [], False
        Node.__init__(self, fixture, index, host=True, transit=True)

    def status(self):
        result = command(self.fixture.ctl, 'status', '--runtime', 'docker', '--name', self.name,
                         '--json', check=False, env=self.engine_environment)
        require(result.returncode in (0, 1), 'retained Docker status failed outside readiness contract')
        return json.loads(result.stdout)

    def launch(self, arguments):
        for path in (self.run / 'netns', self.run / 'bpf', self.data / 'recovery'):
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
        driver = self.docker('info', '--format', '{{.CgroupDriver}}').stdout.strip()
        parent = ('nodeinfra' + hashlib.sha256(self.name.encode()).hexdigest()[:16] + '.slice'
                  if driver == 'systemd' else '/node-infra-' + self.name)
        args = ['docker', 'create', *[arg for arg in arguments[2:] if arg != '--detach'],
                '--cgroup-parent=' + parent,
                '--volume', f'{self.run}/netns:/run/netns:rw,rslave',
                '--volume', f'{self.run}/bpf:/sys/fs/bpf:rw',
                '--volume', f'{self.data}/recovery:/run/node-infra/recovery:rw']
        identity = self.docker(*args[1:], self.fixture.image).stdout.strip()
        # Retain the exact ID even when preparation fails; cleanup must never
        # replace it with a container subsequently created under the same name.
        self.identity = identity
        command(self.fixture.ctl, 'prepare', '--runtime', 'docker', '--name', self.name, '--container', identity, env=self.engine_environment)
        self.docker('start', identity)
        command(self.fixture.ctl, 'started', '--runtime', 'docker', '--name', self.name, env=self.engine_environment)
        return identity

    def stop(self, *, hard=False):
        if self.identity is None:
            return
        super().stop(hard=hard)
        command(self.fixture.ctl, 'recover', '--runtime', 'docker', '--name', self.name, env=self.engine_environment)
        command(self.fixture.ctl, 'recover', '--runtime', 'docker', '--name', self.name, env=self.engine_environment)
        self.assert_returned()
        write_json(self.fixture.output / self.name / ('retained-kill.json' if hard else 'retained-normal.json'),
                   json.loads((self.data / 'applied.json').read_text()))

    def remove(self):
        if self.reserved:
            self.stop()
            Node.remove(self)


def cgroupfs_case(fixture):
    """A task-private Docker daemon, independent of host systemd delegation."""
    data = fixture.work / 'cgroupfs-daemon'
    runtime = Path('/run') / (fixture.key + '-docker')
    runtime.mkdir(mode=0o700)
    endpoint = 'unix://' + str(runtime / 'docker.sock')
    config = runtime / 'daemon.json'
    config.write_text(json.dumps({'storage-driver': 'overlay2'}) + '\n')
    original = os.environ.copy()
    # --bridge=none makes dockerd delete the default docker0 in its network
    # namespace. Give the task daemon its own explicit bridge instead.
    bridge = fixture.key + 'd'
    require(command('ip', 'link', 'show', bridge, check=False).returncode != 0, 'task bridge already exists')
    command('ip', 'link', 'add', bridge, 'type', 'bridge')
    bridge_index = json.loads(command('ip', '-j', 'link', 'show', bridge).stdout)[0]['ifindex']
    bridge_address = str(ipaddress.IPv4Address(fixture.subnet + 13)) + '/30'
    log = (fixture.output / 'cgroupfs-daemon.log').open('w')
    daemon = subprocess.Popen(['dockerd', '--config-file=' + str(config), '--host=' + endpoint, '--data-root=' + str(data),
        '--exec-root=' + str(runtime / 'exec'), '--pidfile=' + str(runtime / 'pid'),
        '--exec-opt=native.cgroupdriver=cgroupfs', '--bridge=' + bridge, '--bip=' + bridge_address, '--iptables=false',
        '--ip-forward=false', '--ip-masq=false'], stdout=log, stderr=subprocess.STDOUT)
    node = None
    try:
        os.environ.pop('DOCKER_CONTEXT', None)
        os.environ['DOCKER_HOST'] = endpoint
        wait_for('isolated cgroupfs Docker daemon',
                 lambda: command('docker', 'info', check=False).returncode == 0)
        require(command('docker', 'info', '--format', '{{.CgroupDriver}}').stdout.strip() == 'cgroupfs',
                'isolated daemon did not use cgroupfs')
        producer = subprocess.Popen(['docker', 'image', 'save', fixture.image],
                                    env=original, stdout=subprocess.PIPE)
        try:
            subprocess.run(['docker', 'image', 'load'], stdin=producer.stdout, check=True)
        finally:
            producer.stdout.close()
            require(producer.wait(timeout=120) == 0, 'same-image export to isolated daemon failed')
        node = DockerNode(fixture, 0)
        node.start()
        guest = node.create()
        node.write_marker(guest)
        node.assert_native_exec(guest)
        node.assert_outer_limits()
        node.stop()
        node.remove()
        node = RetainedDockerNode(fixture, 0)
        for hard in (False, True):
            node.start()
            node.create()
            node.stop(hard=hard)
        node.remove()
        write_json(fixture.output / 'cgroupfs-result.json', {'driver': 'cgroupfs', 'create_exec_limits_stop': 'passed'})
    finally:
        try:
            if node:
                node.remove()
        finally:
            if 'DOCKER_HOST' in original:
                os.environ['DOCKER_HOST'] = original['DOCKER_HOST']
            else:
                os.environ.pop('DOCKER_HOST', None)
            if 'DOCKER_CONTEXT' in original:
                os.environ['DOCKER_CONTEXT'] = original['DOCKER_CONTEXT']
            daemon.terminate()
            daemon.wait(timeout=30)
            log.close()
            actual = json.loads(command('ip', '-j', 'link', 'show', bridge).stdout)[0]
            require(actual['ifindex'] == bridge_index, 'task Docker bridge identity changed')
            command('ip', 'link', 'delete', bridge)


def run():
    fixture = Fixture()
    with metadata_sentinel(fixture):
        run_cases(fixture)


def run_cases(fixture):
    require(os.environ.get('E2E_NODE_INFRA_DOCKER_IMAGE') == fixture.image,
            'Docker and OCI archives must both be prepared from the same image')
    before = network_snapshot()
    try:
        for bridge in (False, True):
            node = DockerNode(fixture, 0, bridge=bridge, pools=True)
            node.start()
            node.assert_units()
            node.assert_build()
            guest = node.create()
            node.write_marker(guest)
            node.assert_native_exec(guest)
            node.assert_files(guest)
            metadata = json.loads(node.guest(guest, '/mmds'))
            require(metadata.get('instanceID') == guest['sandboxID'], 'Docker MMDS failed')
            wait_for('real guest OTLP to MGMT listener', lambda: node.guest(guest, '/otlp') or True)
            listeners = node.execute('ip', 'netns', 'exec', 'mgmt', 'ss', '-lnpt').stdout
            require('127.0.0.1:4318' in listeners, 'OTLP listener is absent from MGMT')
            write_json(fixture.output / node.name / 'otlp.json', {'guest_post': 'passed', 'listeners': listeners})
            node.assert_outer_limits()
            for service in ('node-conductor', 'node-proxy', 'connector-vswitch'):
                token = node.state(guest)['token']
                node.execute('systemctl', 'restart', service)
                # Connector restart can stop dependent management services;
                # restart their native units without replacing the network.
                node.execute('systemctl', 'start', 'node-conductor', 'node-proxy')
                wait_for('component restart readiness', lambda: node.status()['ready'])
                wait_for('Conductor API after component restart',
                         lambda: node.api('GET', '/sandboxes/' + guest['sandboxID']))
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
            write_json(fixture.output / ('bridge-result.json' if bridge else 'host-result.json'),
                       {'build': 'passed', 'create_exec_mmds_otlp': 'passed', 'memory_disk_restore': 'passed',
                        'component_restarts': 'passed', 'normal_and_kill_restart': 'passed'})
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
        external = ExternalEgress(fixture)
        clean = False
        try:
            for hard in (False, True):
                node = DockerNode(fixture, 0, bridge=True)
                external.configure(node)
                node.start()
                guest = node.create()
                external.traffic(node, guest)
                if not hard:
                    external.input_changes(node, guest)
                node.stop(hard=hard)
                external.unchanged('forced' if hard else 'normal')
                node.remove()
            external.failure_cases(DockerNode)
            clean = True
        finally:
            if clean:
                external.close()
        cgroupfs_case(fixture)
        retained = RetainedDockerNode(fixture, 0)
        for hard in (False, True):
            retained.start()
            retained.create()
            retained.stop(hard=hard)
        retained.remove()
        require(network_snapshot() == before, 'Docker lifecycle changed host network state')
        write_json(fixture.output / 'docker-result.json', {'host': 'passed', 'bridge': 'passed', 'three_instances': 'passed'})
    finally:
        for node in fixture.nodes:
            node.diagnostics()
        for node in reversed(fixture.nodes):
            node.remove()


if __name__ == '__main__':
    run()
