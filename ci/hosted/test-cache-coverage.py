#!/usr/bin/env python3
"""Cache completeness isolation across the actual Workbench callers."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("cache_coverage", Path(__file__).with_name("cache-coverage.py"))
SUBJECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUBJECT)


class Coverage(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cache-coverage-")
        self.addCleanup(temporary.cleanup)
        self.sources = Path(temporary.name)
        (self.sources / ".ci").mkdir()
        self.plan = {"owners": ["sandboxer"], "plan_id": "first", "run_id": "100",
                     "lanes": {arch: {"products": ["sandbox-ctl"], "embedded_products": [],
                                       "selection": {"cases": ["sandbox.pause.sh"]}}
                               for arch in SUBJECT.artifacts.ARCHES}}
        self.write_plan()

    def write_plan(self):
        (self.sources / ".ci/plan.json").write_text(json.dumps(self.plan))

    def coverage(self, name, arch="x86_64"):
        return SUBJECT.digest(SUBJECT.identity(name, self.sources, arch))

    def test_helpers_and_deltas_cannot_occupy_release_key_or_restore_prefix(self):
        # Same source scope and recipe/toolchain digest reproduce the reported
        # Actions collision. An immutable key retains only the first save.
        prefix = "workbench-v2-x86_64-trusted-"
        cache = {}
        for name, payload in (("integration-delta", {"go"}), ("aggregate-helpers", {"go", "cargo"}),
                              ("release-sandboxer", {"go", "cargo", "cloud-hypervisor"})):
            key = prefix + self.coverage(name) + "-same-recipes"
            cache.setdefault(key, payload)
        full_prefix = prefix + self.coverage("release-sandboxer") + "-"
        restored = [payload for key, payload in cache.items() if key.startswith(full_prefix)]
        self.assertEqual(restored, [{"go", "cargo", "cloud-hypervisor"}])
        self.assertEqual(len(cache), 3)

    def test_every_concrete_producer_has_a_distinct_coverage(self):
        values = [self.coverage(name) for name in SUBJECT.KINDS]
        self.assertEqual(len(set(values)), len(SUBJECT.KINDS))
        self.assertTrue(all(len(value) == 64 for value in values))

    def test_delta_binds_products_embedded_builds_and_actual_helper_selection(self):
        before = self.coverage("integration-delta")
        variants = []
        for field, value in (("products", ["sandbox-ctl", "cloud-hypervisor"]),
                             ("embedded_products", ["envd"]),
                             ("selection", {"cases": ["sandbox.pause.sh", "sandbox.cgroup.sh"]})):
            plan = copy.deepcopy(self.plan)
            plan["lanes"]["x86_64"][field] = value
            variants.append(plan)
        for self.plan in variants:
            self.write_plan()
            self.assertNotEqual(self.coverage("integration-delta"), before)

    def test_delta_does_not_expand_or_hash_cases_that_add_no_compilation(self):
        before = SUBJECT.identity("integration-delta", self.sources, "x86_64")
        self.plan["lanes"]["x86_64"]["selection"]["cases"].append("sandbox.resources.sh")
        self.write_plan()
        after = SUBJECT.identity("integration-delta", self.sources, "x86_64")
        self.assertEqual(before, after)
        self.assertEqual(after["products"], ["sandbox-ctl"])
        self.assertEqual(after["helpers"], {"zot": "framework", "usage-probe": "sandboxer"})

    def test_delta_ignores_other_architecture_and_volatile_plan_fields(self):
        before = self.coverage("integration-delta")
        self.plan.update(plan_id="second", run_id="200", version="next-daily", framework_sha="f" * 40)
        self.plan["lanes"]["aarch64"]["products"].append("vmlinux")
        self.plan["lanes"]["x86_64"]["performance"] = ["additional-existing-gate"]
        self.write_plan()
        self.assertEqual(self.coverage("integration-delta"), before)

    def test_aggregate_binds_existing_producer_without_duplicating_its_helper_list(self):
        before = self.coverage("aggregate-helpers")
        with patch.object(SUBJECT.artifacts, "digest", return_value="b" * 64):
            self.assertNotEqual(self.coverage("aggregate-helpers"), before)
        value = SUBJECT.identity("aggregate-helpers", self.sources, "x86_64")
        self.assertEqual(set(value["recipes"]), {"release/build-e2e-helpers.py", "ci/integration/build_demo_wheels.py"})

    def test_unknown_or_missing_coverage_fails_closed(self):
        for name in ("", "all", "daily-20261009", "run-100"):
            with self.assertRaisesRegex(ValueError, "unknown Workbench cache coverage"):
                self.coverage(name)
        with self.assertRaisesRegex(ValueError, "architecture"):
            self.coverage("release-sandboxer", "arm64")
        (self.sources / ".ci/plan.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.coverage("integration-delta")


if __name__ == "__main__":
    unittest.main()
