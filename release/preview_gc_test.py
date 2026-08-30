#!/usr/bin/env python3

"""Unit tests for deterministic Preview GC planning primitives."""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import sys
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).with_name("preview_gc.py")
SPEC = importlib.util.spec_from_file_location("preview_gc", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
preview_gc = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preview_gc
SPEC.loader.exec_module(preview_gc)


class PreviewGCTest(unittest.TestCase):
    def test_plan_digest_is_stable_and_includes_tag_commit(self) -> None:
        first = preview_gc.Candidate(
            "kuasar-sandbox/accelerator",
            "accelerator",
            "v1.2.3-preview.20260831",
            "1" * 40,
            42,
            2,
            100,
        )
        second = preview_gc.Candidate(
            "kuasar-sandbox/kuasar-sandbox",
            "platform",
            "release-v9.8.7-preview.20260831",
            "2" * 40,
            43,
            8,
            200,
        )
        value = preview_gc.digest((first, second))
        self.assertRegex(value, r"^[0-9a-f]{64}$")
        self.assertEqual(value, preview_gc.digest((first, second)))
        self.assertNotEqual(
            value,
            preview_gc.digest(
                (
                    first.__class__(
                        first.repository,
                        first.unit,
                        first.tag,
                        "3" * 40,
                        first.release_id,
                        first.asset_count,
                        first.asset_bytes,
                    ),
                    second,
                )
            ),
        )

    def test_grace_period_cannot_be_bypassed(self) -> None:
        recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
        elapsed, eligible = preview_gc.grace_elapsed(recent)
        self.assertFalse(elapsed)
        self.assertGreater(eligible, dt.datetime.now(dt.timezone.utc))

    def test_canonical_history_keeps_newest_selection_for_same_preview(self) -> None:
        aggregate = "release-v1.2.3-preview.20260831"
        with (
            mock.patch.object(preview_gc, "git", return_value="newer\nolder"),
            mock.patch.object(
                preview_gc,
                "parse_snapshot",
                side_effect=(
                    (aggregate, {"connector": "v1.0.2-preview.20260831"}),
                    (aggregate, {"connector": "v1.0.1-preview.20260831"}),
                ),
            ),
        ):
            snapshots = preview_gc.canonical_snapshots("f" * 40, "release-v1.2.3")
        self.assertEqual(
            snapshots[aggregate]["connector"], "v1.0.2-preview.20260831"
        )

    def test_missing_release_and_tag_is_already_converged(self) -> None:
        unit = preview_gc.coordinator.UNIT_BY_NAME["connector"]
        state = mock.Mock()
        state.status.return_value = preview_gc.coordinator.ReleaseStatus(
            None, None, False
        )
        self.assertIsNone(
            preview_gc.release_candidate(
                unit.repository,
                unit.name,
                "v1.2.3-preview.20260831",
                state,
            )
        )

    def test_tagless_incomplete_preview_is_collectable_by_release_target(self) -> None:
        unit = preview_gc.coordinator.UNIT_BY_NAME["connector"]
        state = mock.Mock()
        state.status.return_value = preview_gc.coordinator.ReleaseStatus(
            {
                "id": 42,
                "draft": True,
                "prerelease": True,
                "target_commitish": "4" * 40,
                "assets": [{"name": "partial", "size": 17}],
            },
            None,
            False,
        )
        candidate = preview_gc.release_candidate(
            unit.repository,
            unit.name,
            "v1.2.3-preview.20260831",
            state,
        )
        assert candidate is not None
        self.assertEqual(candidate.source_sha, "4" * 40)
        self.assertEqual(candidate.asset_count, 1)

    def test_precontract_canonical_aggregate_remains_collectable(self) -> None:
        release = {
            "id": 43,
            "draft": False,
            "prerelease": True,
            "target_commitish": "5" * 40,
            "assets": [{"name": "legacy", "size": 19}],
        }
        with mock.patch.object(
            preview_gc.coordinator,
            "platform_asset_names",
            side_effect=preview_gc.coordinator.Deferred("legacy manifest"),
        ):
            candidate = preview_gc.release_candidate(
                preview_gc.coordinator.PLATFORM_REPOSITORY,
                "platform",
                "release-v1.2.3-preview.20260831",
                platform_release=release,
                platform_sha="5" * 40,
            )
        assert candidate is not None
        self.assertEqual(candidate.source_sha, "5" * 40)


if __name__ == "__main__":
    unittest.main()
