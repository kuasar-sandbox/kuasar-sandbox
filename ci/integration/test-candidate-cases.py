#!/usr/bin/env python3
"""Focused regressions for migrated candidate E2E case discovery."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("candidate_case_resolver", ROOT / "resolve-artifacts.py")
resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolver)


class CandidateCaseDiscoveryTests(unittest.TestCase):
    def discover(self, response, legacy=None):
        with patch.object(resolver.release, "api_optional", return_value=legacy), \
             patch.object(resolver.release, "gh", return_value=response):
            return resolver.candidate_case_names("kuasar-sandbox/connector", "a" * 40, "connector")

    def test_directory_array_is_accepted_and_sorted(self):
        response = subprocess.CompletedProcess(
            ["gh", "api"], 0,
            stdout=json.dumps([
                {"type": "file", "name": "network.tap.sh"},
                {"type": "file", "name": "network.management.sh"},
            ]), stderr="")
        self.assertEqual(self.discover(response), ["network.management.sh", "network.tap.sh"])

    def test_missing_directory_keeps_unmigrated_fallback(self):
        response = subprocess.CompletedProcess(["gh", "api"], 1, stdout="", stderr="gh: Not Found (HTTP 404)")
        self.assertEqual(self.discover(response), [])

    def test_non_directory_response_is_rejected(self):
        response = subprocess.CompletedProcess(["gh", "api"], 0, stdout=json.dumps({"type": "file"}), stderr="")
        with self.assertRaisesRegex(ValueError, "not a directory"):
            self.discover(response)

    def test_invalid_case_entry_is_rejected(self):
        response = subprocess.CompletedProcess(
            ["gh", "api"], 0,
            stdout=json.dumps([{"type": "dir", "name": "network.tap.sh"}]), stderr="")
        with self.assertRaisesRegex(ValueError, "flat files"):
            self.discover(response)

    def test_existing_owner_runner_does_not_query_cases(self):
        legacy = {"type": "file", "name": "run_all.sh"}
        with patch.object(resolver.release, "api_optional", return_value=legacy), \
             patch.object(resolver.release, "gh") as gh:
            self.assertEqual(resolver.candidate_case_names("kuasar-sandbox/connector", "a" * 40, "connector"), [])
        gh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
