"""Run the delivered Quick Start with only its Docker image and fresh named volumes."""
import base64
import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import select
import subprocess
import time
from types import SimpleNamespace
import uuid

from node_infra import command, require, wait_for, write_json
from node_infra_docker import DockerNode

DATA = '/var/lib/node-infra'
STATE = DATA + '/quickstart'
BUNDLE_REF = re.compile(r'file://[\da-f]{64}\.bundle@manifest:[\da-f]{64}@location:[A-Za-z0-9._-]+')


class QuickStartNode(DockerNode):
    """Reuse Docker execution and native admission probes, without fixture provisioning."""
    def __init__(self, fixture, image, volume, network):
        self.fixture = fixture
        self.image, self.volume, self.network = image, volume, network
        self.name = self.container = 'ks-dev-' + uuid.uuid4().hex
        self.engine_environment = os.environ.copy()
        self.identity = None
        self.failed_as_expected = False

    def __enter__(self):
        self.identity = self.docker('run', '-d', '--tty', '--pull=never', '--name', self.container,
                                    '--privileged', '--cgroupns=private', '--cpus=4',
                                    '--memory=6g', '--memory-swap=6g', '--tmpfs=/run',
                                    '--tmpfs=/run/lock', '--tmpfs=/tmp:exec', '--stop-timeout=420',
                                    '--network=' + self.network, '-v', self.volume + ':' + DATA,
                                    self.image, 'quickstart').stdout.strip()
        return self

    def __exit__(self, exc_type, exc, tb):
        path = self.fixture.output / self.name
        path.mkdir(parents=True, exist_ok=True)
        try:
            if exc or self.failed_as_expected:
                log = self.execute('journalctl', '--no-pager', check=False)
                (path / 'journal.log').write_text(log.stdout + log.stderr)
            self.docker('stop', self.identity, timeout=480)
            result = self.docker('logs', self.identity)
            (path / 'stop.log').write_text(result.stdout + result.stderr)
            if exc_type is None and not self.failed_as_expected:
                require('node-infra: network cleanup complete' in result.stdout + result.stderr,
                        'Quick Start did not report orderly network cleanup')
        finally:
            self.docker('rm', '-f', self.identity)

    def api(self, method, path, body=None, expected=200):
        # Read inside this private node so docker commands and saved diagnostics
        # never include the credential. Do not log successful Create/session JSON.
        script = ('IFS= read -r key < "$1"; shift; '
                  'printf "X-API-KEY: %s\\n" "$key" | curl -q -sS --noproxy "*" -H @- "$@"')
        args = ['-X', method, '-w', '\n%{http_code}', 'http://127.0.0.1:3000' + path]
        if body is not None:
            args += ['-H', 'Content-Type: application/json', '--data', json.dumps(body)]
        data, code = self.execute('sh', '-ec', script, 'api', STATE + '/api-key', *args,
                                  timeout=180).stdout.rsplit('\n', 1)
        require(int(code) == expected, f'{method} {path}: HTTP {code}, expected {expected}')
        return json.loads(data) if data and expected not in (204, 404) else None

    def run_guest(self, *args, expected=0):
        result = self.execute('node-infra-run', '--', *args, check=False, timeout=270)
        require(result.returncode == expected, f'Guest command returned {result.returncode}: {result.stderr[-1000:]}')
        sid = re.search(r'^node-infra-run: sandbox ([a-z0-9-]+)$', result.stderr, re.M)
        require(sid, 'one-shot command omitted its sandbox ID')
        self.wait_deleted(sid[1])
        return result

    def wait_deleted(self, sid):
        def deleted():
            self.api('GET', '/sandboxes/' + sid, expected=404)
            return True
        wait_for('Guest removal ' + sid, deleted)

    def assert_failed_init(self):
        self.failed_as_expected = True
        wait_for('Quick Start initialization failure',
                 lambda: self.execute('test', '-s', '/run/node-infra/quickstart.failed', check=False).returncode == 0)
        require(self.execute('test', '-e', '/run/node-infra/quickstart.ready', check=False).returncode != 0,
                'failed initialization published readiness')
        result = self.execute('node-infra-run', '--', '/bin/sh', '-c', 'echo launched', check=False)
        require(result.returncode != 0 and 'failed' in result.stderr.lower() and not result.stdout,
                'failed instance accepted a Guest command or omitted its failure diagnostic')

    def instance_details(self):
        keys = self.execute('sha256sum', STATE + '/manifest-key', STATE + '/api-key').stdout.splitlines()
        fingerprints = tuple(line.split()[0] for line in keys)
        template = self.execute('cat', STATE + '/template-id').stdout.strip()
        profile, kind, payload = template.split('-', 2)
        require((profile, kind) == ('bare', 'img'), 'unexpected initial template kind')
        ref = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)).decode()
        require(BUNDLE_REF.fullmatch(ref), 'initial Guest Image is not a located Manifest Bundle')
        return fingerprints, template, ref.split('://', 1)[1].split('@', 1)[0]

    def create(self, template):
        self.wait_capacity('sandbox')
        sid = self.api('POST', '/sandboxes', {'templateID': template, 'timeout': 600}, 201)['sandboxID']
        wait_for('Guest startup with ' + template.split('-', 2)[1],
                 lambda: self.api('GET', '/sandboxes/' + sid)['state'] == 'running')
        return sid

    def delete(self, sid):
        self.api('DELETE', '/sandboxes/' + sid, expected=204)
        self.wait_deleted(sid)

    def guest_shell(self, sid, script, *args):
        # Issue the native single-use session inside the node so even a Docker
        # execution timeout cannot put its token into traceback arguments.
        launch = r'''key=$(cat /var/lib/node-infra/quickstart/api-key)
sid=$1
shift
session=$(printf 'X-API-KEY: %s\n' "$key" | curl -q --noproxy '*' -fsS -H @- \
    --json '{}' "http://127.0.0.1:3000/sandboxes/$sid/exec-sessions")
token=$(printf %s "$session" | jq -er .execAccessToken)
exec sandbox-ctl exec --proxy http://127.0.0.1:3001 \
    --proxy-header "E2b-Sandbox-Id: $sid" --proxy-header 'E2b-Sandbox-Service: exec' \
    --proxy-header "X-Access-Token: $token" -- /bin/sh -ec "$@"'''
        return self.execute('sh', '-ec', launch, 'exec', sid, script, 'guest', *args).stdout

    def export(self, sid, memory):
        self.api('POST', f'/sandboxes/{sid}/pause', {'memory': memory}, 204)
        report = self.api('POST', f'/sandboxes/{sid}/export', {'toTemplate': True, 'keepSource': True})
        require(BUNDLE_REF.fullmatch(report['sandboxRef']), 'disk export lacks located Bundle selector')
        if memory:
            require(BUNDLE_REF.fullmatch(report['snapshotRef']), 'memory export lacks located Bundle selector')
            require(report['snapshotRef'] != report['sandboxRef'], 'memory and disk use the same Manifest selector')
        else:
            require(not report.get('snapshotRef'), 'disk-only export unexpectedly returned a memory root')
        root = report['snapshotRef'] if memory else report['sandboxRef']
        encoded = base64.urlsafe_b64encode(root.encode()).decode().rstrip('=')
        kind = 'snp' if memory else 'sbx'
        require(report['result'] == f'bare-{kind}-{encoded}', 'template ID differs from the published root')
        return report


