#!/usr/bin/env python3
"""Requirements, allocation and complete-result boundary regressions."""
import copy
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest.mock import Mock, patch

import artifacts
import capability_routing as routing
from test_fixtures import CASES, helper_contract

capabilities = artifacts.capabilities_module()


def fixture():
    helper = helper_contract(sha='d' * 40, framework='d' * 40)
    plan = {'schema': 3, 'mode': 'source', 'framework_sha': 'a' * 40, 'baseline': {},
            'case_files': copy.deepcopy(CASES), 'owners': ['platform'],
            'test_overlays': list(artifacts.OWNERS),
            'test_revisions': artifacts.release_test_revisions({owner: 'd' * 40 for owner in artifacts.OWNERS if owner != 'platform'}, 'd' * 40),
            'helper_contract': helper,
            'run_inputs': {owner: copy.deepcopy(row['source']) for owner, row in helper['owners'].items()},
            'lanes': {arch: {'products': [], 'performance': ['working-set-smoke'] if arch == 'x86_64' else [],
                            'selection': artifacts.suite_selection(['platform'], arch, CASES)} for arch in artifacts.ARCHES}}
    contract = {'schema': 1, 'cases': {name: {'sha256': 'b' * 64, 'requires': []}
                                     for names in CASES.values() for name in names}}
    contract['cases']['basic.demo.sh']['requires'] = ['kvm']
    config = routing.read_config()
    with patch.object(routing, 'read_config', return_value=config):
        routing.apply(plan, contract)
    plan['execution_run'] = {'repository': 'kuasar-sandbox/kuasar-sandbox', 'run_id': '1', 'run_attempt': '2'}
    plan['execution_images'] = {arch: {'reference': 'ghcr.io/kuasar-sandbox/workbench@sha256:' + 'c' * 64,
                                      'image_id': 'sha256:' + 'e' * 64} for arch in artifacts.ARCHES}
    return plan


def result_set(plan, arch):
    import sys
    sys.path.insert(0, str(artifacts.ROOT / 'test/e2e/lib'))
    import capability_probe
    results = {}
    for name, group in plan['lanes'][arch]['groups'].items():
        profile = plan['runner_configuration']['value']['profiles'][group['profile']]
        runner = dict(plan['execution_run'], job='e2e', name='fixture-runner', os='Linux',
                      arch='ARM64' if arch == 'aarch64' else 'X64',
                      environment='github-hosted' if profile['public'] else 'self-hosted')
        clean = {'kind': 'clean-container', 'verified': True, 'compilers': [], 'component_source_trees': [],
                 'image_id': 'sha256:' + 'f' * 64}
        receipt = {'profile': group['profile'], 'executor': profile['executor'], 'runner': runner,
                   'configuration_sha256': plan['runner_configuration']['sha256']}
        if profile['executor'] == 'workbench-system':
            receipt['image_id'] = plan['execution_images'][arch]['image_id']
        results[name] = {'arch': arch, 'plan_id': artifacts.identity(plan), 'shard': name, 'cases': group['cases'],
                         'conclusion': 'success', 'execution': receipt,
                         'capabilities': {cap: capability_probe.OPERATIONS[cap] for cap in group['requires']},
                         'environment': clean if group['runtime'] == 'runtime-only' else {'kind': 'native-host'},
                         'preparation_environment': clean, 'test_revisions': plan['test_revisions'],
                         'provenance_sha256': 'd' * 64,
                         'timings': [{'case': case, 'exit_code': 0, 'wall_seconds': 1} for case in group['cases']]}
    return results


