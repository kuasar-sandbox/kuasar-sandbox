"""Only manual correction can resolve a complete publication body overflow."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'release'))
import release_notes as notes
import publication_body as body
import test_workbench_binding


class ManualBudgetTests(unittest.TestCase):
    def test_exact_limit_keeps_complete_prose_and_evidence(self):
        prefix = 'Keep this handwritten warning.  \n请保留全部正文。\n' * 100
        evidence = {'validation_plan': {'cases': ['complete evidence'], 'sha': 'a' * 40}}
        original = copy.deepcopy(evidence)
        expected = body.binding_text(prefix, evidence, '\n')
        with patch.object(body, 'MAX_CHARACTERS', len(expected)):
            self.assertEqual(body.binding_text(prefix, evidence, '\n'), expected)
            with self.assertRaisesRegex(ValueError, 'manual intervention required.*edit and commit notes.body'):
                body.binding_text(prefix + 'x', evidence, '\n')
        self.assertEqual(evidence, original)
        self.assertEqual(json.loads(expected.split(body.MARKER)[1].split(' -->')[0]), original)

    def test_oversized_machine_evidence_is_not_truncated(self):
        evidence = {'validation_plan': {'evidence': 'x' * body.MAX_CHARACTERS}}
        before = copy.deepcopy(evidence)
        with self.assertRaisesRegex(ValueError, 'evidence must not be truncated'):
            body.binding_text('Short notes', evidence, '\n')
        self.assertEqual(evidence, before)

    def test_no_automatic_rewriting_or_manifest_persistence_interface(self):
        self.assertFalse(hasattr(notes, 'compact_commits'))
        self.assertFalse(hasattr(notes, 'persist_compacted_notes'))
        self.assertFalse(hasattr(notes.Narrative, 'fit'))


class BindingManualBudgetTests(unittest.TestCase):
    setUp = test_workbench_binding.BindingTests.setUp

    def test_overflow_preserves_bundle_and_never_writes_manifest(self):
        binder = test_workbench_binding.binder
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        # Even recognizable generated rows must remain intact, in both modes.
        row = '- `aaaaaaaaaaaa` title ([commit](https://github.com/kuasar-sandbox/sandboxer/commit/' + 'a' * 40 + '))\n'
        human = '### sandboxer\nSelected version: `v1` → `v2`.\n' + row * 1000 + '[Full comparison](https://github.com/kuasar-sandbox/sandboxer/compare/v1...v2)\n'
        for mode in ('auto', 'fixed'):
            with self.subTest(mode=mode), patch.object(notes, 'publication_notes', return_value=({'mode': mode, 'body': human}, 'url')):
                with self.assertRaisesRegex(ValueError, 'manual intervention required'):
                    binder.bind(self.root, self.plan, self.validation, self.results, platform=self.root)
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_successful_binding_keeps_canonical_body_verbatim(self):
        binder = test_workbench_binding.binder
        human = 'Handwritten warning.  \nUnicode → 请保留。\n'
        with patch.object(notes, 'publication_notes', return_value=({'mode': 'fixed', 'body': human}, 'url')):
            binder.bind(self.root, self.plan, self.validation, self.results, platform=self.root)
        result = (self.root / 'release-notes.md').read_text()
        self.assertEqual(result.split(body.MARKER)[0], human + '\n')
        evidence = json.loads(result.split(body.MARKER)[1].split(' -->')[0])
        self.assertEqual(evidence['validation_plan'], self.plan)
        self.assertEqual(evidence['source_records'], self.plan['source_records'])
        self.assertEqual(evidence['workbench'], self.results)