def restored_state(node, template):
    sid = node.create(template)
    node.assert_outer_limits()
    marker = uuid.uuid4().hex
    # Detached ordinary Guest process carries a random value only in RAM.
    # Redirect all streams so native Exec completes when its shell exits.
    pid = node.guest_shell(sid,
        'printf %s "$1" >/ks-e2e-disk; KS_RAM="$1" sleep 600 </dev/null >/dev/null 2>&1 & printf %s "$!"',
        marker)
    require(pid.isdecimal(), 'Guest did not start the memory probe')
    probe = 'tr "\\000" "\\n" <"/proc/$1/environ"'
    require('KS_RAM=' + marker in node.guest_shell(sid, probe, pid).splitlines(),
            'random marker absent before memory capture')
    boot = str(uuid.UUID(node.guest_shell(sid, 'cat /proc/sys/kernel/random/boot_id').strip()))
    reports = {}
    for memory in (True, False):
        report = node.export(sid, memory)
        reports['S' if memory else 'E'] = {k: v for k, v in report.items() if k in ('sandboxRef', 'snapshotRef')}
        # Boot from the exported selector; retained local checkpoint files
        # would otherwise mask a broken named publication.
        node.delete(sid)
        sid = node.create(report['result'])
        require(node.guest_shell(sid, 'cat /ks-e2e-disk') == marker, 'Bundle restore lost disk writes')
        now = str(uuid.UUID(node.guest_shell(sid, 'cat /proc/sys/kernel/random/boot_id').strip()))
        if memory:
            require(now == boot and 'KS_RAM=' + marker in node.guest_shell(sid, probe, pid).splitlines(),
                    'Snapshot Bundle lost the original Guest process')
        else:
            require(now != boot, 'disk-only Bundle did not cold boot')
    node.delete(sid)
    return reports


