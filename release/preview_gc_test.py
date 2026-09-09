#!/usr/bin/env python3

"""Unit tests for deterministic Preview GC planning primitives."""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import pathlib
import sys
import unittest
from contextlib import redirect_stdout
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


class PreviewGCConvergenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.version = "release-v1.2.3"
        self.published = "2020-01-01T00:00:00Z"
        self.first = preview_gc.Candidate(
            preview_gc.coordinator.PLATFORM_REPOSITORY,
            "platform",
            f"{self.version}-preview.20260830",
            "5" * 40, 43, 8, 200,
        )
        self.second = preview_gc.Candidate(
            self.first.repository, "platform",
            f"{self.version}-preview.20260831",
            "6" * 40, 44, 8, 200,
        )
        self.component = preview_gc.Candidate(
            "kuasar-sandbox/connector", "connector",
            "v1.2.3-preview.20260830", "7" * 40, 45, 2, 100,
        )
        self.metadata = {
            "stable_version": self.version,
            "stable_sha": "8" * 40,
            "published_at": self.published,
        }
        self.clock = 0.0
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        self.enterContext(mock.patch.dict(
            preview_gc.os.environ, {"STABLE_VERSION": "", "DRY_RUN": "false"}
        ))
        self.versions = self.enterContext(mock.patch.object(
            preview_gc, "stable_versions", return_value=[self.version]
        ))
        self.published_at = self.enterContext(mock.patch.object(
            preview_gc, "stable_published_at", return_value=self.published
        ))
        self.enterContext(mock.patch.object(
            preview_gc.time, "monotonic", side_effect=lambda: self.clock
        ))
        self.sleep = self.enterContext(mock.patch.object(
            preview_gc.time, "sleep", side_effect=self.advance
        ))

    def advance(self, seconds: float) -> None:
        self.clock += seconds

    def test_waits_for_component_and_all_aggregates_then_verifies_zero(self) -> None:
        phases = [
            [self.component, self.first, self.second],
            [self.component, self.first, self.second],
            [self.first, self.second],
            [self.first, self.second],
            [self.second],
            [],
        ]
        phase = -1
        mutations = []

        def plan(version):
            nonlocal phase
            self.assertEqual(version, self.version)
            phase += 1
            return phases[phase], self.metadata

        def active(repository, tag):
            if (phase, tag) in ((1, self.component.tag), (3, self.first.tag)):
                return {"html_url": "https://example.invalid/active"}
            return None

        def dispatch_aggregate(*args):
            self.assertEqual(args[:3], ("workflow", "run", "delete-preview.yml"))
            mutations.append(next(arg for arg in args if arg.startswith("version=")))

        with (
            mock.patch.object(preview_gc, "plan", side_effect=plan),
            mock.patch.object(preview_gc, "live_manifest_protection", return_value=set()),
            mock.patch.object(preview_gc.coordinator, "active_delete_run", side_effect=active),
            mock.patch.object(preview_gc.coordinator, "latest_run", return_value=None),
            mock.patch.object(preview_gc, "dispatch_component", side_effect=lambda item: mutations.append(item.tag)),
            mock.patch.object(preview_gc.coordinator, "gh", side_effect=dispatch_aggregate),
        ):
            preview_gc.main()
        self.assertEqual(mutations, [
            self.component.tag, f"version={self.first.tag}", f"version={self.second.tag}"
        ])
        self.assertEqual(phase, 5)
        self.assertEqual(self.sleep.call_count, 5)
        self.assertEqual(self.output.getvalue().count("Preview GC converged"), 1)

    def test_dry_run_plans_each_version_once_without_dispatch_or_wait(self) -> None:
        self.versions.return_value = [self.version, "release-v1.2.4"]
        with (
            mock.patch.dict(preview_gc.os.environ, {"DRY_RUN": "true"}),
            mock.patch.object(preview_gc, "plan", return_value=([self.first], self.metadata)) as plan,
            mock.patch.object(preview_gc, "apply") as apply,
        ):
            preview_gc.main()
        self.assertEqual(plan.call_args_list, [mock.call(self.version), mock.call("release-v1.2.4")])
        self.published_at.assert_not_called()
        apply.assert_not_called()
        self.sleep.assert_not_called()

    def test_pending_deletion_times_out_instead_of_reporting_success(self) -> None:
        with (
            mock.patch.object(preview_gc, "WAIT_SECONDS", 45),
            mock.patch.object(preview_gc, "plan", return_value=([self.first], self.metadata)),
            mock.patch.object(preview_gc, "apply", return_value=False) as apply,
            self.assertRaisesRegex(preview_gc.GCError, "timed out with pending candidates"),
        ):
            preview_gc.main()
        self.assertEqual(apply.call_count, 2)
        self.assertEqual(self.clock, 45)
        self.assertNotIn("Preview GC converged", self.output.getvalue())

    def test_replanning_error_stops_without_another_dispatch(self) -> None:
        with (
            mock.patch.object(preview_gc, "plan", side_effect=[
                ([self.first], self.metadata), preview_gc.GCError("ownership changed")
            ]),
            mock.patch.object(preview_gc, "apply", return_value=False) as apply,
            self.assertRaisesRegex(preview_gc.GCError, "ownership changed"),
        ):
            preview_gc.main()
        apply.assert_called_once_with([self.first])
        self.assertNotIn("Preview GC converged", self.output.getvalue())

    def test_grace_period_is_rechecked_before_every_apply(self) -> None:
        changed = dict(self.metadata, published_at=dt.datetime.now(dt.timezone.utc).isoformat())
        with (
            mock.patch.object(preview_gc, "plan", side_effect=[
                ([self.first], self.metadata), ([self.first], changed)
            ]),
            mock.patch.object(preview_gc, "apply", return_value=False) as apply,
            self.assertRaisesRegex(preview_gc.GCError, "publication timestamp changed"),
        ):
            preview_gc.main()
        apply.assert_called_once_with([self.first])

    def test_wait_budget_is_shared_across_stable_versions(self) -> None:
        second_version = "release-v1.2.4"
        self.versions.return_value = [self.version, second_version]
        with (
            mock.patch.object(preview_gc, "WAIT_SECONDS", 30),
            mock.patch.object(preview_gc, "plan", side_effect=[
                ([self.first], self.metadata), ([], self.metadata),
                ([self.second], dict(self.metadata, stable_version=second_version)),
            ]),
            mock.patch.object(preview_gc, "apply", return_value=False) as apply,
            self.assertRaisesRegex(preview_gc.GCError, f"pending candidates for {second_version}"),
        ):
            preview_gc.main()
        apply.assert_called_once_with([self.first])
        self.assertEqual(self.output.getvalue().count("Preview GC converged"), 1)


if __name__ == "__main__":
    unittest.main()
