"""Fail-closed stage/validation binding tests; these do not execute images."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci/integration'))
import artifacts
from test_fixtures import WORKBENCH_CASES, architecture_result, workbench_results, registry_binding
spec = importlib.util.spec_from_file_location('workbench_bind_validation', ROOT / 'release/bind-validation.py')
binder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(binder)


class BindingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'assets').mkdir()
        (self.root / 'workbench').mkdir()
        self.version, self.sha = 'release-v1.2.3', 'b' * 40
        units = {unit: {'version': (unit + '-' if unit in ('runtime', 'vmlinux') else '') + 'v1.2.3',
                        'sha': 'c' * 40} for unit in artifacts.UNITS}
        names = ['SHA256SUMS', 'platform-' + self.version + '.tar.gz']
        names += [artifacts.archive_name(unit, row['version'], arch) for unit, row in units.items() for arch in artifacts.ARCHES]
        names += ['workbench-' + arch + '-v1.2.3.tar.gz' for arch in artifacts.ARCHES]
        records = []
        for name in names:
            path = self.root / 'assets' / name
            path.write_bytes(('unit-test identity fixture ' + name).encode())
            records.append({'name': name, 'digest': 'sha256:' + artifacts.digest(path), 'size': path.stat().st_size})
        pins = {owner: 'd' * 40 for owner in artifacts.OWNERS if owner != 'platform'}
        self.plan = {'schema': 2, 'mode': 'exact-assets', 'framework_sha': 'a' * 40, 'owners': ['platform'],
            'baseline': {'version': self.version, 'sha': self.sha, 'delivery': 'workbench-v1', 'staged': True,
                         'units': units, 'assets': records}, 'test_revisions': artifacts.release_test_revisions(pins, self.sha),
            'test_overlays': [], 'case_files': WORKBENCH_CASES, 'lanes': {arch: {'products': [], 'performance': [],
                'selection': artifacts.suite_selection(['platform'], arch, WORKBENCH_CASES)} for arch in artifacts.ARCHES}}
        self.receipts = {}
        for index, arch in enumerate(artifacts.ARCHES, 1):
            name = 'workbench-' + arch + '-v1.2.3.tar.gz'
            path = self.root / 'assets' / name
            self.receipts[arch] = {'arch': arch, 'aggregate_version': self.version, 'source_revision': self.sha,
                'archive': name, 'sha256': artifacts.digest(path), 'image_id': 'sha256:' + str(index) * 64, 'size': path.stat().st_size}
            (self.root / 'workbench' / ('workbench-' + arch + '.json')).write_text(json.dumps(self.receipts[arch]))
        (self.root / 'test-revisions.json').write_text(json.dumps(pins))
        (self.root / 'release-notes.md').write_text('Original notes\n')
        self.results = workbench_results(self.plan, self.receipts)
        self.validation = artifacts.collect_results(self.plan, {arch: architecture_result(self.plan, arch) for arch in artifacts.ARCHES})

    def test_byte_bound_joint_results_preserve_assets_and_notes(self):
        before = artifacts.tree_files(self.root / 'assets')
        binder.bind(self.root, self.plan, self.validation, self.results)
        self.assertEqual(artifacts.tree_files(self.root / 'assets'), before)
        text = (self.root / 'release-notes.md').read_text()
        self.assertTrue(text.startswith('Original notes\n'))
        binding = json.loads(text.split('<!-- kuasar-integration-validation ')[1].split(' -->')[0])
        self.assertEqual(binding['workbench'], self.results)
        with self.assertRaisesRegex(ValueError, 'registry identity'):
            artifacts.check_registry_binding(self.version, binding)
        binding['registry'] = registry_binding(self.version, self.results)
        artifacts.check_registry_binding(self.version, binding)
        binding['registry']['architectures']['x86_64']['image_id'] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            artifacts.check_registry_binding(self.version, binding)

    def test_missing_arch_input_or_declared_asset_never_becomes_history(self):
        for name in ('workbench-aarch64-v1.2.3.tar.gz', artifacts.archive_name('connector', 'v1.2.3', 'aarch64')):
            broken = copy.deepcopy(self.plan)
            broken['baseline']['assets'] = [row for row in broken['baseline']['assets'] if row['name'] != name]
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'complete declared assets'):
                artifacts.check_plan(broken)
        with self.assertRaisesRegex(ValueError, 'both workbench architecture'):
            binder.bind(self.root, self.plan, self.validation, {'x86_64': self.results['x86_64']})

    def test_incomplete_or_mismatched_system_evidence_cannot_publish(self):
        mutations = [('preparation_network', 'bridge'), ('execution_network', 'none'), ('offline', False), ('empty_private_daemon', False), ('conclusion', 'skipped'),
                     ('cases', []), ('timings', []), ('image_id', 'sha256:' + '0' * 64),
                     ('input_assets', {}), ('source_revision', 'e' * 40), ('preflight', {}),
                     ('host_apparmor_enabled', True), ('disk', {}), ('size', 2 * 1024**3),
                     ('provenance_sha256', ''), ('compression_seconds', -1), ('import_seconds', float('nan')), ('start_seconds', True), ('isolation', {'complete': False})]
        for key, value in mutations:
            broken = copy.deepcopy(self.results); broken['aarch64'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                binder.bind(self.root, self.plan, self.validation, broken)
            self.assertEqual((self.root / 'release-notes.md').read_text(), 'Original notes\n')

    def artifact_only(self, arch='aarch64'):
        results = copy.deepcopy(self.results)
        record = results[arch]
        record['qualification_scope'] = 'artifact-only'
        record['planned_cases'] = record['cases']
        record['cases'] = []
        for key in ('offline', 'empty_private_daemon', 'isolation', 'preflight', 'apparmor',
                    'timings', 'provenance_sha256', 'start_seconds', 'preparation_network', 'execution_network'):
            record.pop(key, None)
        return results

    def test_explicit_hosted_arm_artifacts_bind_without_claiming_system_execution(self):
        results = self.artifact_only()
        binder.bind(self.root, self.plan, self.validation, results)
        binding = json.loads((self.root / 'release-notes.md').read_text().split(
            '<!-- kuasar-integration-validation ')[1].split(' -->')[0])
        self.assertEqual(binding['workbench']['aarch64']['cases'], [])
        self.assertEqual(binding['workbench']['aarch64']['qualification_scope'], 'artifact-only')
        self.assertEqual(binding['workbench']['x86_64']['qualification_scope'], 'system')

    def test_artifact_scope_is_never_inferred_or_accepted_as_system_proof(self):
        for key, value in [('qualification_scope', None), ('qualification_scope', 'system'),
                           ('imported_image_id', 'sha256:' + '0' * 64), ('release_inputs_verified', False),
                           ('planned_cases', []), ('cases', ['sandbox.lifecycle.sh']),
                           ('offline', True), ('preflight', {}), ('timings', [])]:
            results = self.artifact_only()
            results['aarch64'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                binder.bind(self.root, self.plan, self.validation, results)
        with self.assertRaisesRegex(ValueError, 'qualification scope'):
            binder.bind(self.root, self.plan, self.validation, self.artifact_only('x86_64'))

    def test_published_reader_requires_declared_assets_and_current_profiles(self):
        spec = importlib.util.spec_from_file_location('workbench_release_reader', ROOT / 'ci/integration/resolve-artifacts.py')
        reader = importlib.util.module_from_spec(spec); spec.loader.exec_module(reader)
        binder.bind(self.root, self.plan, self.validation, self.results)
        original = json.loads(reader.PROFILE_BINDING.search((self.root / 'release-notes.md').read_text())[1])
        original['registry'] = registry_binding(self.version, self.results)
        manifest = 'delivery: workbench-v1\nversion: ' + self.version + '\ncomponents:\n' + ''.join(
            f"  {unit}: {row['version']}\n" for unit, row in self.plan['baseline']['units'].items())
        manifest += 'test_revisions:\n' + ''.join(f"  {owner}: {row['sha']}\n"
            for owner, row in self.plan['test_revisions'].items() if owner != 'platform')
        assets = [dict(row, state='uploaded', id=index) for index, row in enumerate(self.plan['baseline']['assets'], 1)]
        state = {'id': 1, 'tag_name': self.version, 'target_commitish': self.sha, 'draft': False, 'prerelease': False,
                 'assets': assets}
        def notes(binding):
            state['body'] = '<!-- kuasar-integration-validation ' + json.dumps(binding) + ' -->'
        run = {'id': 1, 'status': 'completed', 'conclusion': 'success', 'html_url': 'https://example.invalid/run',
               'display_title': reader.release.aggregate_run_title(self.version, self.sha)}
        with patch.object(reader.release, 'api_optional', return_value=state), patch.object(reader, 'source_text', return_value=manifest), patch.object(
                reader.release, 'aggregate_runs', return_value=[run]), patch.object(reader, 'case_files', return_value=WORKBENCH_CASES), patch.object(
                reader.release, 'tag_sha', side_effect=lambda repository, tag: self.sha if repository == reader.PLATFORM else 'c' * 40):
            notes(original)
            self.assertEqual(reader.aggregate(self.version)['delivery'], 'workbench-v1')
            artifact_binding = copy.deepcopy(original)
            artifact_binding['workbench'] = self.artifact_only()
            notes(artifact_binding)
            self.assertEqual(reader.aggregate(self.version)['delivery'], 'workbench-v1')
            notes(original)
            state['assets'] = [row for row in assets if not row['name'].startswith('workbench-')]
            with self.assertRaisesRegex(ValueError, 'complete workbench asset set'):
                reader.aggregate(self.version)
            state['assets'] = assets
            broken = copy.deepcopy(original)
            for arch, record in broken['architectures'].items():
                record.pop('selection'); record['profile'] = reader.historical_profile(arch)
            notes(broken)
            with self.assertRaisesRegex(ValueError, 'current explicit case selection'):
                reader.aggregate(self.version)
            for field in ('workbench', 'registry', 'delivery'):
                broken = copy.deepcopy(original); broken.pop(field); notes(broken)
                with self.subTest(field=field), self.assertRaises(ValueError):
                    reader.aggregate(self.version)

    def test_framework_and_test_provenance_remain_required(self):
        for key, value in [('plan_id', '0' * 64), ('framework_sha', 'e' * 40), ('test_revisions', {})]:
            broken = copy.deepcopy(self.results); broken['x86_64'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'provenance differs'):
                binder.bind(self.root, self.plan, self.validation, broken)


if __name__ == '__main__':
    unittest.main()
