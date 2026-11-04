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
    def test_exact_limit_keeps_complete_prose(self):
        text = 'Keep this handwritten warning.  \n请保留全部正文。\n' * 100
        with patch.object(body, 'MAX_CHARACTERS', len(text)):
            self.assertEqual(body.checked(text), text)
            with self.assertRaisesRegex(ValueError, 'manual intervention required.*edit and commit notes.body'):
                body.checked(text + 'x')

    def test_machine_evidence_is_not_a_publication_input(self):
        self.assertFalse(hasattr(body, 'binding_text'))
        for marker in ('kuasar-integration-validation', 'kuasar-preview-binding', 'kuasar-release-source'):
            with self.assertRaisesRegex(ValueError, 'machine'):
                body.checked('Human notes\n<!-- ' + marker + ' {} -->')

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
        self.assertEqual(result, human)
        self.assertFalse(body.has_machine_records(result))
