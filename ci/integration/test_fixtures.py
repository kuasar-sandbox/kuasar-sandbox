"""Small complete source sets for integration contract regressions."""
import artifacts
import hashlib
import json
import zipfile

CASES = {'accelerator': ['image.manifest.sh', 'storage.cache.sh', 'storage.obs.sh'],
         'connector': ['network.tap.sh'], 'guest-runtime': ['image.guest-fixture.sh'],
         'sandboxer': ['sandbox.lifecycle.sh'], 'orchestrator': ['orchestrator.exec.sh'],
         'platform': ['basic.demo.sh']}


def selection(owners, arch, updates=None):
    return artifacts.suite_selection(owners, arch, CASES | (updates or {}))


def make_demo_wheelhouse(demo, output, architectures=('x86_64', 'aarch64')):
    """A real offline wheel closure with one required transitive dependency."""
    demo.mkdir(parents=True, exist_ok=True)
    requirements = b'e2b==2.25.1\n'
    (demo / 'requirements.txt').write_bytes(requirements)
    for arch in architectures:
        directory = output / arch
        directory.mkdir(parents=True)
        records = {}
        for name, version in [('e2b', '2.25.1'), ('prepared-dependency', '1.0')]:
            module = name.replace('-', '_')
            filename = f'{module}-{version}-py3-none-any.whl'
            with zipfile.ZipFile(directory / filename, 'w') as wheel:
                wheel.writestr(module + '/__init__.py',
                               'import prepared_dependency\n' if name == 'e2b' else 'value = "prepared"\n')
                metadata = f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n'
                if name == 'e2b': metadata += 'Requires-Dist: prepared-dependency==1.0\n'
                wheel.writestr(f'{module}-{version}.dist-info/METADATA', metadata)
                wheel.writestr(f'{module}-{version}.dist-info/WHEEL',
                               'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
                wheel.writestr(f'{module}-{version}.dist-info/RECORD', '')
            records[filename] = {'package': name, 'version': version,
                                 'sha256': hashlib.sha256((directory / filename).read_bytes()).hexdigest()}
        # Copy identical wheels to each architecture so their lock is shared.
        if arch == architectures[0]:
            lock = ''.join(f"{r['package']}=={r['version']} --hash=sha256:{r['sha256']}\n" for r in records.values()).encode()
            (demo / 'requirements.lock').write_bytes(lock)
        else:
            for name in records:
                (directory / name).write_bytes((output / architectures[0] / name).read_bytes())
                records[name]['sha256'] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        manifest = {'arch': arch, 'python': '3.12', 'wheels': records,
                    'lock_sha256': hashlib.sha256(lock).hexdigest(),
                    'requirements_sha256': hashlib.sha256(requirements).hexdigest()}
        (directory / 'manifest.json').write_text(json.dumps(manifest))


def select_plan(plan, owners):
    plan['owners'] = owners
    for arch in artifacts.ARCHES:
        plan['lanes'][arch]['selection'] = selection(owners, arch)
        plan['lanes'][arch]['performance'] = ['working-set-smoke'] if (
            plan['mode'] == 'source' and arch == 'x86_64' and set(owners) & {'platform', 'sandboxer'}) else []


def architecture_result(plan, arch):
    records = {}
    for shard, cases in artifacts.shards(plan['lanes'][arch]['selection']).items():
        records[shard] = {'arch': arch, 'shard': shard, 'plan_id': artifacts.identity(plan),
                         'test_revisions': plan['test_revisions'], 'conclusion': 'success',
                         'provenance_sha256': 'a' * 64, 'cases': cases,
                         'timings': [{'case': case, 'exit_code': 0, 'wall_seconds': 0.1} for case in cases]}
    for shard, record in records.items():
        if record['cases']:
            record['preparation_environment'] = {'kind': 'clean-container', 'verified': True,
                                                'compilers': [], 'component_source_trees': [], 'image_id': 'sha256:' + 'd' * 64}
        if artifacts.requires_clean_runtime(arch, shard):
            record['environment'] = {'kind': 'clean-container', 'verified': True,
                                     'compilers': [], 'component_source_trees': [], 'image_id': 'sha256:' + 'e' * 64}
    checks = plan['lanes'][arch]['performance']
    if checks:
        records['performance'] = {'arch': arch, 'plan_id': artifacts.identity(plan),
                                  'test_revisions': plan['test_revisions'], 'conclusion': 'success',
                                  'provenance_sha256': 'a' * 64, 'checks': checks,
                                  'timings': [{'case': case, 'exit_code': 0, 'wall_seconds': 0.1} for case in checks]}
    return artifacts.collect_shard_results(plan, arch, records)

# Non-executable result fixtures for binding/reader unit tests. Runtime
# acceptance always comes from the real staged workbench test, not this helper.
WORKBENCH_CASES = CASES | {'sandboxer': sorted([*CASES['sandboxer'], 'network.tapfd.sh', 'snapshot.restore.sh'])}


def workbench_results(plan, receipts):
    result = {}
    for arch, receipt in receipts.items():
        cases = artifacts.workbench_cases(plan['case_files'], arch)
        result[arch] = dict(receipt, conclusion='success', plan_id=artifacts.identity(plan),
            qualification_scope='system', imported_image_id=receipt['image_id'], release_inputs_verified=True,
            framework_sha=plan['framework_sha'], test_revisions=plan['test_revisions'],
            offline=True, empty_private_daemon=True, preparation_network='none', execution_network='owned-bridge', isolation={'complete': True, 'image': receipt['image_id']},
            cases=cases, timings=[{'case': name, 'exit_code': 0, 'wall_seconds': .1} for name in cases],
            input_assets={row['name']: row['digest'] for row in plan['baseline']['assets'] if row['name'] != 'SHA256SUMS'},
            preflight={'arch': arch, 'page_size': 4096, 'kvm': 'api-12', 'tun': 'create-close',
                'uffd': 'api-ioctl', 'bpf': 'create-pin-remove',
                'docker': {'driver': 'overlay2', 'root': '/var/lib/docker'},
                'containerd': {'root': '/var/lib/containerd', 'state': '/run/containerd'}},
            host_apparmor_enabled=False, provenance_sha256='a' * 64, compression_seconds=.1, import_seconds=.1, start_seconds=.1,
            wall_seconds=1, retained_task_bytes=100, disk={'used_before': 100, 'peak_used': 200, 'peak_increase': 100})
    return result


def registry_binding(version, results):
    return {'reference': 'ghcr.io/kuasar-sandbox/workbench:' + version.removeprefix('release-'),
            'digest': 'sha256:' + 'f' * 64, 'architectures': {
                arch: {'digest': 'sha256:' + str(index) * 64, 'size': 100,
                       'image_id': row['image_id'], 'archive_sha256': row['sha256']}
                for index, (arch, row) in enumerate(results.items(), 1)}}
