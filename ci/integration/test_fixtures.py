"""Small complete source sets for integration contract regressions."""
import artifacts

CASES = {'accelerator': ['image.manifest.sh', 'storage.cache.sh', 'storage.obs.sh'],
         'connector': ['network.tap.sh'], 'guest-runtime': ['image.guest-fixture.sh'],
         'sandboxer': ['sandbox.lifecycle.sh'], 'orchestrator': ['orchestrator.exec.sh'],
         'platform': ['basic.demo.sh']}


def selection(owners, arch, updates=None):
    return artifacts.suite_selection(owners, arch, CASES | (updates or {}))


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