class Requirements(unittest.TestCase):
    def test_static_header(self):
        self.assertEqual(capabilities.parse(b'#!/bin/bash\n# e2e-requires: ["tun","kvm"]\nexit 0\n'), ['kvm', 'tun'])
        self.assertEqual(capabilities.parse(b'# e2e-requires: []\n'), [])
        self.assertIsNone(capabilities.parse(b'#!/bin/bash\nexit 0\n', required=False))

    def test_invalid_or_missing_is_never_empty(self):
        for text in ('', '# e2e-requires: ["kvm","kvm"]', '# e2e-requires: ["typo"]',
                     '# e2e-requires: {}', '# e2e-requires: [1]', '# e2e-requires: $(touch /tmp/never)',
                     '# e2e-requires: []\n# e2e-requires: []', 'true\n# e2e-requires: []',
                     ' # e2e-requires: []', '# e2e-requires: ["kvm",]'):
            with self.subTest(text=text), self.assertRaises((ValueError, json.JSONDecodeError)):
                capabilities.parse(text.encode())

    def test_owner_generation_and_auto_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = {owner: Path(directory) / owner for owner in artifacts.OWNERS}
            files = {owner: ['basic.' + owner + '.sh'] for owner in roots}
            for owner, root in roots.items():
                e2e = root / ('test/e2e/platform' if owner == 'platform' else 'test/e2e')
                (e2e / 'cases').mkdir(parents=True)
                (e2e / 'lib').mkdir()
                (e2e / 'cases' / files[owner][0]).write_text('#!/bin/bash\n')
            self.assertIsNone(capabilities.discover(roots, files))
            for owner, root in roots.items():
                e2e = root / ('test/e2e/platform' if owner == 'platform' else 'test/e2e')
                (e2e / 'lib/requirements.json').write_text('{"schema":1}')
                (e2e / 'cases' / files[owner][0]).write_text('#!/bin/bash\n# e2e-requires: []\n')
                if owner != 'platform':
                    with self.assertRaisesRegex(ValueError, 'mixed'):
                        capabilities.discover(roots, files)
            initial = capabilities.discover(roots, files)
            target = roots['connector'] / 'test/e2e/cases/network.new.sh'
            target.write_text('#!/bin/bash\n# e2e-requires: ["tun"]\n')
            files['connector'].append(target.name)
            changed = capabilities.discover(roots, files)
            self.assertEqual(changed['cases'][target.name]['requires'], ['tun'])
            target.write_text('#!/bin/bash\n# e2e-requires: ["kvm"]\n')
            self.assertNotEqual(changed, capabilities.discover(roots, files))
            target.unlink(); files['connector'].remove(target.name)
            self.assertEqual(initial, capabilities.discover(roots, files))
            (roots['connector'] / 'test/e2e/cases' / files['connector'][0]).write_text('#!/bin/bash\n')
            with self.assertRaisesRegex(ValueError, 'missing'):
                capabilities.discover(roots, files)


