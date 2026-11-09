import json
import os
import subprocess
import unittest
from unittest.mock import patch

import workbench_gc as subject

VERSION = 'release-v1.2.3-preview.20260928.2'
TAG = VERSION.removeprefix('release-')


def record(identifier, tags, digest=None):
    return {'id': identifier, 'name': digest or 'sha256:' + str(identifier) * 64,
            'metadata': {'container': {'tags': tags}}}


class WorkbenchGCTests(unittest.TestCase):
    def test_node_infra_tag_namespace_is_explicit_and_does_not_adopt_workbench_tags(self):
        rows = [record(1, [VERSION]), record(2, [VERSION + '-amd64']),
                record(3, [VERSION + '-arm64', 'retained']), record(4, [TAG + '-x86_64'])]
        pending, shared = subject.candidates(rows, VERSION, set(), image='node-infra')
        self.assertEqual([row['id'] for row in pending], [1, 2])
        self.assertEqual(shared, [3])

    def test_only_exact_preview_tags_are_candidates_and_shared_content_survives(self):
        rows = [record(1, [TAG]), record(2, [TAG + '-x86_64']), record(3, [TAG + '-aarch64', 'stable']),
                record(4, ['v1.2.4']), record(5, []), record(6, [TAG + '-aarch64'])]
        pending, shared = subject.candidates(rows, VERSION, {rows[5]['name']})
        self.assertEqual([row['id'] for row in pending], [1, 2])
        self.assertEqual(shared, [3, 6])

    def test_tagless_non_target_index_protects_its_children(self):
        child = json.dumps({'schemaVersion': 2, 'config': {}, 'layers': []}).encode()
        child_id = subject.registry.digest(child)
        parent = json.dumps({'schemaVersion': 2, 'manifests': [{'digest': child_id}]}).encode()
        parent_id = subject.registry.digest(parent)
        rows = [record(1, [], parent_id), record(2, [TAG + '-x86_64'], child_id)]
        with patch.object(subject.registry, 'inspect_raw', side_effect=[parent, child]):
            protected = subject.shared_closure(rows, VERSION, set(), None)
        pending, retained = subject.candidates(rows, VERSION, protected)
        self.assertEqual(pending, [])
        self.assertEqual(retained, [2])

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
        config = json.dumps({'os': 'linux', 'architecture': 'amd64', 'config': {'Labels': {'org.opencontainers.image.source': subject.SOURCE,
                    'org.opencontainers.image.version': VERSION, 'org.opencontainers.image.revision': 'b' * 40}}}).encode()
        manifest = json.dumps({'config': {'digest': subject.registry.digest(config)}}).encode()
        with patch.object(subject.registry, 'inspect_raw', side_effect=[manifest, config]):
            with self.assertRaisesRegex(ValueError, 'another source/version'):
                subject.verify_source(subject.registry.digest(manifest), VERSION, 'a' * 40, None)
        with patch.object(subject.registry, 'inspect_raw', return_value=manifest):
            with self.assertRaisesRegex(ValueError, 'manifest identity differs'):
                subject.verify_source('sha256:' + 'c' * 64, VERSION, 'a' * 40, None)

    def test_validation_runs_do_not_own_registry_gc_but_ordinary_runs_do(self):
        title = subject.coordinator.aggregate_run_title(VERSION, 'a' * 40)
        for status in ('queued', 'pending', 'in_progress'):
            validation = [dict(display_title=title + ' [validation only]', status=status),
                          dict(display_title=title.replace('Aggregate ', 'Validate aggregate ', 1), status=status)]
            for ordinary in (False, True):
                rows = validation + ([dict(display_title=title, status=status)] if ordinary else [])
                with self.subTest(status=status, ordinary=ordinary), \
                     patch.object(subject.coordinator, 'paginated', return_value=rows), \
                     patch.object(subject.coordinator, 'api_optional', return_value=None) as api, \
                     patch.object(subject.coordinator, 'gh') as mutation:
                    if ordinary:
                        with self.assertRaisesRegex(ValueError, 'still active'):
                            subject.collect(VERSION, 'a' * 40)
                        api.assert_not_called()
                    else:
                        self.assertEqual(subject.collect(VERSION, 'a' * 40), {'deleted': [], 'retained_shared': []})
                        api.assert_called_once()
                    mutation.assert_not_called()

    def fixture_collect(self, *, failure=None, shared_after_parent=False, missing=False, retained_parent=None,
                        image='workbench'):
        tag = TAG if image == 'workbench' else VERSION
        suffix = '-aarch64' if image == 'workbench' else '-arm64'
        package = subject.PACKAGE if image == 'workbench' else 'orgs/kuasar-sandbox/packages/container/node-infra'
        configs, documents, children = {}, {}, []
        for i, arch in enumerate(('amd64', 'arm64')):
            config = json.dumps({'os': 'linux', 'architecture': arch, 'config': {'Labels': {
                'org.opencontainers.image.source': subject.SOURCE,
                'org.opencontainers.image.version': VERSION,
                'org.opencontainers.image.revision': 'a' * 40}}}).encode()
            child = json.dumps({'schemaVersion': 2, 'config': {'digest': subject.registry.digest(config)}, 'layers': []}).encode()
            identity = subject.registry.digest(child)
            documents[identity], configs[identity] = child, config
            children.append({'digest': identity, 'platform': {'os': 'linux', 'architecture': arch}})
        parent = json.dumps({'schemaVersion': 2, 'manifests': children}).encode()
        parent_id = subject.registry.digest(parent)
        documents[parent_id] = parent
        # API order intentionally places a tagless child before its parent.
        rows = [record(2, [], children[0]['digest']), record(1, [tag], parent_id),
                record(3, [tag + suffix], children[1]['digest'])]
        deletes, listings = [], []
        def api(endpoint):
            if endpoint == package:
                return {'repository': {'full_name': subject.coordinator.PLATFORM_REPOSITORY}}
            if retained_parent == 'detail' and endpoint.endswith('/1'):
                parent = next(x for x in rows if x['id'] == 1)
                return parent | {'metadata': {'container': {'tags': [tag, 'shared']}}}
            return next((x for x in rows if endpoint.endswith('/' + str(x['id']))), None)
        def listing(endpoint):
            self.assertEqual(endpoint, package + '/versions?per_page=100')
            listings.append(endpoint)
            if retained_parent == 'tagless' and len(listings) > 1:
                next(x for x in rows if x['id'] == 1)['metadata']['container']['tags'] = []
            if shared_after_parent and deletes:
                rows[0]['metadata']['container']['tags'] = ['another-release']
            return rows.copy()
        def delete(*args, **kwargs):
            self.assertEqual(args[:3], ('api', '--method', 'DELETE'))
            identifier = int(args[3].rsplit('/', 1)[1])
            deletes.append(identifier)
            if failure:
                return subprocess.CompletedProcess(args, 1, '', failure)
            rows[:] = [x for x in rows if x['id'] != identifier]
            return subprocess.CompletedProcess(args, 1 if missing else 0, '', 'gh: Not Found (HTTP 404)' if missing else '')
        with patch.object(subject.coordinator, 'aggregate_runs', return_value=[]), patch.object(
                subject.coordinator, 'api_optional', side_effect=api), patch.object(
                subject.coordinator, 'paginated', side_effect=listing), patch.object(
                subject.coordinator, 'gh', side_effect=delete), patch.object(subject.registry, 'inspect_raw',
                side_effect=lambda ref, auth, config=False: (configs if config else documents)[ref.split('@')[1]]), patch.dict(
                os.environ, {'GH_TOKEN': 'test-only', 'GITHUB_ACTOR': 'fixture'}):
            if failure:
                with self.assertRaises(ValueError): subject.collect(VERSION, 'a' * 40, image=image)
                self.assertEqual(deletes, [1])
                return
            result = subject.collect(VERSION, 'a' * 40, image=image)
        return result, deletes

    def test_delete_index_before_exclusive_children_including_direct_tagless_child(self):
        result, calls = self.fixture_collect()
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(result, {'deleted': [1, 2, 3], 'retained_shared': []})

    def test_node_infra_uses_same_exclusive_scope_and_shared_parent_protection(self):
        self.assertEqual(self.fixture_collect(image='node-infra')[1], [1, 2, 3])
        result, calls = self.fixture_collect(image='node-infra', retained_parent='detail')
        self.assertEqual(calls, [])
        self.assertEqual(result['retained_shared'], [1, 2, 3])

    def test_refresh_sharing_before_each_delete(self):
        result, calls = self.fixture_collect(shared_after_parent=True)
        self.assertEqual(calls, [1, 3])
        self.assertEqual(result['retained_shared'], [2])

    def test_retained_parent_protects_children_even_if_paginated_state_is_stale(self):
        for change in ('tagless', 'detail'):
            with self.subTest(change=change):
                result, calls = self.fixture_collect(retained_parent=change)
                self.assertEqual(calls, [])
                self.assertEqual(result['retained_shared'], [1, 2, 3])

    def test_delete_404_is_idempotent_but_other_errors_are_not_absence(self):
        self.assertEqual(self.fixture_collect(missing=True)[1], [1, 2, 3])
        for error in ('gh: Forbidden (HTTP 403)', 'network timeout', 'gh: Server Error (HTTP 500)'):
            with self.subTest(error=error): self.fixture_collect(failure=error)

    def test_active_publication_prevents_any_registry_mutation(self):
        with patch.object(subject.coordinator, 'aggregate_runs', return_value=[{'status': 'in_progress'}]), patch.object(
                subject.coordinator, 'api_optional') as api:
            with self.assertRaisesRegex(ValueError, 'still active'):
                subject.collect(VERSION, 'a' * 40)
            api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