def interrupted_run(node):
    # Signal the actual user command in the node, not the local docker client.
    script = 'echo $$ >/run/ks-e2e-run.pid; exec node-infra-run -- /bin/sh -c \'echo started; exec sleep 600\''
    with subprocess.Popen(['docker', 'exec', node.identity, 'sh', '-ec', script],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        try:
            require(select.select([proc.stdout], [], [], 120)[0]
                    and proc.stdout.readline() == 'started\n', 'temporary Guest did not start')
            rows = node.api('GET', '/v2/sandboxes')
            require(len(rows) == 1, 'expected exactly one interruptible Guest')
            node.execute('sh', '-ec', 'kill -TERM "$(cat /run/ks-e2e-run.pid)"')
            _, err = proc.communicate(timeout=90)
            require(proc.returncode != 0, f'interrupted Guest reported success: {err[-500:]}')
            node.wait_deleted(rows[0]['sandboxID'])
        finally:
            if proc.poll() is None:
                proc.kill()


@contextmanager
def fresh_volume():
    volume = command('docker', 'volume', 'create').stdout.strip()
    try:
        yield volume
    finally:
        command('docker', 'volume', 'rm', volume)


def run(image, output):
    fixture = SimpleNamespace(output=output)
    with fresh_volume() as first_volume:
        start = time.monotonic()
        # No external interface or host fixture. Only the named volume and
        # already imported image are available when the first command runs.
        with QuickStartNode(fixture, image, first_volume, 'none') as node:
            marker = uuid.uuid4().hex
            result = node.run_guest('/bin/sh', '-ec',
                'printf "%s" "$1"; printf "%s\\n" err >&2; exit 7', 'guest', marker, expected=7)
            require(result.stdout == marker and 'err' in result.stderr.splitlines(),
                    'one-shot command lost Guest stdout/stderr or changed its argv')
            cold_start = round(time.monotonic() - start, 3)
            inspected = json.loads(node.docker('inspect', node.identity).stdout)[0]
            require(inspected['Image'] == image, 'executed container differs from imported image')
            fingerprints, template, bundle = node.instance_details()
            evidence = {'image_id': image, 'architecture': node.execute('uname', '-m').stdout.strip(),
                        'cold_start_and_first_exec_seconds': cold_start,
                        'quickstart_seed_sha256': node.execute('sha256sum', '/usr/share/node-infra/quickstart/SHA256SUMS').stdout.split()[0],
                        'image_template': template, 'restored': restored_state(node, template)}
            interrupted_run(node)
            with QuickStartNode(fixture, image, first_volume, 'none') as competitor:
                competitor.assert_failed_init()
            require(node.instance_details() == (fingerprints, template, bundle),
                    'competing container changed active instance state')
        with QuickStartNode(fixture, image, first_volume, 'none') as replacement:
            result = replacement.run_guest('/bin/sh', '-c', 'printf ready')
            require(result.stdout == 'ready' and replacement.instance_details() == (fingerprints, template, bundle),
                    'container recreation lost instance credentials or template')
        with fresh_volume() as second_volume:
            with QuickStartNode(fixture, image, second_volume, 'bridge') as other:
                result = other.run_guest('/bin/sh', '-c', 'printf "hello from guest\\n"')
                other_fingerprints, other_template, other_bundle = other.instance_details()
                require(result.stdout == 'hello from guest\n', 'documented Guest command returned unexpected output')
                require(all(a != b for a, b in zip(other_fingerprints, fingerprints)) and other_template != template,
                        'independent Quick Start volumes share credentials or template root')
                physical = other.execute('find', STATE + '/publications', '-type', 'f', '-name', other_bundle).stdout.splitlines()
                require(len(physical) == 1, 'could not identify the published Guest Image')
            # Modify only our stopped, disposable second volume. Keep this
            # independent of init so it models an already damaged publication.
            command('docker', 'run', '--rm', '--pull=never', '--network=none', '--entrypoint', '/bin/sh',
                    '-v', second_volume + ':' + DATA, image, '-ec', 'printf damaged > "$1"', 'fixture', physical[0])
            with QuickStartNode(fixture, image, second_volume, 'none') as damaged:
                damaged.assert_failed_init()
                require(damaged.instance_details() == (other_fingerprints, other_template, other_bundle)
                        and damaged.execute('cat', physical[0]).stdout == 'damaged',
                        'failed initialization silently replaced durable state')
    write_json(output / 'quickstart-result.json', {**evidence, 'fresh_volumes': 2,
               'same_volume_recreate': 'passed', 'shared_volume_rejection': 'passed',
               'corrupt_bundle_rejection': 'passed', 'interrupted_guest_cleanup': 'passed'})


if __name__ == '__main__':
    run(os.environ['E2E_NODE_INFRA_DOCKER_IMAGE'], Path(os.environ['OUT']))
