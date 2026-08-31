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
            snapshots[aggregate].components["connector"],
            "v1.0.2-preview.20260831",
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
        components = {
            "accelerator": "v1.0.0-preview.20260831",
            "connector": "v1.0.0-preview.20260831",
            "sandboxer": "v1.0.0-preview.20260831",
            "orchestrator": "v1.0.0-preview.20260831",
            "runtime": "runtime-v1.0.0-preview.20260831",
            "vmlinux": "vmlinux-v1.0.0-preview.20260831",
        }
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
        ), mock.patch.object(
            preview_gc, "parse_snapshot", return_value=None
        ), mock.patch.object(
            preview_gc, "first_parent_contains", return_value=True
        ):
            candidate = preview_gc.release_candidate(
                preview_gc.coordinator.PLATFORM_REPOSITORY,
                "platform",
                "release-v1.2.3-preview.20260831",
                platform_release=release,
                platform_sha="5" * 40,
                platform_snapshot=preview_gc.CanonicalSnapshot(
                    "6" * 40, components
                ),
            )
        assert candidate is not None
        self.assertEqual(candidate.source_sha, "5" * 40)

    def test_moved_precontract_tag_is_rejected(self) -> None:
        release = {
            "id": 43,
            "draft": False,
            "prerelease": True,
            "target_commitish": "4" * 40,
            "assets": [],
        }
        snapshot = preview_gc.CanonicalSnapshot("6" * 40, {})
        with self.assertRaisesRegex(preview_gc.GCError, "Release and Tag disagree"):
            preview_gc.validate_platform_ownership(
                "release-v1.2.3-preview.20260831",
                release,
                "5" * 40,
                snapshot,
            )

    def test_retained_precontract_preview_uses_stable_canonical_history(self) -> None:
        tag = "release-v1.2.3-preview.20260831"
        release = {"target_commitish": "5" * 40}
        snapshot = preview_gc.CanonicalSnapshot(
            "6" * 40, {"connector": "v1.0.1-preview.20260831"}
        )
        with (
            mock.patch.object(preview_gc, "parse_snapshot", return_value=None),
            mock.patch.object(
                preview_gc,
                "canonical_snapshots",
                return_value={tag: snapshot},
            ),
            mock.patch.object(preview_gc, "first_parent_contains", return_value=True),
        ):
            selected = preview_gc.retained_snapshot(
                tag,
                release,
                {tag: "5" * 40, "release-v1.2.3": "7" * 40},
                {},
            )
        self.assertEqual(selected, snapshot)

    def test_failed_aggregate_deletion_is_retried_before_third_attempt(self) -> None:
        candidate = preview_gc.Candidate(
            preview_gc.coordinator.PLATFORM_REPOSITORY,
            "platform",
            "release-v1.2.3-preview.20260831",
            "5" * 40,
            43,
            8,
            200,
        )
        failed = {
            "id": 99,
            "status": "completed",
            "conclusion": "failure",
            "run_attempt": 1,
            "html_url": "https://example.invalid/run/99",
        }
        with (
            mock.patch.object(
                preview_gc, "live_manifest_protection", return_value=set()
            ),
            mock.patch.object(
                preview_gc.coordinator, "active_delete_run", return_value=None
            ),
            mock.patch.object(
                preview_gc.coordinator, "latest_run", return_value=failed
            ),
            mock.patch.object(preview_gc.coordinator, "gh") as gh,
        ):
            self.assertFalse(preview_gc.apply([candidate]))
        gh.assert_called_once_with(
            "run",
            "rerun",
            "99",
            "--repo",
            preview_gc.coordinator.PLATFORM_REPOSITORY,
            "--failed",
        )

    def test_cancelled_component_gc_is_retried_as_a_full_workflow(self) -> None:
        candidate = preview_gc.Candidate(
            "kuasar-sandbox/connector",
            "connector",
            "v1.2.3-preview.20260831",
            "5" * 40,
            43,
            2,
            100,
        )
        cancelled = {
            "id": 100,
            "status": "completed",
            "conclusion": "cancelled",
            "run_attempt": 1,
            "html_url": "https://example.invalid/run/100",
        }
        with (
            mock.patch.object(
                preview_gc, "live_manifest_protection", return_value=set()
            ),
            mock.patch.object(
                preview_gc.coordinator, "active_delete_run", return_value=None
            ),
            mock.patch.object(
                preview_gc.coordinator, "latest_run", return_value=cancelled
            ),
            mock.patch.object(preview_gc.coordinator, "gh") as gh,
        ):
            self.assertFalse(preview_gc.apply([candidate]))
        gh.assert_called_once_with(
            "run",
            "rerun",
            "100",
            "--repo",
            "kuasar-sandbox/connector",
        )

    def test_failed_aggregate_deletion_stops_after_third_attempt(self) -> None:
        candidate = preview_gc.Candidate(
            preview_gc.coordinator.PLATFORM_REPOSITORY,
            "platform",
            "release-v1.2.3-preview.20260831",
            "5" * 40,
            43,
            8,
            200,
        )
        failed = {
            "id": 99,
            "status": "completed",
            "conclusion": "failure",
            "run_attempt": 3,
            "html_url": "https://example.invalid/run/99",
        }
        with (
            mock.patch.object(
                preview_gc, "live_manifest_protection", return_value=set()
            ),
            mock.patch.object(
                preview_gc.coordinator, "active_delete_run", return_value=None
            ),
            mock.patch.object(
                preview_gc.coordinator, "latest_run", return_value=failed
            ),
            mock.patch.object(preview_gc.coordinator, "gh") as gh,
            self.assertRaisesRegex(preview_gc.GCError, "aggregate GC failed"),
        ):
            preview_gc.apply([candidate])
        gh.assert_not_called()

    def test_apply_rechecks_live_manifest_protection_before_dispatch(self) -> None:
        candidate = preview_gc.Candidate(
            "kuasar-sandbox/connector",
            "connector",
            "v1.2.3-preview.20260831",
            "5" * 40,
            43,
            2,
            200,
        )
        with (
            mock.patch.object(
                preview_gc,
                "live_manifest_protection",
                return_value={(candidate.unit, candidate.tag)},
            ),
            mock.patch.object(preview_gc, "dispatch_component") as dispatch,
            self.assertRaisesRegex(preview_gc.GCError, "now protects"),
        ):
            preview_gc.apply([candidate])
        dispatch.assert_not_called()

    def test_successful_old_component_gc_is_redispatched(self) -> None:
        candidate = preview_gc.Candidate(
            "kuasar-sandbox/connector",
            "connector",
            "v1.2.3-preview.20260831",
            "5" * 40,
            43,
            2,
            200,
        )
        completed = {
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
        }
        with (
            mock.patch.object(
                preview_gc, "live_manifest_protection", return_value=set()
            ),
            mock.patch.object(
                preview_gc.coordinator, "active_delete_run", return_value=None
            ),
            mock.patch.object(
                preview_gc.coordinator, "latest_run", return_value=completed
            ),
            mock.patch.object(preview_gc, "dispatch_component") as dispatch,
        ):
            self.assertFalse(preview_gc.apply([candidate]))
        dispatch.assert_called_once_with(candidate)

    def test_successful_old_aggregate_gc_is_redispatched(self) -> None:
        candidate = preview_gc.Candidate(
            preview_gc.coordinator.PLATFORM_REPOSITORY,
            "platform",
            "release-v1.2.3-preview.20260831",
            "5" * 40,
            43,
            8,
            200,
        )
        completed = {
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
        }
        with (
            mock.patch.object(
                preview_gc.coordinator, "active_delete_run", return_value=None
            ),
            mock.patch.object(
                preview_gc.coordinator, "latest_run", return_value=completed
            ),
            mock.patch.object(preview_gc.coordinator, "gh") as gh,
        ):
            self.assertFalse(preview_gc.apply([candidate]))
        self.assertEqual(
            gh.call_args.args[:3], ("workflow", "run", "delete-preview.yml")
        )


if __name__ == "__main__":
    unittest.main()
