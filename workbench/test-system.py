#!/usr/bin/env python3
"""Real, bounded Docker isolation/lifecycle checks. Requires the built native image."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

LAUNCHER = Path(__file__).with_name('workbench')


def run(command, timeout=180, success=True):
    result = subprocess.run(list(map(str, command)), capture_output=True, text=True, timeout=timeout)
    if (result.returncode == 0) != success:
        raise RuntimeError(f'{command!r}: exit {result.returncode}\n{result.stdout}\n{result.stderr}')
    return result.stdout.strip() if success else result.stderr.strip()



def smoke(image, root):
    """Two administrator instances and real inner containers, without requiring KVM."""
    root = Path(root).absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    names = ['admin-' + uuid.uuid4().hex[:12] + '-' + suffix for suffix in ('a', 'b')]
    result = {'image': image, 'complete': False, 'checks': [], 'cleanup': {}}

    def invoke(name, *arguments):
        return run([sys.executable, '-B', LAUNCHER, '--root', root, '--name', name, *arguments])

    # Real tools from the image form a tiny runtime fixture, without network,
    # product sources, a fake Docker executable or an extra downloaded image.
    program = r"""
set -eu
test -z "$(docker ps -aq)"
test -z "$(ip -4 route show default)"
python3 -B - <<'INNER'
import re, subprocess, tarfile
from pathlib import Path
paths = {'/bin/sh', '/usr/bin/sleep'}
for executable in sorted(paths):
    text = subprocess.check_output(['ldd', executable], text=True)
    paths.update(re.findall(r'(/[^\s()]+)', text))
archive = Path('/work/admin-smoke-root.tar')
with tarfile.open(archive, 'w', dereference=True) as out:
    for path in sorted(paths):
        if not Path(path).is_file():
            raise RuntimeError('missing dynamic dependency: ' + path)
        out.add(path, arcname=path.lstrip('/'), recursive=False)
identity = subprocess.check_output(['docker', 'import', str(archive)], text=True).strip()
subprocess.run(['docker', 'network', 'create', 'same-network'], check=True)
subprocess.run(['docker', 'run', '-d', '--pull=never', '--name', 'same-inner',
                '--network', 'same-network', '--memory=64m', '--pids-limit=32',
                identity, '/usr/bin/sleep', 'infinity'], check=True)
INNER
cp /etc/machine-id /work/identity
systemd-run --quiet --unit=same-service --property=MemoryMax=64M \
 /usr/bin/python3 -m http.server 18080 --bind 127.0.0.1 --directory /work
