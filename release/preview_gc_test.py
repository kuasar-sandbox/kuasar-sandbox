#!/usr/bin/env python3

"""Unit tests for deterministic Preview GC planning primitives."""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import pathlib
import sys
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).with_name("preview_gc.py")
SPEC = importlib.util.spec_from_file_location("preview_gc", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
preview_gc = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preview_gc
SPEC.loader.exec_module(preview_gc)


class PreviewGCTest(unittest.TestCase):
    def test_complete_dual_release_is_not_incomplete_recovery_material(self) -> None:
        version, sha = "release-v1.2.3-preview.20260831", "a" * 40
        components = {unit: (unit + "-" if unit in ("runtime", "vmlinux") else "") + "v1.0.0"
                      for unit in preview_gc.coordinator.selection.UNITS}
        old = preview_gc.coordinator.platform_asset_names_for_components(version, components)
        dual = preview_gc.coordinator.platform_asset_names_for_components(version, components, ("x86_64", "aarch64"))
        with mock.patch.object(preview_gc.coordinator, "platform_asset_names", return_value=old):
            for names in (old, dual):
                release = {"draft": False, "prerelease": True, "target_commitish": sha,
                           "assets": [{"name": name, "state": "uploaded"} for name in names]}
                self.assertTrue(preview_gc.platform_release_status(version, release, sha).complete)
            release["assets"].pop()
            self.assertFalse(preview_gc.platform_release_status(version, release, sha).complete)

    def test_stable_release_accepts_same_tree_legacy_target(self) -> None:
        version = "release-v1.2.3"
        tag_sha = "2" * 40
        target_sha = "1" * 40
        release = {
            "target_commitish": target_sha,
            "prerelease": False,
            "assets": [{"digest": "sha256:" + "3" * 64, "size": 1}],
        }

        def status(_version, item, sha):
            return preview_gc.coordinator.ReleaseStatus(
                item, sha, item.get("target_commitish") == tag_sha
            )

        with (
            mock.patch.object(preview_gc, "platform_release_status", side_effect=status),
            mock.patch.object(
                preview_gc, "commit_tree", side_effect=["same-tree", "same-tree"]
            ),
            mock.patch.object(preview_gc, "git", return_value=tag_sha),
        ):
            self.assertIs(
                preview_gc.stable_release(version, {version: release}, {version: tag_sha}),
                release,
            )

    def test_stable_release_rejects_different_tree_legacy_target(self) -> None:
        version = "release-v1.2.3"
        tag_sha = "2" * 40
        release = {
            "target_commitish": "1" * 40,
            "prerelease": False,
            "assets": [{"digest": "sha256:" + "3" * 64, "size": 1}],
        }
        incomplete = preview_gc.coordinator.ReleaseStatus(release, tag_sha, False)
        with (
            mock.patch.object(preview_gc, "platform_release_status", return_value=incomplete),
            mock.patch.object(
                preview_gc, "commit_tree", side_effect=["target-tree", "tag-tree"]
            ),
            self.assertRaisesRegex(preview_gc.GCError, "violates the release contract"),
        ):
            preview_gc.stable_release(version, {version: release}, {version: tag_sha})

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

    def test_canonical_history_keeps_all_selections_newest_first(self) -> None:
        aggregate = "release-v1.2.3-preview.20260831"
        with (
            mock.patch.object(preview_gc, "git", return_value="newer\nolder"),
            mock.patch.object(
                preview_gc,
                "parse_snapshot",
                side_effect=(
                    (aggregate, {"connector": "v1.0.2-preview.20260831"}, "historical"),
                    (aggregate, {"connector": "v1.0.1-preview.20260831"}, "historical"),
                ),
            ),
        ):
            snapshots = preview_gc.canonical_snapshots("f" * 40, "release-v1.2.3")
        self.assertEqual([snapshot.commit for snapshot in snapshots[aggregate]], ["newer", "older"])
        self.assertEqual(
            [snapshot.components["connector"] for snapshot in snapshots[aggregate]],
            ["v1.0.2-preview.20260831", "v1.0.1-preview.20260831"],
        )

    def test_numbered_previews_remain_distinct_canonical_gc_candidates(self) -> None:
        tags = [
            "release-v1.2.3-preview.20260914",
            "release-v1.2.3-preview.20260914.1",
            "release-v1.2.3-preview.20260914.10",
        ]
        with (
            mock.patch.object(preview_gc, "git", return_value="third\nsecond\nfirst"),
            mock.patch.object(preview_gc, "parse_snapshot", side_effect=[(tag, {}, "historical") for tag in reversed(tags)]),
        ):
            snapshots = preview_gc.canonical_snapshots("f" * 40, "release-v1.2.3")
        self.assertEqual(set(snapshots), set(tags))
        for tag in tags:
            self.assertIsNotNone(preview_gc.AGGREGATE_PREVIEW_RE.fullmatch(tag))
        self.assertIsNone(preview_gc.AGGREGATE_PREVIEW_RE.fullmatch("release-v1.2.3"))

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
                return_value={tag: [snapshot]},
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



class CanonicalSourceHistoryTest(unittest.TestCase):
    """Real manifest/first-parent histories; only public transport is simulated."""

    version = "release-v1.2.3"
    preview = version + "-preview.20260831"
    old_component = "v1.0.1-preview.20260831"
    new_component = "v1.0.2-preview.20260831"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gc-history-")
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.run_git("init", "-q")
        self.run_git("config", "user.name", "GC fixture")
        self.run_git("config", "user.email", "gc@example.invalid")
        self.run_git("config", "commit.gpgsign", "false")
        root_patch = mock.patch.object(preview_gc, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        (self.root / "README").write_text("Initial history\n")
        self.initial = self.commit("Before maintained selections")

    def run_git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.root), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    def commit(self, message: str) -> str:
        self.run_git("add", ".")
        self.run_git("commit", "-qm", message)
        return self.run_git("rev-parse", "HEAD")

    def selection(self, component: str, *, legacy: bool = False,
                  tag: str | None = None) -> str:
        tag = tag or self.preview
        version, suffix = tag.split("-preview.")
        units = preview_gc.coordinator.selection.UNITS
        components = {unit: (unit + "-" if unit in ("runtime", "vmlinux") else "") + "v1.0.0"
                      for unit in units}
        components["connector"] = component
        text = f"version: {version}\npreview_version: preview.{suffix}\ncomponents:\n"
        text += "".join(f"  {unit}: {value}\n" for unit, value in components.items())
        if legacy:
            text += "test_revisions:\n" + "".join(
                f"  {owner}: {index:040x}\n" for index, owner in
                enumerate(preview_gc.coordinator.selection.TEST_OWNERS, 1))
        path = self.root / "releases/daily-preview.yaml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        return self.commit("Maintained Preview selection")

    def stable(self) -> str:
        (self.root / "README").write_text("Stable publication\n")
        sha = self.commit("Stable")
        self.run_git("tag", self.version)
        return sha

    def release(self, tag: str, sha: str, identity: int = 42) -> dict:
        return dict(id=identity, tag_name=tag, target_commitish=sha, draft=False,
                    prerelease="-preview." in tag, published_at="2026-09-01T00:00:00Z",
                    assets=[dict(name="archive", state="uploaded", size=1, digest="sha256:" + "1" * 64)])

    @contextmanager
    def public_state(self, stable_sha: str, source: str, retained: tuple | None = None):
        tags = {self.version: stable_sha, self.preview: source}
        releases = [self.release(self.version, stable_sha), self.release(self.preview, source)]
        if retained:
            tag, sha = retained
            tags[tag] = sha
            releases.append(self.release(tag, sha, 44))
        state = mock.Mock()
        state.status.side_effect = lambda tag: preview_gc.coordinator.ReleaseStatus(
            self.release(tag, "a" * 40, 43), "a" * 40, True)
        with (
            mock.patch.object(preview_gc, "tag_index", return_value=tags),
            mock.patch.object(preview_gc.coordinator, "paginated", return_value=releases),
            mock.patch.object(preview_gc.coordinator, "platform_asset_names", return_value={"archive"}),
            mock.patch.object(preview_gc.coordinator, "RepositoryState", return_value=state),
            mock.patch.object(preview_gc, "current_manifest_protection", return_value=set()),
            mock.patch.object(preview_gc, "active_release_titles", return_value=set()),
        ):
            yield

    def test_validation_titles_do_not_reserve_preview_gc_candidates(self) -> None:
        source = self.selection(self.old_component)
        self.run_git('tag', self.preview, source)
        stable = self.stable()
        title = preview_gc.coordinator.aggregate_run_title(self.preview, source)
        for status in ('queued', 'pending', 'in_progress'):
            rows = [dict(display_title=title + ' [validation only]', status=status),
                    dict(display_title=title.replace('Aggregate ', 'Validate aggregate ', 1), status=status)]
            with mock.patch.object(preview_gc.coordinator, 'paginated', return_value=rows):
                validation_titles = preview_gc.active_release_titles(
                    preview_gc.coordinator.PLATFORM_REPOSITORY, 'aggregate-release.yml')
            self.assertEqual(len(validation_titles), 2)
            for ordinary in (False, True):
                titles = validation_titles | ({title} if ordinary else set())
                with self.subTest(status=status, ordinary=ordinary), self.public_state(stable, source), \
                     mock.patch.object(preview_gc, 'active_release_titles', side_effect=lambda repo, workflow:
                                       titles if workflow == 'aggregate-release.yml' else set()), \
                     mock.patch.object(preview_gc.coordinator, 'gh') as mutation:
                    if ordinary:
                        with self.assertRaisesRegex(preview_gc.GCError, 'aggregate release workflow is active'):
                            preview_gc.plan(self.version)
                    else:
                        candidates, _ = preview_gc.plan(self.version)
                        self.assertIn(self.preview, {row.tag for row in candidates})
                    mutation.assert_not_called()

    def test_metadata_cleanup_after_tag_plans_original_history_in_dry_run(self) -> None:
        source = self.selection(self.old_component, legacy=True)
        self.run_git("tag", self.preview, source)
        original = self.run_git("show", source + ":releases/daily-preview.yaml")
        cleanup = self.selection(self.old_component)
        stable = self.stable()
        history = preview_gc.canonical_snapshots(stable, self.version)
        self.assertEqual([row.commit for row in history[self.preview]], [cleanup, source])
        release = self.release(self.preview, source)
        selected = preview_gc.resolve_canonical_snapshot(self.preview, release, source, history[self.preview])
        self.assertEqual(selected.commit, source)
        self.assertEqual(self.run_git("show", selected.commit + ":releases/daily-preview.yaml"), original)
        self.assertIn("test_revisions:", original)
        with self.public_state(stable, source):
            candidates, _ = preview_gc.plan(self.version)
            self.assertEqual({row.tag for row in candidates}, {self.old_component, self.preview})
            output = io.StringIO()
            with (
                mock.patch.dict(preview_gc.os.environ, {"DRY_RUN": "true", "STABLE_VERSION": self.version}),
                mock.patch.object(preview_gc, "apply") as apply,
                mock.patch.object(preview_gc.coordinator, "gh") as mutation,
                mock.patch.object(preview_gc.time, "sleep") as sleep,
                redirect_stdout(output),
            ):
                preview_gc.main()
            self.assertIn("eligible_at", output.getvalue())
            self.assertIn(self.preview, output.getvalue())
            apply.assert_not_called()
            mutation.assert_not_called()
            sleep.assert_not_called()

    def test_latest_reachable_product_selection_wins_before_tag(self) -> None:
        older = self.selection(self.old_component)
        selected = self.selection(self.new_component)
        (self.root / "README").write_text("Release source after selection\n")
        source = self.commit("Publish source")
        self.run_git("tag", self.preview, source)
        stable = self.stable()
        history = preview_gc.canonical_snapshots(stable, self.version)[self.preview]
        self.assertEqual([row.commit for row in history], [selected, older])
        result = preview_gc.resolve_canonical_snapshot(self.preview, self.release(self.preview, source), source, history)
        self.assertEqual(result.commit, selected)
        self.assertEqual(result.components["connector"], self.new_component)

    def test_post_tag_product_change_is_not_old_release_or_retained_dependency(self) -> None:
        source = self.selection(self.old_component, legacy=True)
        self.run_git("tag", self.preview, source)
        self.selection(self.new_component)
        stable = self.stable()
        release = self.release(self.preview, source)
        tags = {self.version: stable, self.preview: source}
        history = preview_gc.canonical_snapshots(stable, self.version)[self.preview]
        candidate = preview_gc.resolve_canonical_snapshot(self.preview, release, source, history)
        retained = preview_gc.retained_snapshot(self.preview, release, tags, {})
        self.assertEqual(candidate, retained)
        self.assertEqual(retained.commit, source)
        protected = preview_gc.protected_component_tags({self.preview: release}, tags, set())
        self.assertEqual(protected, {("connector", self.old_component)})
        with self.public_state(stable, source):
            candidates, _ = preview_gc.plan(self.version)
            self.assertEqual({row.tag for row in candidates}, {self.old_component, self.preview})
        # A retained later release still protects the older shared component.
        later = "release-v1.2.4-preview.20260901"
        later_sha = self.selection(self.old_component, tag=later)
        with self.public_state(stable, source, (later, later_sha)):
            candidates, metadata = preview_gc.plan(self.version)
            self.assertEqual({row.tag for row in candidates}, {self.preview})
            self.assertEqual(metadata["protected_component_count"], 1)

    def test_source_ownership_and_no_match_still_fail(self) -> None:
        source = self.selection(self.old_component)
        cleanup = self.selection(self.old_component, legacy=True)
        stable = self.stable()
        history = preview_gc.canonical_snapshots(stable, self.version)[self.preview]
        release = self.release(self.preview, source)
        outside = self.run_git("commit-tree", self.run_git("rev-parse", source + "^{tree}"), "-m", "Unrelated root")
        wrong_version = self.selection(self.old_component, tag="release-v1.2.3-preview.20260901")
        for label, row, sha, choices, error in [
            ("off-canonical", self.release(self.preview, outside), outside, history, "canonical history"),
            ("moved tag", release, cleanup, history, "Release and Tag disagree"),
            ("wrong Release target", self.release(self.preview, outside), source, history, "Release and Tag disagree"),
            ("wrong version", self.release(self.preview, wrong_version), wrong_version, history, "another manifest"),
            ("no reachable snapshot", release, source, [history[0]], "canonical history"),
            ("missing history", release, source, [], "no canonical snapshot"),
        ]:
            with self.subTest(label=label), self.assertRaisesRegex(preview_gc.GCError, error):
                preview_gc.resolve_canonical_snapshot(self.preview, row, sha, choices)

    def test_absent_release_only_and_tag_only_preserve_their_boundaries(self) -> None:
        source = self.selection(self.old_component)
        newest = self.selection(self.new_component)
        history = preview_gc.canonical_snapshots(self.stable(), self.version)[self.preview]
        release = self.release(self.preview, source)
        for row in (None, release):
            selected = preview_gc.resolve_canonical_snapshot(self.preview, row, None, history)
            self.assertEqual(selected.commit, newest)
            candidate = preview_gc.release_candidate(preview_gc.coordinator.PLATFORM_REPOSITORY, "platform",
                self.preview, platform_release=row, platform_snapshot=selected)
            if row is None:
                self.assertIsNone(candidate)
            else:
                self.assertEqual(candidate.source_sha, source)
        selected = preview_gc.resolve_canonical_snapshot(self.preview, None, source, history)
        self.assertEqual(selected.commit, source)
        candidate = preview_gc.release_candidate(preview_gc.coordinator.PLATFORM_REPOSITORY, "platform",
            self.preview, platform_sha=source, platform_snapshot=selected)
        self.assertEqual(candidate.source_sha, source)
        self.assertIsNone(candidate.release_id)

    def test_pre_manifest_resolution_keeps_existing_narrow_rule(self) -> None:
        self.selection(self.old_component)
        newest = self.selection(self.new_component)
        history = preview_gc.canonical_snapshots(self.stable(), self.version)[self.preview]
        release = self.release(self.preview, self.initial)
        selected = preview_gc.resolve_canonical_snapshot(self.preview, release, self.initial, history)
        self.assertEqual(selected.commit, newest)
        outside = self.run_git("commit-tree", self.run_git("rev-parse", self.initial + "^{tree}"), "-m", "Unrelated pre-manifest root")
        with self.assertRaisesRegex(preview_gc.GCError, "canonical history"):
            preview_gc.resolve_canonical_snapshot(self.preview, self.release(self.preview, outside), outside, history)

if __name__ == "__main__":
    unittest.main()
