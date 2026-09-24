#!/usr/bin/env python3
"""Regression coverage for legacy-vs-rewritten E2E entry modes."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ci/integration"))
SPEC = importlib.util.spec_from_file_location("prepare_artifacts", ROOT / "ci/integration/prepare-artifacts.py")
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


class SelectedEntryModes(unittest.TestCase):
    def test_rewritten_case_may_be_0644_but_legacy_runner_must_be_executable(self):
        with tempfile.TemporaryDirectory(prefix="kuasar-entry-modes-") as directory:
            workspace = Path(directory)
            rewritten = workspace / "test/e2e/connector/cases/network.tap.sh"
            rewritten.parent.mkdir(parents=True)
            rewritten.write_text("#!/bin/sh\nexit 0\n")
            rewritten.chmod(0o644)
            PREPARE.validate_selected_entry_modes(
                workspace, {"cases": ["test/e2e/connector/cases/network.tap.sh"]})

            legacy = workspace / "test/e2e/sandboxer/run_all.sh"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("#!/bin/sh\nexit 0\n")
            legacy.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "legacy test entry is not executable"):
                PREPARE.validate_selected_entry_modes(
                    workspace, {"cases": ["test/e2e/sandboxer/run_all.sh"]})

            legacy.chmod(0o755)
            PREPARE.validate_selected_entry_modes(
                workspace, {"cases": ["test/e2e/sandboxer/run_all.sh"]})


if __name__ == "__main__":
    unittest.main()