"""
    try:
        states = []
        for name in names:
            invoke(name, 'start', '--image', image, '--network', 'none', '--cpus', '1',
                   '--memory-gib', '2', '--timeout', '90')
            state = json.loads((root / name / 'instance.json').read_text())
            config = json.loads(run(['docker', 'inspect', state['container_id']]))[0]
            assert config['HostConfig']['Privileged'] is True
            assert config['HostConfig']['CgroupnsMode'] == 'private'
            assert not config['HostConfig'].get('CpusetCpus')
            assert not any(m['Source'] in ('/', '/var/run/docker.sock', '/run/docker.sock')
                           for m in config['Mounts'])
            states.append(state)
            invoke(name, 'exec', '--', 'bash', '-ceu', program)
        a, b = states
        assert a['preflight']['machine_id'] != b['preflight']['machine_id']
        assert all(a['preflight']['namespaces'][kind] != b['preflight']['namespaces'][kind]
                   for kind in a['preflight']['namespaces'])
        for name, state in zip(names, states):
            content = invoke(name, 'exec', '--', 'curl', '--fail', '--silent', '--retry', '10',
                             '--retry-connrefused', '--retry-delay', '1', 'http://127.0.0.1:18080/identity')
            assert content == state['preflight']['machine_id']
            assert invoke(name, 'exec', '--', 'docker', 'inspect', 'same-inner',
                          '--format', '{{.State.Running}}') == 'true'
        invoke(names[0], 'stop')
        assert invoke(names[1], 'exec', '--', 'curl', '--fail', '--silent',
                      'http://127.0.0.1:18080/identity') == b['preflight']['machine_id']
        assert invoke(names[1], 'exec', '--', 'docker', 'inspect', 'same-inner',
                      '--format', '{{.State.Running}}') == 'true'
        result.update(complete=True, checks=['private namespaces and daemons',
                      'real inner containers with identical names',
                      'same systemd unit, network name and HTTP port coexist',
                      'stopping A preserves B'])
    finally:
        for name in names:
            if (root / name / 'instance.json').exists():
                try:
                    invoke(name, 'cleanup')
                    assert json.loads((root / name / 'instance.json').read_text())['status'] == 'cleaned'
                    result['cleanup'][name] = 'owned resources removed; output retained'
                except Exception as error:
                    result['cleanup'][name] = str(error)
                    result['complete'] = False
        (root / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    assert result['complete'], 'administrator startup/cleanup failed; inspect result.json'
    return result


def exercise(args):
    root = args.root.absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    identifier = 'test-' + uuid.uuid4().hex[:12]
    names = [identifier + '-' + suffix for suffix in ('a', 'b', 'timeout', 'failed', 'foreign')]
    results = {'image': args.image, 'started_at': time.time(), 'checks': [], 'complete': False}
    derived = []
    foreign = None

    def launch(name, *command, **kwargs):
        return run([sys.executable, '-B', LAUNCHER, '--root', root, '--name', name, *command], **kwargs)

    def execute(name, script):
        return launch(name, 'exec', '--', 'bash', '-ceu', script)

    def state(name):
        return json.loads((root / name / 'instance.json').read_text())

    def protected():
        return {name: json.loads(run(['docker', 'container', 'inspect', name]))[0]
                for name in args.protect_container}

    before = protected()
    try:
        for name in names[:2]:
            launch(name, 'start', '--image', args.image, '--cpus', '2', '--memory-gib', '4',
                   '--network', 'none', '--timeout', '90')
        a, b = (state(name) for name in names[:2])
        assert a['preflight']['machine_id'] != b['preflight']['machine_id']
        assert all(a['preflight']['namespaces'][kind] != b['preflight']['namespaces'][kind]
                   for kind in a['preflight']['namespaces'])
        results['instances'] = [a, b]
        # The outer network has no external route; even a failed pull would be
        # real Docker behavior. The only image source is the verified archive.
        program = '''
test -z "$(ip -4 route show default)"
python3 -B - <<'PY'
import ctypes,json,os,platform,struct,subprocess
from pathlib import Path
r=next(r for r in json.loads(Path('/opt/workbench/deps/images.json').read_text()) if r['reference']=='python:3.12-slim')
subprocess.run(['docker','load','-i',str(Path('/opt/workbench/deps')/r['archive'])],check=True)
Path('/work/shared-test').mkdir()
Path('/work/shared-test/identity').write_text(Path('/etc/machine-id').read_text())
subprocess.run(['docker','run','-d','--name','same-inner','--network','host','--read-only',
 '--cap-drop=ALL','--security-opt=no-new-privileges','--mount','type=bind,src=/work/shared-test,dst=/content,readonly',
 r['image_id'],'python3','-m','http.server','18080','--directory','/content'],check=True)
libc=ctypes.CDLL(None,use_errno=True)
nr=321 if platform.machine()=='x86_64' else 280
attr=ctypes.create_string_buffer(struct.pack('IIII',2,4,8,1)+bytes(128))
fd=libc.syscall(nr,0,ctypes.byref(attr),len(attr))
assert fd>=0,os.strerror(ctypes.get_errno())
pin=ctypes.create_string_buffer(b'/sys/fs/bpf/same-pin')
attr=ctypes.create_string_buffer(struct.pack('QII',ctypes.addressof(pin),fd,0))
assert libc.syscall(nr,6,ctypes.byref(attr),16)==0,os.strerror(ctypes.get_errno())
os.close(fd)
PY
ip netns add same-netns
ip -n same-netns link set lo up
ip link add same-link type dummy
curl --fail --silent --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:18080/identity
'''
        for name in names[:2]:
            response = execute(name, program)
            assert response.endswith(state(name)['preflight']['machine_id'])
        results['checks'].append('distinct namespaces, machine IDs, daemons; identical inner names, port, netns, BPF pin and link')
        health = '''
test -e /sys/fs/bpf/same-pin
ip -n same-netns link show lo
ip link show same-link
test "$(docker inspect same-inner --format '{{.State.Running}}')" = true
curl --fail --silent http://127.0.0.1:18080/identity
'''
        execute(names[0], 'systemctl stop docker.service docker.socket')
        assert execute(names[1], health).endswith(b['preflight']['machine_id'])
        launch(names[0], 'stop')
        assert execute(names[1], health).endswith(b['preflight']['machine_id'])
        results['checks'].append('A daemon failure and graceful stop leave B running')
        launch(names[0], 'start', '--image', args.image, '--timeout', '90')
        assert state(names[0])['preflight']['machine_id'] == a['preflight']['machine_id']
        execute(names[0], "docker start same-inner; test -f /work/shared-test/identity; test ! -e /sys/fs/bpf/same-pin")
        results['checks'].append('restart retains only owned disk state, resets bpffs, restores private daemons')
        # Negative startup images deliberately delay or fail PID 1. They use
        # real Docker; no fake executable is substituted for system acceptance.
        delay = json.dumps(['python3', '-c', 'import signal,time; signal.signal(signal.SIGRTMIN+3,lambda *_:exit(0)); time.sleep(30)'])
        for name, entrypoint, timeout in ((names[2], delay, '1'),
                                          (names[3], '["sh","-c","exit 42"]', '10')):
            context = root / (name + '-context')
            context.mkdir()
            (context / 'Dockerfile').write_text(f'FROM {args.image}\nENTRYPOINT {entrypoint}\nCMD []\n')
            tag = 'workbench-lifecycle:' + name
            run(['docker', 'build', '--pull=false', '--memory=1g', '--cpu-period=100000', '--cpu-quota=100000',
                 '--label', 'org.kuasar.workbench.test=' + identifier, '-t', tag, context])
            derived.append(tag)
            launch(name, 'start', '--image', tag, '--cpus', '1', '--memory-gib', '1', '--network', 'none',
                   '--timeout', timeout, success=False)
            assert state(name)['status'] == 'failed-start'
            assert list((root / name / 'output').glob('startup-*.log'))
        results['checks'].append('bounded timeout and exited PID 1 retain diagnostics and ownership')
        foreign = run(['docker', 'create', '--pull=never', '--name', 'kuasar-workbench-' + names[4],
                       '--network=none', '--cap-drop=ALL', '--entrypoint', 'sleep', args.image, 'infinity'])
        assert 'foreign container name collision' in launch(names[4], 'start', '--image', args.image, success=False)
        assert json.loads(run(['docker', 'inspect', foreign]))[0]['Id'] == foreign
        results['checks'].append('foreign container name is rejected without adoption or deletion')
        after = protected()
        for name in before:
            assert before[name]['Id'] == after[name]['Id']
            assert before[name]['State']['StartedAt'] == after[name]['State']['StartedAt']
            assert before[name]['State']['Running'] == after[name]['State']['Running']
        results['protected_containers'] = {name: entry['Id'] for name, entry in after.items()}
        results['complete'] = True
    finally:
        results['wall_seconds'] = time.time() - results['started_at']
        results['cleanup'] = {}
        for name in names[:4]:
            if (root / name / 'instance.json').exists():
                try:
                    launch(name, 'cleanup', timeout=420)
                    results['cleanup'][name] = 'owned resources removed; output retained'
                    assert state(name)['status'] == 'cleaned'
                except Exception as error:
                    results['cleanup'][name] = str(error)
                    results['complete'] = False
        if foreign:
            run(['docker', 'rm', foreign])
        for tag in derived:
            run(['docker', 'image', 'rm', tag])
        (root / 'result.json').write_text(json.dumps(results, sort_keys=True, indent=2) + '\n')
    assert results['complete'], 'lifecycle cleanup failed; inspect result.json'
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--root', required=True, type=Path, help='new task directory; existing paths are refused')
    parser.add_argument('--protect-container', action='append', default=[])
    parser.add_argument('--smoke', action='store_true', help='test generic system startup and inner Docker without test-image inputs or KVM')
    args = parser.parse_args()
    print(json.dumps(smoke(args.image, args.root) if args.smoke else exercise(args), sort_keys=True))
