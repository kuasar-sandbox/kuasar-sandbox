"""Use complete historical evidence to bound the next publication's JSON body."""
import json
import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'release'))
sys.path.insert(0, str(ROOT / 'ci/integration'))
import artifacts
import publication_body
from test_fixtures import tag_source_plan, helper_contract, registry_binding


class PublicationSize(unittest.TestCase):
    def test_real_release_with_full_plan_and_owner_records_fits_api(self):
        fixtures = ROOT / 'ci/integration/fixtures'
        old = (fixtures / 'pre-cutover-release-body.txt').read_text()
        marker = '<!-- kuasar-integration-validation '
        prefix, tail = old.split(marker)
        encoded, suffix = tail.split(' -->', 1)
        binding = json.loads(encoded)
        plan = json.loads((fixtures / 'pre-cutover-plan.json').read_text())
        self.assertEqual(binding['plan_id'], artifacts.identity(plan))
        # The plan is additional self-contained evidence, never a summary.
        binding['validation_plan'] = plan
        binding['case_files'] = plan['case_files']
        binding['source_records'] = {unit: dict(repository=row['repository'], tag=row['version'],
            sha=row['sha'], tree='9' * 40) for unit, row in plan['baseline']['units'].items()}
        text = prefix + marker + artifacts.canonical(binding).decode() + ' -->' + suffix
        checked = publication_body.checked(text)
        payload = json.dumps({'body': checked}, ensure_ascii=True)
        self.assertEqual(json.loads(payload)['body'], text)
        restored = json.loads(checked.split(marker)[1].split(' -->')[0])
        self.assertEqual(artifacts.identity(restored['validation_plan']), binding['plan_id'])
        self.assertEqual(restored['architectures'], binding['architectures'])
        self.assertEqual(restored['registry'], binding['registry'])
        self.assertEqual(restored['workbench'], binding['workbench'])
        print(f'Release body: original={len(old)}, with full evidence={len(text)}, API limit={publication_body.MAX_CHARACTERS}, JSON bytes={len(payload.encode())}')

    def test_full_owner_contract_is_retained_in_publication_payload(self):
        fixtures = ROOT / 'ci/integration/fixtures'
        old = (fixtures / 'pre-cutover-release-body.txt').read_text()
        marker = '<!-- kuasar-integration-validation '
        prefix, tail = old.split(marker)
        encoded, suffix = tail.split(' -->', 1)
        binding = json.loads(encoded)
        plan = json.loads((fixtures / 'pre-cutover-plan.json').read_text())
        plan['schema'] = 3
        plan['helper_contract'] = helper_contract('a' * 40, 'b' * 40)
        binding.update(validation_plan=plan, plan_id=artifacts.identity(plan),
                       case_files=plan['case_files'])
        binding['source_records'] = {unit: dict(repository=row['repository'], tag=row['version'],
            sha=row['sha'], tree='9' * 40) for unit, row in plan['baseline']['units'].items()}
        text = prefix + marker + artifacts.canonical(binding).decode() + ' -->' + suffix
        payload = json.dumps({'body': publication_body.checked(text)}, ensure_ascii=True)
        restored = json.loads(json.loads(payload)['body'].split(marker)[1].split(' -->')[0])
        self.assertEqual(restored, binding)
        self.assertEqual(artifacts.identity(restored['validation_plan']), restored['plan_id'])
        self.assertEqual(restored['validation_plan']['helper_contract'], plan['helper_contract'])
        print(f'Release body with owner contract={len(text)}, JSON bytes={len(payload.encode())}')

    def test_limit_fails_without_truncating_evidence(self):
        text = 'x' * publication_body.MAX_CHARACTERS
        self.assertEqual(publication_body.checked(text), text)
        with self.assertRaisesRegex(ValueError, 'must not be truncated'):
            publication_body.checked(text + 'x')

    def test_complete_registry_proof_is_budgeted_without_persisting_placeholders(self):
        version = 'release-v0.1.7-preview.20261022.1'
        binding = {'workbench': {arch: {'image_id': 'sha256:' + '1' * 64, 'sha256': '2' * 64}
                                 for arch in artifacts.ARCHES}}
        text = publication_body.binding_text('', binding, '\n', reserve_registry_version=version)
        restored = json.loads(text.split(publication_body.MARKER)[1].split(' -->')[0])
        self.assertEqual(restored, binding)
        self.assertNotIn('registry', binding)
        proof = registry_binding(version, binding['workbench'])
        for record in proof['architectures'].values():
            record['size'] = sys.maxsize
        full = publication_body.binding_text('', dict(binding, registry=proof), '\n')
        prefix = 'x' * (publication_body.MAX_CHARACTERS - len(full))
        publication_body.binding_text(prefix, binding, '\n', reserve_registry_version=version)
        publication_body.binding_text(prefix, dict(binding, registry=proof), '\n')
        with self.assertRaisesRegex(ValueError, 'must not be truncated'):
            publication_body.binding_text(prefix + 'x', binding, '\n', reserve_registry_version=version)
        # The unreserved body fits; rejecting it early prevents partial publication.
        publication_body.binding_text(prefix + 'x', binding, '\n')

    def repeated_cases(self):
        cases = {'platform': [f'basic.case-{index}.sh' for index in range(100)]}
        return {'delivery': 'workbench-v1', 'case_files': cases,
                'validation_plan': {'case_files': copy.deepcopy(cases)},
                'workbench': {arch: {'image_id': 'sha256:' + '1' * 64, 'sha256': '2' * 64}
                              for arch in artifacts.ARCHES}}

    def test_normal_body_is_unchanged_and_only_full_proof_overflow_removes_duplicate(self):
        version = 'release-v0.1.7-preview.20261022.1'
        binding = self.repeated_cases()
        before = copy.deepcopy(binding)
        original = 'Notes\n' + publication_body.MARKER + artifacts.canonical(binding).decode() + ' -->\n'
        self.assertEqual(publication_body.binding_text('Notes\n', binding, '\n', reserve_registry_version=version), original)
        prefix = 'x' * (publication_body.MAX_CHARACTERS - len(original)) + 'Notes\n'
        # The original body fits exactly, but its final registry proof does not.
        self.assertEqual(len(publication_body.binding_text(prefix, binding, '\n')), publication_body.MAX_CHARACTERS)
        adjusted = publication_body.binding_text(prefix, binding, '\n', reserve_registry_version=version)
        restored = json.loads(adjusted.split(publication_body.MARKER)[1].split(' -->')[0])
        expected = {key: value for key, value in binding.items() if key != 'case_files'}
        self.assertEqual(restored, expected)
        self.assertEqual(binding, before)
        proof = registry_binding(version, binding['workbench'])
        for row in proof['architectures'].values():
            row['size'] = sys.maxsize
        complete = publication_body.binding_text(prefix, dict(binding, registry=proof), '\n')
        self.assertEqual(json.loads(complete.split(publication_body.MARKER)[1].split(' -->')[0]), dict(expected, registry=proof))
        self.assertLessEqual(len(complete), publication_body.MAX_CHARACTERS)

    def test_conflicting_optional_copy_is_rejected_even_before_size_adjustment(self):
        for value in ({'platform': ['basic.changed.sh']}, {'platform': [True]}, {'platform': [1]}):
            binding = self.repeated_cases()
            binding['case_files'] = value
            for prefix in ('', 'x' * publication_body.MAX_CHARACTERS):
                with self.subTest(value=value, oversized=bool(prefix)), self.assertRaisesRegex(ValueError, 'conflicting evidence'):
                    publication_body.binding_text(prefix, binding, '')
        binding = self.repeated_cases()
        binding['case_files'], binding['validation_plan']['case_files'] = {'platform': [True]}, {'platform': [1]}
        with self.assertRaisesRegex(ValueError, 'conflicting evidence'):
            publication_body.binding_text('', binding, '')

    def test_irreducible_overflow_reports_actual_budget_and_major_block_sizes(self):
        binding = self.repeated_cases()
        before = copy.deepcopy(binding)
        with self.assertRaisesRegex(ValueError, r'actual=\d+, budget=125000, blocks=.*validation_plan.*workbench.*must not be truncated'):
            publication_body.binding_text('x' * publication_body.MAX_CHARACTERS, binding, '',
                                          reserve_registry_version='release-v0.1.7-preview.20261022.1')
        self.assertEqual(binding, before)

    def test_registry_budget_rejects_missing_architecture_or_invalid_identity(self):
        for mutation in ('missing', 'extra', 'image', 'archive', 'number'):
            binding = self.repeated_cases()
            if mutation == 'missing': del binding['workbench']['aarch64']
            elif mutation == 'extra': binding['workbench']['unknown'] = binding['workbench']['x86_64']
            elif mutation == 'image': binding['workbench']['x86_64']['image_id'] = 'unknown'
            elif mutation == 'number': binding['workbench']['x86_64']['sha256'] = int('1' * 64)
            else: binding['workbench']['x86_64']['sha256'] = 'unknown'
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'registry capacity check requires'):
                publication_body.binding_text('', binding, '', reserve_registry_version='release-v0.1.7-preview.20261022.1')


if __name__ == '__main__': unittest.main()