class Allocation(unittest.TestCase):
    def test_dependency_coverage_survives_routing_and_cannot_be_dropped(self):
        for owner, path, case in (
                ('accelerator', 'pkg/tarstream/write.go', 'telemetry.usage.sh'),
                ('sandboxer', 'pkg/diffstore/single_format.go', 'orchestrator.resource-control.sh')):
            with self.subTest(owner=owner):
                plan = fixture()
                plan['owners'] = [owner]
                plan['changes'] = {owner: [path]}
                test_owner = 'sandboxer' if case.startswith('telemetry.') else 'orchestrator'
                plan['case_files'][test_owner] = sorted(plan['case_files'][test_owner] + [case])
                contract = copy.deepcopy(plan['case_requirements'])
                contract['cases'][case] = {'sha256': 'c' * 64, 'requires': ['kvm']}
                routing.apply(plan, contract)
                routing.validate(plan)
                for arch in artifacts.ARCHES:
                    self.assertIn(case, plan['lanes'][arch]['selection']['cases'])
                    groups = plan['lanes'][arch]['groups'].values()
                    self.assertEqual(sum(case in group['cases'] for group in groups), 1)
                forged = copy.deepcopy(plan)
                forged['lanes']['aarch64']['selection']['cases'].remove(case)
                with self.assertRaisesRegex(ValueError, 'required coverage'):
                    routing.validate(forged)

    def test_public_first_with_exact_disjoint_union(self):
        plan = fixture()
        artifacts.check_plan(plan)
        for arch, lane in plan['lanes'].items():
            cases = [case for group in lane['groups'].values() for case in group['cases']]
            self.assertEqual(len(cases), len(set(cases)))
            self.assertEqual(sorted(cases), lane['selection']['cases'])
        arm = plan['lanes']['aarch64']['groups']
        self.assertEqual(arm['supplemental-arm--basic']['cases'], ['basic.demo.sh'])
        self.assertEqual(arm['supplemental-arm--basic']['public_gaps'],
                         {'basic.demo.sh': {'hosted-arm': ['kvm']}})

    def test_public_capability_update_moves_cases_without_owner_changes(self):
        plan = fixture()
        config = plan['runner_configuration']['value']
        config['profiles']['hosted-arm']['capabilities']['kvm']['state'] = 'supported'
        groups = routing.allocate('aarch64', plan['lanes']['aarch64']['selection'], plan['case_requirements'], config)
        self.assertFalse(any(group['profile'] == 'supplemental-arm' for group in groups.values()))

    def test_unknown_cannot_satisfy_or_justify_supplemental(self):
        plan = fixture()
        config = plan['runner_configuration']['value']
        config['profiles']['hosted-arm']['capabilities']['kvm']['state'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            routing.allocate('aarch64', plan['lanes']['aarch64']['selection'], plan['case_requirements'], config)
        config['profiles']['supplemental-arm']['enabled'] = False
        with self.assertRaisesRegex(ValueError, 'no capable'):
            routing.allocate('aarch64', plan['lanes']['aarch64']['selection'], plan['case_requirements'], config)

    def test_disabled_and_unknown_supplemental_fail(self):
        for disabled in (True, False):
            plan = fixture(); config = plan['runner_configuration']['value']
            if disabled:
                config['profiles']['supplemental-arm']['enabled'] = False
            else:
                config['profiles']['supplemental-arm']['capabilities']['kvm']['state'] = 'unknown'
            with self.assertRaisesRegex(ValueError, 'no capable'):
                routing.allocate('aarch64', plan['lanes']['aarch64']['selection'], plan['case_requirements'], config)

    def test_same_priority_is_deterministic_and_empty_does_not_schedule(self):
        plan = fixture(); config = plan['runner_configuration']['value']
        config['profiles']['another-arm'] = copy.deepcopy(config['profiles']['supplemental-arm'])
        groups = routing.allocate('aarch64', plan['lanes']['aarch64']['selection'], plan['case_requirements'], config)
        self.assertIn('another-arm--basic', groups)
        self.assertEqual(routing.allocate('aarch64', {'cases': []}, plan['case_requirements'], config), {})

    def test_tampered_inventory_profile_or_group_is_rejected(self):
        for mutation in (lambda p: p['lanes']['aarch64']['groups'].pop('supplemental-arm--basic'),
                         lambda p: p['case_requirements']['cases'].pop('basic.demo.sh'),
                         lambda p: p['runner_configuration']['value']['profiles']['hosted-arm'].update(runs_on=['self-hosted']),
                         lambda p: p['lanes']['aarch64']['groups']['supplemental-arm--basic']['cases'].append('basic.fake.sh')):
            plan = fixture(); mutation(plan)
            with self.assertRaises(ValueError):
                artifacts.check_plan(plan)

    def test_partial_rerun_carries_only_the_same_plan_and_no_future_receipt(self):
        plan = fixture(); results = result_set(plan, 'aarch64')
        results['supplemental-arm--basic']['execution']['runner']['run_attempt'] = '3'
        environment = {'GITHUB_REPOSITORY': plan['execution_run']['repository'],
                       'GITHUB_RUN_ID': '1', 'GITHUB_RUN_ATTEMPT': '3'}
        with patch.dict('os.environ', environment):
            artifacts.collect_shard_results(plan, 'aarch64', results)
            results['supplemental-arm--basic']['execution']['runner']['run_attempt'] = '4'
            with self.assertRaisesRegex(ValueError, 'future'):
                artifacts.collect_shard_results(plan, 'aarch64', results)

    def test_compact_dual_architecture_evidence_remains_lossless(self):
        plan = fixture()
        records = {}
        for arch in artifacts.ARCHES:
            shards = result_set(plan, arch)
            complete = dict(shards)
            if plan['lanes'][arch]['performance']:
                complete['performance'] = {'arch': arch, 'plan_id': artifacts.identity(plan),
                    'test_revisions': plan['test_revisions'], 'conclusion': 'success',
                    'checks': ['working-set-smoke'], 'provenance_sha256': 'd' * 64,
                    'timings': [{'case': 'working-set-smoke', 'exit_code': 0, 'wall_seconds': 1}]}
            records[arch] = artifacts.collect_shard_results(plan, arch, complete)
            self.assertEqual(artifacts.expand_shards(plan, arch, records[arch]), shards)
        accepted = artifacts.collect_results(plan, records)
        self.assertEqual(accepted['architectures'], records)
        changed = copy.deepcopy(records)
        changed['aarch64']['shards']['supplemental-arm--basic']['timings'][0]['exit_code'] = False
        with self.assertRaises(ValueError):
            artifacts.collect_results(plan, changed)

    def test_final_gate_requires_every_group_and_identity(self):
        plan = fixture(); results = result_set(plan, 'aarch64')
        compact = artifacts.collect_shard_results(plan, 'aarch64', results)
        self.assertEqual(artifacts.expand_shards(plan, 'aarch64', compact), results)
        changed = copy.deepcopy(compact)
        changed['shards']['supplemental-arm--basic']['plan_id'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'overrides'):
            artifacts.expand_shards(plan, 'aarch64', changed)
        key = 'supplemental-arm--basic'
        mutations = [lambda r: r.pop(key), lambda r: r[key].update(conclusion='cancelled'),
                     lambda r: r[key].update(conclusion='failure'), lambda r: r[key].update(capabilities={}),
                     lambda r: r[key]['execution'].update(image_id='sha256:' + '0' * 64),
                     lambda r: r[key]['execution']['runner'].update(run_attempt='1'),
                     lambda r: r[key].update(provenance_sha256='0' * 64),
                     lambda r: r[key].update(cases=[])]
        for mutation in mutations:
            changed = copy.deepcopy(results); mutation(changed)
            with self.assertRaises(ValueError):
                artifacts.collect_shard_results(plan, 'aarch64', changed)


class RuntimeTransport(unittest.TestCase):
    def test_changed_runtime_is_rejected_before_docker(self):
        import runtime_archive
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'runtime.tar.gz'
            archive.write_bytes(b'changed archive')
            provenance = {'arch': 'aarch64', 'preparation_environment': {
                'kind': 'clean-container', 'verified': True, 'image_id': 'sha256:' + 'f' * 64,
                'archive_sha256': '0' * 64, 'compilers': [], 'component_source_trees': []}}
            with patch.object(runtime_archive.subprocess, 'run') as docker:
                with self.assertRaisesRegex(ValueError, 'archive identity'):
                    runtime_archive.load(archive, provenance)
                docker.assert_not_called()

    def test_host_collection_rejects_output_links_and_special_files(self):
        import importlib.util
        import os
        spec = importlib.util.spec_from_file_location('group_driver', Path(__file__).with_name('run-capability-group.py'))
        driver = importlib.util.module_from_spec(spec); spec.loader.exec_module(driver)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'private'; target.write_text('never collect')
            link = root / 'result.json'; link.symlink_to(target)
            with self.assertRaises(OSError):
                driver.read_owned(link)
            link.unlink(); os.mkfifo(link)
            with self.assertRaisesRegex(ValueError, 'unsafe'):
                driver.read_owned(link)
            link.unlink(); link.write_bytes(b'x' * 11)
            with self.assertRaisesRegex(ValueError, 'excessive'):
                driver.read_owned(link, limit=10)

    def test_workbench_names_are_stable_and_unique_per_task_directory(self):
        spec = importlib.util.spec_from_file_location('group_names', Path(__file__).with_name('run-capability-group.py'))
        driver = importlib.util.module_from_spec(spec); spec.loader.exec_module(driver)
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / 'runner-one/group', Path(directory) / 'runner-two/group'
            self.assertNotEqual(driver.instance(first).name, driver.instance(second).name)
            self.assertEqual(driver.instance(first), driver.instance(first))
            for operation in ('start', 'exec', 'cleanup'):
                command = driver.command(first, operation)
                self.assertEqual(command[command.index('--name') + 1], driver.instance(first).name)

    def test_start_case_and_cancellation_failures_cleanup_without_success(self):
        spec = importlib.util.spec_from_file_location('group_lifecycle', Path(__file__).with_name('run-capability-group.py'))
        driver = importlib.util.module_from_spec(spec); spec.loader.exec_module(driver)
        for phase, error, conclusion in (
                ('start', subprocess.CalledProcessError(1, ['start']), 'failure'),
                ('exec', subprocess.CalledProcessError(23, ['exec']), 'failure'),
                ('exec', InterruptedError('cancelled'), 'cancelled')):
            with self.subTest(phase=phase, error=error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); plan = fixture()
                args = argparse.Namespace(plan=root / 'plan.json', workspace=root / 'prepared', arch='aarch64',
                    group='supplemental-arm--basic', runtime=root / 'runtime.tar.gz', result=root / 'result.json',
                    readers=root / 'readers', inputs=root / 'inputs', root=root / 'state')
                args.plan.write_text(json.dumps(plan))
                def invoke(command, **options):
                    if phase in command:
                        raise error
                    return subprocess.CompletedProcess(command, 0)
                with patch.object(driver.artifacts, 'verify_workspace', return_value={}), \
                     patch.object(driver.platform, 'machine', return_value='aarch64'), \
                     patch.object(driver.routing, 'runner_identity', return_value={}), \
                     patch.object(driver.shutil, 'copytree'), patch.object(driver.shutil, 'copy2'), \
                     patch.object(driver.subprocess, 'run', side_effect=invoke), patch.object(driver, 'cleanup') as clean, \
                     patch.object(driver, 'capture_startup_daemons', side_effect=OSError('private diagnostic error')) as diagnostic:
                    with self.assertRaises(type(error)):
                        driver.execute(args)
                    clean.assert_called_once_with(args.root)
                result = json.loads(args.result.read_text())
                self.assertEqual(result['conclusion'], conclusion)
                self.assertEqual(result['plan_id'], artifacts.identity(plan))
                if conclusion == 'cancelled':
                    diagnostic.assert_not_called()
                else:
                    diagnostic.assert_called_once_with(args.root)
                    self.assertEqual(result['startup_diagnostic_error'], 'OSError')
                    self.assertNotIn('private diagnostic error', json.dumps(result))


class StartupDiagnostics(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('group_diagnostics', Path(__file__).with_name('run-capability-group.py'))
        self.driver = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.driver)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.driver.instance(self.root)
        self.state.mkdir()
        (self.state / 'instance.json').write_text('{}')
        self.launcher = Mock()
        self.launcher.record.return_value = {'status': 'failed-start', 'mode': 'system'}
        self.launcher.owned_container.return_value = {'Id': 'owned-id', 'State': {'Running': True}}

    def test_failed_start_queries_only_owned_private_daemons_and_redacts_output(self):
        def run(command, **kwargs):
            self.assertEqual(command[:3], ['docker', 'exec', 'owned-id'])
            self.assertEqual(kwargs['timeout'], 5)
            self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
            if command[3] == 'systemctl':
                self.assertEqual(command[4:7], ['show', 'docker.service', 'containerd.service'])
                self.assertNotIn('--property=Environment', command)
                text = 'Id=docker.service\nMainPID=23\nActiveState=active\nEnvironment=private-secret\n'
            else:
                self.assertEqual(command[3], 'journalctl')
                self.assertIn('--unit=docker.service', command)
                self.assertIn('--unit=containerd.service', command)
                self.assertIn('--lines=200', command)
                self.assertNotIn('--all', command)
                text = json.dumps({'MESSAGE': 'failed to dial private-secret context deadline exceeded',
                    '_SYSTEMD_UNIT': 'docker.service', '_PID': '23', '__REALTIME_TIMESTAMP': '12345'}) + '\n'
            kwargs['stdout'].write(text.encode())
            return subprocess.CompletedProcess(command, 0)
        with patch.object(self.driver, 'workbench_launcher', return_value=self.launcher), \
                patch.object(self.driver.subprocess, 'run', side_effect=run) as calls:
            self.driver.capture_startup_daemons(self.root)
        self.assertEqual(calls.call_count, 2)
        self.launcher.record.assert_called_once_with(self.state)
        self.launcher.owned_container.assert_called_once_with(self.launcher.record.return_value)
        path, = (self.state / 'output').glob('startup-daemons-*.log')
        evidence = json.loads(path.read_text())
        self.assertEqual(evidence['units']['rows'], [{'Id': 'docker.service'}, {'MainPID': '23'}, {'ActiveState': 'active'}])
        self.assertEqual(evidence['journal']['rows'][0]['terms'], ['context deadline exceeded', 'failed to dial'])
        self.assertEqual(evidence['journal']['rows'][0]['unit'], 'docker.service')
        self.assertNotIn('private-secret', path.read_text())
        destination = self.root / 'retained'
        self.driver.diagnostics(self.root, destination)
        self.assertEqual((destination / path.name).read_bytes(), path.read_bytes())

    def test_timeout_is_recorded_without_retry_or_secret_error_contents(self):
        with patch.object(self.driver.subprocess, 'run', side_effect=subprocess.TimeoutExpired('private-secret', 5)) as run:
            evidence = self.driver.startup_daemons('owned-id')
        self.assertEqual(run.call_count, 2)
        self.assertEqual(evidence['units'], {'error': 'TimeoutExpired'})
        self.assertEqual(evidence['journal'], {'error': 'TimeoutExpired'})
        self.assertNotIn('private-secret', json.dumps(evidence))

    def test_success_build_stopped_and_foreign_containers_cannot_be_queried(self):
        for data in ({'mode': 'system', 'status': 'running'}, {'mode': 'build', 'status': 'failed-start'}):
            self.launcher.record.return_value = data
            with patch.object(self.driver, 'workbench_launcher', return_value=self.launcher), \
                    patch.object(self.driver, 'startup_daemons') as query:
                self.driver.capture_startup_daemons(self.root)
            query.assert_not_called()
        self.launcher.record.return_value = {'status': 'failed-start', 'mode': 'system'}
        self.launcher.owned_container.return_value = {'Id': 'owned-id', 'State': {'Running': False}}
        with patch.object(self.driver, 'workbench_launcher', return_value=self.launcher), \
                patch.object(self.driver, 'startup_daemons') as query:
            self.driver.capture_startup_daemons(self.root)
        query.assert_not_called()
        self.launcher.owned_container.side_effect = ValueError('foreign container')
        with patch.object(self.driver, 'workbench_launcher', return_value=self.launcher), \
                patch.object(self.driver, 'startup_daemons') as query:
            with self.assertRaisesRegex(ValueError, 'foreign'):
                self.driver.capture_startup_daemons(self.root)
        query.assert_not_called()

    def test_diagnostic_output_cannot_escape_instance(self):
        outside = self.root / 'unrelated'; outside.mkdir()
        (self.state / 'output').symlink_to(outside, target_is_directory=True)
        with patch.object(self.driver, 'workbench_launcher', return_value=self.launcher), \
                patch.object(self.driver, 'startup_daemons', return_value={}):
            with self.assertRaisesRegex(ValueError, 'escaping'):
                self.driver.capture_startup_daemons(self.root)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
