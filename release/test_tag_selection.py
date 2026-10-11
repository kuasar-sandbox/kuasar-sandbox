"""The maintained selection has one tag per unit; history is read-only evidence."""
import copy
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selection
import preview_coordinator as coordinator
import formal_coordinator as formal


def manifest(preview=False):
    lines = ['version: release-v1.2.3', 'delivery: workbench-v1']
    if preview:
        lines.append('preview_version: preview.20261010')
    lines.append('components:')
    lines.extend(f'  {unit}: {unit + "-" if unit in ("runtime", "vmlinux") else ""}v1.2.3'
                 for unit in selection.UNITS)
    return '\n'.join(lines) + '\n'


def historical_manifest(preview=False):
    return manifest(preview) + 'test_revisions:\n' + ''.join(
        f'  {owner}: {index:040x}\n' for index, owner in enumerate(selection.TEST_OWNERS, 1))


class TagSelectionTests(unittest.TestCase):
    def test_new_manifest_rejects_independent_test_selection(self):
        for preview in (False, True):
            with self.subTest(preview=preview), self.assertRaisesRegex(selection.ManifestError, 'test_revisions'):
                selection.parse_manifest(historical_manifest(preview), 'new selection', preview)

    def test_read_only_history_preserves_true_test_evidence(self):
        text = historical_manifest(True)
        before = selection.read_simple_yaml(text, 'immutable history')
        saved = copy.deepcopy(before)
        record = selection.parse_historical_manifest(text, 'immutable history', True)
        self.assertEqual(record[0], 'release-v1.2.3-preview.20261010')
        self.assertEqual(selection.historical_test_revisions(before, 'immutable history'), saved['test_revisions'])
        self.assertEqual(before, saved)
        with self.assertRaises(selection.ManifestError):
            selection.parse_historical_manifest(text.replace('0000000000000000000000000000000000000001', 'main'), 'bad history', True)

    def test_new_selection_accepts_only_unit_tags(self):
        for value in ('a' * 40, 'main', 'HEAD', 'refs/heads/main'):
            with self.subTest(value=value), self.assertRaises(selection.ManifestError):
                selection.parse_manifest(manifest().replace('  connector: v1.2.3', '  connector: ' + value), 'new selection', False)

    def test_history_reader_does_not_authorize_new_assembly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'releases').mkdir()
            (root / 'releases/release.yaml').write_text(manifest())
            (root / 'releases/daily-preview.yaml').write_text(manifest(True))
            self.assertEqual(selection.resolve_new_selection(root, 'release-v1.2.3'),
                             selection.parse_manifest(manifest(), 'fixture', False)[2])
            with self.assertRaisesRegex(selection.ManifestError, 'consume historical packages by tag'):
                selection.resolve_new_selection(root, 'release-v1.2.2')
            (root / 'releases/release.yaml').write_text(historical_manifest())
            with self.assertRaisesRegex(selection.ManifestError, 'historical evidence only'):
                selection.resolve_new_selection(root, 'release-v1.2.3')

    def test_guide_declaration_and_test_changes_do_not_widen_product_diff(self):
        import preview_selection_test
        repository = preview_selection_test.Repository()
        self.addCleanup(repository.close)
        before = repository.commit_files('product', {'cmd/main.go': 'package main\n',
            'native-deps/Makefile': 'LINUX_TARBALL := https://kernel.invalid/fixture.tar.gz\n'})
        after = repository.commit_files('delivery contract', {
            'release/guide-inputs.txt': 'README*.md\ndocs/\n',
            'docs/ordinary.md': 'updated guide\n',
            'test/e2e/cases/basic.fixture.sh': 'echo candidate-test\n'})
        for unit in selection.UNITS:
            with self.subTest(unit=unit):
                self.assertFalse(coordinator.preview_selection.unit_inputs_changed(repository.root, before, after, unit))

    def test_render_has_no_test_selection_even_when_branch_tests_advance(self):
        components = selection.parse_manifest(manifest(True), 'fixture', True)[2]
        plans = {unit.name: coordinator.Plan(unit, components[unit.name], 'main', 'a' * 40,
                    components[unit.name], components[unit.name], 'reuse', source_head='b' * 40)
                 for unit in coordinator.UNITS}
        text = coordinator.render_manifest('release-v1.2.3', None, '20261010', None, plans)
        self.assertNotIn('test_revisions', text)
        self.assertNotIn('b' * 40, text)
        self.assertEqual(selection.parse_manifest(text, 'generated', True)[2], components)
        fixed = {name: coordinator.replace(plan, source_ref=None, source_head=None) for name, plan in plans.items()}
        self.assertEqual(coordinator.render_manifest('release-v1.2.3', None, '20261010', None, fixed), text)

    def test_test_head_changes_do_not_trigger_new_preview_selection(self):
        units = selection.parse_manifest(manifest(True), 'fixture', True)[2]
        plans = {unit.name: coordinator.Plan(unit, units[unit.name], 'main', 'a' * 40,
                    units[unit.name], units[unit.name], 'reuse', source_head='b' * 40)
                 for unit in coordinator.UNITS}
        with mock.patch.object(coordinator, 'validate_environment'), \
             mock.patch.object(coordinator, 'PLATFORM_REF', 'main'), \
             mock.patch.object(coordinator, 'TODAY', '20261011'), \
             mock.patch.object(coordinator, 'manifest_values', return_value=(
                 'release-v1.2.3', 'release-v1.2.2', 'preview.20261010', None, units)), \
             mock.patch.object(coordinator, 'release_version', return_value='release-v1.2.2'), \
             mock.patch.object(coordinator, 'platform_release', return_value=coordinator.ReleaseStatus({}, 'a' * 40, True)), \
             mock.patch.object(coordinator, 'active_aggregate_run', return_value=None), \
             mock.patch.object(coordinator, 'active_delete_run', return_value=None), \
             mock.patch.object(coordinator, 'active_release_run', return_value=None), \
             mock.patch.object(coordinator, 'plan_units', return_value=plans), \
             mock.patch.object(coordinator, 'platform_changed_since', return_value=False), \
             mock.patch.object(coordinator, 'persist_manifest') as persist:
            self.assertEqual(coordinator.main(), 'unchanged')
            persist.assert_not_called()

    def test_formal_coordinator_requires_tags_not_independent_test_revisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'releases').mkdir()
            (root / 'releases/release.yaml').write_text(manifest())
            (root / 'releases/daily-preview.yaml').write_text(manifest(True))
            # All publication-facing work is mocked; this exercises admission only.
            with mock.patch.object(formal, 'ROOT', root), \
                 mock.patch.dict(formal.os.environ, {'RELEASE_VERSION': 'release-v1.2.3',
                     'PLATFORM_REF': 'main', 'PLATFORM_SHA': 'a' * 40}), \
                 mock.patch.object(formal.coordinator, 'run', return_value=mock.Mock(stdout='a' * 40)), \
                 mock.patch.object(formal.coordinator, 'branch_sha', return_value='a' * 40), \
                 mock.patch.object(formal.coordinator, 'platform_release', return_value=coordinator.ReleaseStatus(None, None, False)), \
                 mock.patch.object(formal.coordinator, 'RepositoryState') as state, \
                 mock.patch.object(formal.coordinator, 'has_arm_archive', return_value=True), \
                 mock.patch.object(formal, 'wait_for_convergence') as converge:
                state.return_value.status.return_value = coordinator.ReleaseStatus({}, 'c' * 40, True)
                formal.main()
            plans, version, sha = converge.call_args.args
            self.assertEqual((version, sha), ('release-v1.2.3', 'a' * 40))
            self.assertEqual({name: plan.selected for name, plan in plans.items()},
                             selection.parse_manifest(manifest(), 'fixture', False)[2])


if __name__ == '__main__':
    unittest.main()
