import json
import unittest
from unittest.mock import patch

import workbench_gc as subject

VERSION = 'release-v1.2.3-preview.20260928.2'
TAG = VERSION.removeprefix('release-')


def record(identifier, tags, digest=None):
    return {'id': identifier, 'name': digest or 'sha256:' + str(identifier) * 64,
            'metadata': {'container': {'tags': tags}}}


class WorkbenchGCTests(unittest.TestCase):
    def test_only_exact_preview_tags_are_candidates_and_shared_content_survives(self):
        rows = [record(1, [TAG]), record(2, [TAG + '-x86_64']), record(3, [TAG + '-aarch64', 'stable']),
                record(4, ['v1.2.4']), record(5, []), record(6, [TAG + '-aarch64'])]
        pending, shared = subject.candidates(rows, VERSION, {rows[5]['name']})
        self.assertEqual([row['id'] for row in pending], [1, 2])
        self.assertEqual(shared, [3, 6])

    def test_other_live_release_binding_protects_parent_and_children(self):
        proof = {'reference': subject.registry.REPOSITORY + ':other', 'digest': 'sha256:' + 'a' * 64,
                 'architectures': {'x86_64': {'digest': 'sha256:' + 'b' * 64},
                                   'aarch64': {'digest': 'sha256:' + 'c' * 64}}}
        notes = subject.registry.MARKER + json.dumps({'registry': proof}) + ' -->'
        self.assertEqual(subject.protected_digests([{'tag_name': 'other', 'body': notes}], VERSION),
                         {proof['digest'], 'sha256:' + 'b' * 64, 'sha256:' + 'c' * 64})
        self.assertFalse(subject.protected_digests([{'tag_name': VERSION, 'body': notes}], VERSION))

    def test_shared_index_protects_children_with_only_owned_architecture_tags(self):
        child = json.dumps({'schemaVersion': 2, 'config': {}, 'layers': []}).encode()
        child_id = subject.registry.digest(child)
        parent = json.dumps({'schemaVersion': 2, 'manifests': [{'digest': child_id}]}).encode()
        parent_id = subject.registry.digest(parent)
        rows = [record(1, [TAG, 'retained-alias'], parent_id), record(2, [TAG + '-x86_64'], child_id)]
        with patch.object(subject.registry, 'inspect_raw', side_effect=[parent, child]):
            protected = subject.shared_closure(rows, VERSION, set(), None)
        pending, retained = subject.candidates(rows, VERSION, protected)
        self.assertEqual(pending, [])
        self.assertEqual(retained, [1, 2])

    def test_foreign_source_or_self_claimed_config_prevents_deletion(self):
        config = json.dumps({'config': {'Labels': {'org.opencontainers.image.source': subject.SOURCE,
                    'org.opencontainers.image.version': VERSION, 'org.opencontainers.image.revision': 'b' * 40}}}).encode()
        manifest = json.dumps({'config': {'digest': subject.registry.digest(config)}}).encode()
        with patch.object(subject.registry, 'inspect_raw', side_effect=[manifest, config]):
            with self.assertRaisesRegex(ValueError, 'another source/version'):
                subject.verify_source(subject.registry.digest(manifest), VERSION, 'a' * 40, None)
        with patch.object(subject.registry, 'inspect_raw', return_value=manifest):
            with self.assertRaisesRegex(ValueError, 'manifest identity differs'):
                subject.verify_source('sha256:' + 'c' * 64, VERSION, 'a' * 40, None)

    def test_active_publication_prevents_any_registry_mutation(self):
        with patch.object(subject.coordinator, 'aggregate_runs', return_value=[{'status': 'in_progress'}]), patch.object(
                subject.coordinator, 'api_optional') as api:
            with self.assertRaisesRegex(ValueError, 'still active'):
                subject.collect(VERSION, 'a' * 40)
            api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
