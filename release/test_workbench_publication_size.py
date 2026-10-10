"""Use complete historical evidence to bound the next publication's JSON body."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'release'))
sys.path.insert(0, str(ROOT / 'ci/integration'))
import artifacts
import publication_body
from test_fixtures import tag_source_plan


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

    def test_limit_fails_without_truncating_evidence(self):
        text = 'x' * publication_body.MAX_CHARACTERS
        self.assertEqual(publication_body.checked(text), text)
        with self.assertRaisesRegex(ValueError, 'must not be truncated'):
            publication_body.checked(text + 'x')


if __name__ == '__main__': unittest.main()
