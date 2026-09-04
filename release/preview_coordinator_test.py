#!/usr/bin/env python3

"""Unit tests for Daily Preview convergence state transitions."""

from __future__ import annotations

import base64
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).with_name("preview_coordinator.py")
SPEC = importlib.util.spec_from_file_location("preview_coordinator", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
coordinator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = coordinator
SPEC.loader.exec_module(coordinator)

FORMAL_PATH = pathlib.Path(__file__).with_name("formal_coordinator.py")
FORMAL_SPEC = importlib.util.spec_from_file_location(
    "formal_coordinator_test_subject", FORMAL_PATH
)
assert FORMAL_SPEC is not None and FORMAL_SPEC.loader is not None
formal = importlib.util.module_from_spec(FORMAL_SPEC)
sys.modules[FORMAL_SPEC.name] = formal
FORMAL_SPEC.loader.exec_module(formal)


class PreviewCoordinatorTest(unittest.TestCase):
    @staticmethod
    def binding_body(
        unit: str, source_sha: str, dependencies: str = ""
    ) -> str:
        value = json.dumps(
            {
                "aggregate_sha": "f" * 40,
                "aggregate_version": "release-v9.8.7-preview.20260831",
                "dependencies": dependencies,
                "source_ref": "main",
                "source_sha": source_sha,
                "unit": unit,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"<!-- kuasar-preview-binding {value} -->"

    def test_branch_sha_resolves_only_heads_namespace(self) -> None:
        with mock.patch.object(
            coordinator,
            "api_optional",
            return_value={"object": {"type": "commit", "sha": "a" * 40}},
        ) as query:
            sha = coordinator.branch_sha(
                "kuasar-sandbox/sandboxer", "release/v0.3.x"
            )
        self.assertEqual(sha, "a" * 40)
        query.assert_called_once_with(
            "repos/kuasar-sandbox/sandboxer/git/ref/heads/release/v0.3.x"
        )

    def test_branch_sha_rejects_non_commit_ref(self) -> None:
        with (
            mock.patch.object(
                coordinator,
                "api_optional",
                return_value={"object": {"type": "tag", "sha": "a" * 40}},
            ),
            self.assertRaisesRegex(RuntimeError, "not a commit branch ref"),
        ):
            coordinator.branch_sha("kuasar-sandbox/sandboxer", "main")

    def test_clone_fetch_allows_updating_attached_default_branch(self) -> None:
        unit = coordinator.UNIT_BY_NAME["accelerator"]
        with (
            mock.patch.dict(
                coordinator.os.environ,
                {"GH_TOKEN": "test-component-token", "GIT_CONFIG_COUNT": "0"},
            ),
            mock.patch.object(coordinator, "gh"),
            mock.patch.object(coordinator, "run") as run,
        ):
            coordinator.clone_repository(unit, "main", pathlib.Path("unused"))
        command = run.call_args.args[0]
        self.assertIn("--update-head-ok", command)
        self.assertIn("refs/heads/main:refs/heads/main", command)
        self.assertNotIn("test-component-token", " ".join(command))
        environment = run.call_args.kwargs["environment"]
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(
            environment["GIT_CONFIG_KEY_0"],
            "http.https://github.com/.extraheader",
        )
        header = environment["GIT_CONFIG_VALUE_0"]
        encoded = header.removeprefix("AUTHORIZATION: basic ")
        self.assertEqual(
            base64.b64decode(encoded).decode(),
            "x-access-token:test-component-token",
        )
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")

    def test_clone_requires_component_git_token_before_fetch(self) -> None:
        unit = coordinator.UNIT_BY_NAME["accelerator"]
        with (
            mock.patch.dict(coordinator.os.environ, {}, clear=True),
            mock.patch.object(coordinator, "gh") as gh,
            mock.patch.object(coordinator, "run") as run,
            self.assertRaisesRegex(RuntimeError, "GH_TOKEN is required"),
        ):
            coordinator.clone_repository(unit, "main", pathlib.Path("unused"))
        gh.assert_not_called()
        run.assert_not_called()

    def test_maintenance_branch_rejects_another_platform_version_line(self) -> None:
        with (
            mock.patch.object(coordinator, "PLATFORM_REF", "release/v0.1.x"),
            mock.patch.object(coordinator, "validate_environment"),
            mock.patch.object(
                coordinator,
                "manifest_values",
                return_value=(
                    "release-v0.2.0",
                    "release-v0.1.9",
                    "preview.20260831",
                    None,
                    {},
                ),
            ),
            mock.patch.object(coordinator, "platform_release") as platform_release,
            mock.patch.object(coordinator, "plan_units") as plan_units,
            self.assertRaisesRegex(RuntimeError, "does not belong"),
        ):
            coordinator.main()
        platform_release.assert_not_called()
        plan_units.assert_not_called()

    def test_incomplete_stable_release_closes_preview_before_planning(self) -> None:
        partial = coordinator.ReleaseStatus(
            {"tag_name": "release-v0.1.2", "assets": []}, None, False
        )
        with (
            mock.patch.object(coordinator, "PLATFORM_REF", "main"),
            mock.patch.object(coordinator, "validate_environment"),
            mock.patch.object(
                coordinator,
                "manifest_values",
                return_value=(
                    "release-v0.1.2",
                    "release-v0.1.1",
                    "preview.20260831",
                    None,
                    {},
                ),
            ),
            mock.patch.object(
                coordinator, "release_version", return_value="release-v0.1.2"
            ),
            mock.patch.object(coordinator, "platform_release", return_value=partial),
            mock.patch.object(coordinator, "plan_units") as plan_units,
            self.assertRaisesRegex(coordinator.Deferred, "incomplete Stable Release"),
        ):
            coordinator.main()
        plan_units.assert_not_called()

    def test_formal_coordinator_rejects_stale_local_checkout(self) -> None:
        selected = "a" * 40
        with (
            mock.patch.dict(
                formal.os.environ,
                {
                    "RELEASE_VERSION": "release-v0.1.2",
                    "PLATFORM_REF": "main",
                    "PLATFORM_SHA": selected,
                },
            ),
            mock.patch.object(
                formal.coordinator,
                "run",
                return_value=mock.Mock(stdout="b" * 40 + "\n"),
            ),
            mock.patch.object(formal.coordinator, "branch_sha") as branch_sha,
            self.assertRaisesRegex(SystemExit, "checkout does not match"),
        ):
            formal.main()
        branch_sha.assert_not_called()

    def test_formal_coordinator_polls_after_pending_api_convergence(self) -> None:
        plans: dict[str, coordinator.Plan] = {}
        with (
            mock.patch.dict(
                formal.os.environ,
                {"RELEASE_WAIT_SECONDS": "60", "RELEASE_POLL_SECONDS": "1"},
            ),
            mock.patch.object(
                formal.coordinator,
                "converge",
                side_effect=(coordinator.Pending("API convergence"), True),
            ) as converge,
            mock.patch.object(formal.time, "sleep") as sleep,
        ):
            formal.wait_for_convergence(plans, "release-v1.2.3", "a" * 40)
        self.assertEqual(converge.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_cancelled_workflow_uses_full_rerun(self) -> None:
        state = {"id": 17, "conclusion": "cancelled"}
        with mock.patch.object(coordinator, "gh") as gh:
            coordinator.rerun_workflow("kuasar-sandbox/accelerator", state)
        gh.assert_called_once_with(
            "run", "rerun", "17", "--repo", "kuasar-sandbox/accelerator"
        )

    def test_failed_workflow_uses_failed_job_rerun(self) -> None:
        state = {"id": 18, "conclusion": "failure"}
        with mock.patch.object(coordinator, "gh") as gh:
            coordinator.rerun_workflow("kuasar-sandbox/accelerator", state)
        gh.assert_called_once_with(
            "run",
            "rerun",
            "18",
            "--repo",
            "kuasar-sandbox/accelerator",
            "--failed",
        )

    def test_aggregate_run_identity_includes_selected_source(self) -> None:
        sha = "c" * 40
        title = coordinator.aggregate_run_title(
            "release-v0.1.2-preview.20260831", sha
        )
        self.assertEqual(
            title, f"Aggregate release-v0.1.2-preview.20260831 @{sha}"
        )
        self.assertTrue(
            coordinator.aggregate_title_matches(
                title, "release-v0.1.2-preview.20260831"
            )
        )

    def test_active_aggregate_keeps_current_manifest_immutable(self) -> None:
        empty = coordinator.ReleaseStatus(None, None, False)
        active = {
            "status": "in_progress",
            "html_url": "https://example.invalid/run/19",
        }
        with (
            mock.patch.object(coordinator, "PLATFORM_REF", "main"),
            mock.patch.object(coordinator, "validate_environment"),
            mock.patch.object(
                coordinator,
                "manifest_values",
                return_value=(
                    "release-v0.1.2",
                    "release-v0.1.1",
                    "preview.20260831",
                    None,
                    {},
                ),
            ),
            mock.patch.object(
                coordinator, "release_version", return_value="release-v0.1.1"
            ),
            mock.patch.object(coordinator, "platform_release", return_value=empty),
            mock.patch.object(
                coordinator, "active_aggregate_run", return_value=active
            ),
            mock.patch.object(coordinator, "plan_units") as plan_units,
            self.assertRaisesRegex(coordinator.Pending, "manifest immutable"),
        ):
            coordinator.main()
        plan_units.assert_not_called()

    def test_stale_incomplete_selection_rolls_to_requested_date(self) -> None:
        empty = coordinator.ReleaseStatus(None, None, False)
        selected = "a" * 40
        with (
            mock.patch.object(coordinator, "PLATFORM_REF", "main"),
            mock.patch.object(coordinator, "PLATFORM_SHA", selected),
            mock.patch.object(coordinator, "TODAY", "20260831"),
            mock.patch.object(coordinator, "validate_environment"),
            mock.patch.object(
                coordinator,
                "manifest_values",
                return_value=(
                    "release-v1.2.3",
                    "release-v1.2.2",
                    "preview.20260830",
                    "preview.20260829",
                    {},
                ),
            ),
            mock.patch.object(
                coordinator, "release_version", return_value="release-v1.2.2"
            ),
            mock.patch.object(coordinator, "platform_release", return_value=empty),
            mock.patch.object(coordinator, "active_aggregate_run", return_value=None),
            mock.patch.object(coordinator, "active_delete_run", return_value=None),
            mock.patch.object(
                coordinator, "plan_units", return_value={}
            ) as plan_units,
            mock.patch.object(
                coordinator, "render_manifest", return_value="updated"
            ) as render_manifest,
            mock.patch.object(
                coordinator, "persist_manifest", return_value=selected
            ),
            mock.patch.object(coordinator, "converge", return_value=True),
        ):
            coordinator.main()
        plan_units.assert_called_once_with({}, "20260831")
        self.assertEqual(
            render_manifest.call_args.args[2:4],
            ("20260831", "preview.20260830"),
        )

    def test_stale_partial_aggregate_is_cleaned_before_rollover(self) -> None:
        empty = coordinator.ReleaseStatus(None, None, False)
        partial = coordinator.ReleaseStatus(
            {"target_commitish": "b" * 40}, "b" * 40, False
        )
        with (
            mock.patch.object(coordinator, "PLATFORM_REF", "main"),
            mock.patch.object(coordinator, "TODAY", "20260831"),
            mock.patch.object(coordinator, "validate_environment"),
            mock.patch.object(
                coordinator,
                "manifest_values",
                return_value=(
                    "release-v1.2.3",
                    "release-v1.2.2",
                    "preview.20260830",
                    None,
                    {},
                ),
            ),
            mock.patch.object(
                coordinator, "release_version", return_value="release-v1.2.2"
            ),
            mock.patch.object(
                coordinator, "platform_release", side_effect=(empty, partial)
            ),
            mock.patch.object(
                coordinator,
                "dispatch_platform_cleanup",
                side_effect=coordinator.Pending("cleanup"),
            ) as cleanup,
            mock.patch.object(coordinator, "plan_units") as plan_units,
            self.assertRaisesRegex(coordinator.Pending, "cleanup"),
        ):
            coordinator.main()
        cleanup.assert_called_once_with(
            "release-v1.2.3-preview.20260830", "b" * 40
        )
        plan_units.assert_not_called()

    def test_aggregate_lookup_is_exact_for_selected_source(self) -> None:
        version = "release-v0.1.2-preview.20260831"
        sha = "d" * 40
        empty = coordinator.ReleaseStatus(None, None, False)
        other = {
            "status": "completed",
            "conclusion": "cancelled",
            "created_at": "2026-08-31T08:00:00Z",
            "display_title": coordinator.aggregate_run_title(version, "e" * 40),
        }
        with (
            mock.patch.object(coordinator, "platform_release", return_value=empty),
            mock.patch.object(coordinator, "aggregate_runs", return_value=[other]),
            mock.patch.object(coordinator, "gh") as gh,
        ):
            self.assertFalse(coordinator.ensure_aggregate(version, sha))
        gh.assert_called_once_with(
            "workflow",
            "run",
            "aggregate-release.yml",
            "--repo",
            coordinator.PLATFORM_REPOSITORY,
            "--ref",
            "main",
            "-f",
            f"version={version}",
            "-f",
            f"source_ref={coordinator.PLATFORM_REF}",
            "-f",
            f"source_sha={sha}",
        )

    def test_failed_aggregate_redispatches_a_fresh_exact_stage(self) -> None:
        version = "release-v0.1.2-preview.20260831"
        sha = "d" * 40
        empty = coordinator.ReleaseStatus(None, None, False)
        failed = {
            "id": 20,
            "status": "completed",
            "conclusion": "failure",
            "run_attempt": 2,
            "created_at": "2026-08-31T08:00:00Z",
            "display_title": coordinator.aggregate_run_title(version, sha),
            "html_url": "https://example.invalid/run/20",
        }
        with (
            mock.patch.object(coordinator, "platform_release", return_value=empty),
            mock.patch.object(coordinator, "aggregate_runs", return_value=[failed]),
            mock.patch.object(coordinator, "gh") as gh,
        ):
            self.assertFalse(coordinator.ensure_aggregate(version, sha))
        gh.assert_called_once_with(
            "workflow",
            "run",
            "aggregate-release.yml",
            "--repo",
            coordinator.PLATFORM_REPOSITORY,
            "--ref",
            "main",
            "-f",
            f"version={version}",
            "-f",
            f"source_ref={coordinator.PLATFORM_REF}",
            "-f",
            f"source_sha={sha}",
        )

    def test_aggregate_fresh_runs_share_the_three_attempt_budget(self) -> None:
        version = "release-v0.1.2-preview.20260831"
        sha = "d" * 40
        title = coordinator.aggregate_run_title(version, sha)
        empty = coordinator.ReleaseStatus(None, None, False)
        failed = [
            {
                "id": 20,
                "status": "completed",
                "conclusion": "failure",
                "run_attempt": 2,
                "created_at": "2026-08-31T08:00:00Z",
                "display_title": title,
                "html_url": "https://example.invalid/run/20",
            },
            {
                "id": 21,
                "status": "completed",
                "conclusion": "failure",
                "run_attempt": 1,
                "created_at": "2026-08-31T09:00:00Z",
                "display_title": title,
                "html_url": "https://example.invalid/run/21",
            },
        ]
        with (
            mock.patch.object(coordinator, "platform_release", return_value=empty),
            mock.patch.object(coordinator, "aggregate_runs", return_value=failed),
            mock.patch.object(coordinator, "gh") as gh,
            self.assertRaisesRegex(coordinator.Deferred, "aggregate publication failed"),
        ):
            coordinator.ensure_aggregate(version, sha)
        gh.assert_not_called()

    def test_older_active_aggregate_is_not_hidden_by_newer_cancelled_run(self) -> None:
        version = "release-v0.1.2-preview.20260831"
        older_active = {
            "status": "in_progress",
            "created_at": "2026-08-31T07:00:00Z",
        }
        newer_cancelled = {
            "status": "completed",
            "conclusion": "cancelled",
            "created_at": "2026-08-31T08:00:00Z",
        }
        with mock.patch.object(
            coordinator,
            "aggregate_runs",
            return_value=[older_active, newer_cancelled],
        ):
            self.assertIs(coordinator.active_aggregate_run(version), older_active)

    def test_validation_defers_when_scanned_platform_branch_moves(self) -> None:
        selected = "a" * 40
        with (
            mock.patch.object(coordinator, "PLATFORM_REF", "main"),
            mock.patch.object(coordinator, "PLATFORM_SHA", selected),
            mock.patch.object(coordinator, "TODAY", "20260831"),
            mock.patch.object(
                coordinator, "run", return_value=mock.Mock(stdout=selected + "\n")
            ),
            mock.patch.object(coordinator, "branch_sha", return_value="b" * 40),
            mock.patch.object(coordinator.selection, "validate_current_manifests"),
            self.assertRaisesRegex(coordinator.Deferred, "moved after the scanner"),
        ):
            coordinator.validate_environment()

    def test_release_contract_rejects_incomplete_assets(self) -> None:
        unit = coordinator.UNIT_BY_NAME["accelerator"]
        state = coordinator.RepositoryState.__new__(coordinator.RepositoryState)
        state.unit = unit
        state._statuses = {}
        state.tag_shas = {"v1.2.3-preview.20260831": "1" * 40}
        state.invalid_tags = set()
        state.releases = {
            "v1.2.3-preview.20260831": {
                "draft": False,
                "prerelease": True,
                "target_commitish": "1" * 40,
                "assets": [
                    {"name": "SHA256SUMS", "state": "uploaded"},
                ],
            }
        }
        status = state.status("v1.2.3-preview.20260831")
        self.assertTrue(status.partial)
        self.assertFalse(status.complete)

    def test_release_contract_accepts_exact_component_assets(self) -> None:
        unit = coordinator.UNIT_BY_NAME["runtime"]
        state = coordinator.RepositoryState.__new__(coordinator.RepositoryState)
        state.unit = unit
        state._statuses = {}
        state.tag_shas = {"runtime-v1.2.3-preview.20260831": "2" * 40}
        state.invalid_tags = set()
        state.releases = {
            "runtime-v1.2.3-preview.20260831": {
                "draft": False,
                "prerelease": True,
                "target_commitish": "2" * 40,
                "assets": [
                    {"name": "SHA256SUMS", "state": "uploaded"},
                    {
                        "name": "sandbox-runtime-x86_64-v1.2.3-preview.20260831.tar.gz",
                        "state": "uploaded",
                    },
                ],
            }
        }
        status = state.status("runtime-v1.2.3-preview.20260831")
        self.assertTrue(status.complete)

    def test_platform_assets_are_derived_from_exact_manifest_commit(self) -> None:
        manifest = """version: release-v9.8.7
preview_version: preview.20260831
components:
  accelerator: v1.0.1-preview.20260831
  connector: v1.0.2
  sandboxer: v1.0.3-preview.20260831
  orchestrator: v1.0.4
  runtime: runtime-v1.0.5
  vmlinux: vmlinux-v1.0.6
"""
        response = {"content": base64.b64encode(manifest.encode()).decode()}
        with mock.patch.object(coordinator, "api_optional", return_value=response):
            assets = coordinator.platform_asset_names(
                "release-v9.8.7-preview.20260831", "d" * 40
            )
        self.assertEqual(len(assets), 8)
        self.assertIn("connector-v1.0.2-linux-x86_64.tar.gz", assets)
        self.assertIn("sandbox-runtime-x86_64-v1.0.5.tar.gz", assets)

    def test_tagless_release_target_is_recoverable(self) -> None:
        status = coordinator.ReleaseStatus(
            {"target_commitish": "c" * 40}, None, False
        )
        self.assertEqual(coordinator.recoverable_source_sha(status), "c" * 40)

    def test_configured_tagless_preview_dispatches_cleanup(self) -> None:
        unit = coordinator.UNIT_BY_NAME["connector"]
        tag = "v1.2.3-preview.20260831"
        state = mock.Mock()
        state.status.return_value = coordinator.ReleaseStatus(
            {"target_commitish": "c" * 40}, None, False
        )
        with (
            mock.patch.object(
                coordinator, "active_release_run", return_value=None
            ),
            mock.patch.object(coordinator, "dispatch_cleanup") as cleanup,
        ):
            coordinator.ensure_configured_state(unit, tag, state)
        self.assertEqual(cleanup.call_args.args[0].source_sha, "c" * 40)
        self.assertEqual(cleanup.call_args.args[1], "incomplete")

    def test_missing_component_maintenance_branch_freezes_exact_selection(self) -> None:
        unit = coordinator.UNIT_BY_NAME["sandboxer"]
        stable = coordinator.ReleaseStatus(
            {"tag_name": "v0.3.5"}, "3" * 40, True
        )
        fake_state = mock.Mock()
        fake_state.status.return_value = stable
        with (
            mock.patch.object(coordinator, "PLATFORM_REF", "release/v0.5.x"),
            mock.patch.object(coordinator, "RepositoryState", return_value=fake_state),
            mock.patch.object(coordinator, "ensure_configured_state"),
            mock.patch.object(coordinator, "branch_sha", return_value=None),
        ):
            plan = coordinator.make_plan(
                unit, "v0.3.5", "20260831", pathlib.Path("/unused")
            )
        self.assertIsNone(plan.source_ref)
        self.assertEqual(plan.selected, "v0.3.5")
        self.assertEqual(plan.action, "fixed")

    def test_dependency_rebuild_cannot_reuse_same_day_preview(self) -> None:
        unit = coordinator.UNIT_BY_NAME["sandboxer"]
        plan = coordinator.Plan(
            unit,
            "v0.3.6-preview.20260831",
            "release/v0.3.x",
            "4" * 40,
            "v0.3.6-preview.20260831",
            "v0.3.6-preview.20260831",
            "reuse",
        )
        state = mock.Mock()
        state.status.return_value = coordinator.ReleaseStatus(
            {"tag_name": plan.selected}, plan.source_sha, True
        )
        with self.assertRaisesRegex(coordinator.Deferred, "same Preview date"):
            coordinator.force_dependency_preview(plan, "20260831", state)

    def test_dependency_change_defers_when_dependent_branch_is_missing(self) -> None:
        plan = coordinator.Plan(
            coordinator.UNIT_BY_NAME["sandboxer"],
            "v0.3.5",
            None,
            "4" * 40,
            "v0.3.5",
            "v0.3.5",
            "fixed",
        )
        with self.assertRaisesRegex(
            coordinator.Deferred, "no derived source branch"
        ):
            coordinator.force_dependency_preview(
                plan, "20260831", mock.Mock()
            )

    def test_unchanged_manifest_still_rechecks_remote_branch_and_blob(self) -> None:
        content = """version: release-v1.2.3
preview_version: preview.20260831
components:
  accelerator: v1.0.0
  connector: v1.0.0
  sandboxer: v1.0.0
  orchestrator: v1.0.0
  runtime: runtime-v1.0.0
  vmlinux: vmlinux-v1.0.0
"""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "releases").mkdir()
            (root / "releases/daily-preview.yaml").write_text(
                content, encoding="utf-8"
            )
            remote = {
                "content": base64.b64encode(content.encode()).decode(),
                "sha": "blob",
            }
            with (
                mock.patch.object(coordinator, "PLATFORM_ROOT", root),
                mock.patch.object(coordinator, "PLATFORM_REF", "main"),
                mock.patch.object(coordinator, "PLATFORM_SHA", "a" * 40),
                mock.patch.object(
                    coordinator, "branch_sha", return_value="a" * 40
                ) as branch_sha,
                mock.patch.object(
                    coordinator, "api", return_value=remote
                ) as api,
                mock.patch.object(coordinator, "gh_platform") as gh_platform,
            ):
                result = coordinator.persist_manifest(
                    content, "release-v1.2.3-preview.20260831"
                )
        self.assertEqual(result, "a" * 40)
        branch_sha.assert_called_once_with(
            coordinator.PLATFORM_REPOSITORY, "main"
        )
        api.assert_called_once()
        gh_platform.assert_not_called()

    def test_manifest_keeps_independent_component_versions(self) -> None:
        plans = {
            name: coordinator.Plan(
                coordinator.UNIT_BY_NAME[name],
                tag,
                "main",
                str(index) * 40,
                tag,
                tag,
                "reuse",
            )
            for index, (name, tag) in enumerate(
                {
                    "accelerator": "v0.2.1",
                    "connector": "v0.1.9",
                    "sandboxer": "v0.3.6-preview.20260831",
                    "orchestrator": "v0.4.3",
                    "runtime": "runtime-v0.1.2",
                    "vmlinux": "vmlinux-v0.1.0",
                }.items(),
                1,
            )
        }
        content = coordinator.render_manifest(
            "release-v0.5.7",
            "release-v0.5.6",
            "20260831",
            "preview.20260830",
            plans,
        )
        self.assertIn("  sandboxer: v0.3.6-preview.20260831\n", content)
        self.assertIn("  orchestrator: v0.4.3\n", content)
        self.assertIn("previous_preview_version: preview.20260830\n", content)

    def test_component_dispatch_carries_aggregate_commit(self) -> None:
        unit = coordinator.UNIT_BY_NAME["accelerator"]
        plan = coordinator.Plan(
            unit,
            "v0.2.0",
            "main",
            "a" * 40,
            "v0.2.1-preview.20260831",
            "v0.2.1-preview.20260831",
            "publish",
        )
        values = coordinator.release_inputs(
            plan,
            {"accelerator": plan},
            "release-v0.3.0-preview.20260831",
            "b" * 40,
        )
        self.assertIn("aggregate_sha=" + "b" * 40, values)

    def test_release_run_title_pins_source_and_dependency_tuple(self) -> None:
        accelerator = coordinator.Plan(
            coordinator.UNIT_BY_NAME["accelerator"],
            "v1.0.0",
            "main",
            "a" * 40,
            "v1.0.1-preview.20260831",
            "v1.0.1-preview.20260831",
            "publish",
        )
        connector = coordinator.Plan(
            coordinator.UNIT_BY_NAME["connector"],
            "v1.0.0",
            "main",
            "b" * 40,
            "v1.0.1-preview.20260831",
            "v1.0.1-preview.20260831",
            "publish",
        )
        sandboxer = coordinator.Plan(
            coordinator.UNIT_BY_NAME["sandboxer"],
            "v2.0.0",
            "main",
            "c" * 40,
            "v2.0.1-preview.20260831",
            "v2.0.1-preview.20260831",
            "publish",
        )
        title = coordinator.release_run_title(
            sandboxer,
            {
                "accelerator": accelerator,
                "connector": connector,
                "sandboxer": sandboxer,
            },
        )
        self.assertEqual(
            title,
            "Release v2.0.1-preview.20260831 @"
            + "c" * 40
            + " [accelerator=v1.0.1-preview.20260831,"
            "connector=v1.0.1-preview.20260831]",
        )

    def test_preview_reuse_rejects_another_dependency_binding(self) -> None:
        accelerator = coordinator.Plan(
            coordinator.UNIT_BY_NAME["accelerator"],
            "v1.0.0",
            "main",
            "a" * 40,
            "v1.0.1-preview.20260831",
            "v1.0.1-preview.20260831",
            "reuse",
        )
        connector = coordinator.Plan(
            coordinator.UNIT_BY_NAME["connector"],
            "v1.0.0",
            "main",
            "b" * 40,
            "v1.0.1-preview.20260831",
            "v1.0.1-preview.20260831",
            "reuse",
        )
        sandboxer = coordinator.Plan(
            coordinator.UNIT_BY_NAME["sandboxer"],
            "v2.0.1-preview.20260831",
            "main",
            "c" * 40,
            "v2.0.1-preview.20260831",
            "v2.0.1-preview.20260831",
            "reuse",
        )
        status = coordinator.ReleaseStatus(
            {
                "body": self.binding_body(
                    "sandboxer",
                    sandboxer.source_sha,
                    "accelerator=v1.0.1-preview.20260831,connector=v1.0.0",
                )
            },
            sandboxer.source_sha,
            True,
        )
        with self.assertRaisesRegex(coordinator.Deferred, "dependency binding"):
            coordinator.validate_preview_reuse(
                sandboxer,
                {
                    "accelerator": accelerator,
                    "connector": connector,
                    "sandboxer": sandboxer,
                },
                status,
            )

    def test_cleanup_does_not_start_a_fourth_attempt(self) -> None:
        unit = coordinator.UNIT_BY_NAME["connector"]
        plan = coordinator.Plan(
            unit,
            "v1.2.3-preview.20260831",
            "main",
            "c" * 40,
            "v1.2.3-preview.20260831",
            "v1.2.3-preview.20260831",
            "cleanup",
        )
        failed = {
            "status": "completed",
            "conclusion": "failure",
            "run_attempt": 3,
            "html_url": "https://example.invalid/run/1",
        }
        with (
            mock.patch.object(coordinator, "active_delete_run", return_value=None),
            mock.patch.object(coordinator, "latest_run", return_value=failed),
            mock.patch.object(coordinator, "gh") as gh,
            self.assertRaisesRegex(coordinator.Deferred, "three attempts"),
        ):
            coordinator.dispatch_cleanup(plan, "incomplete")
        gh.assert_not_called()

    def test_cancelled_cleanup_is_retried_after_concurrency_coalescing(self) -> None:
        plan = coordinator.Plan(
            coordinator.UNIT_BY_NAME["connector"],
            "v1.2.3-preview.20260831",
            "main",
            "c" * 40,
            "v1.2.3-preview.20260831",
            "v1.2.3-preview.20260831",
            "cleanup",
        )
        cancelled = {
            "id": 23,
            "status": "completed",
            "conclusion": "cancelled",
            "run_attempt": 1,
            "html_url": "https://example.invalid/run/23",
        }
        with (
            mock.patch.object(coordinator, "active_delete_run", return_value=None),
            mock.patch.object(coordinator, "latest_run", return_value=cancelled),
            mock.patch.object(coordinator, "gh") as gh,
            self.assertRaisesRegex(coordinator.Pending, "reran cleanup"),
        ):
            coordinator.dispatch_cleanup(plan, "incomplete")
        gh.assert_called_once_with(
            "run",
            "rerun",
            "23",
            "--repo",
            plan.unit.repository,
        )

    def test_cancelled_publication_is_retried_after_mutation_coalescing(self) -> None:
        plan = coordinator.Plan(
            coordinator.UNIT_BY_NAME["accelerator"],
            "v1.2.3-preview.20260831",
            "main",
            "c" * 40,
            "v1.2.3",
            "v1.2.3-preview.20260831",
            "publish",
        )
        state = mock.Mock()
        state.status.return_value = coordinator.ReleaseStatus(None, None, False)
        cancelled = {
            "id": 24,
            "status": "completed",
            "conclusion": "cancelled",
            "run_attempt": 1,
            "html_url": "https://example.invalid/run/24",
            "display_title": coordinator.release_run_title(
                plan, {"accelerator": plan}
            ),
        }
        with (
            mock.patch.object(coordinator, "RepositoryState", return_value=state),
            mock.patch.object(coordinator, "active_release_run", return_value=None),
            mock.patch.object(coordinator, "latest_run", return_value=cancelled),
            mock.patch.object(coordinator, "gh") as gh,
        ):
            self.assertFalse(
                coordinator.ensure_unit(
                    plan,
                    {"accelerator": plan},
                    "release-v9.8.7-preview.20260831",
                    "d" * 40,
                )
            )
        gh.assert_called_once_with(
            "run",
            "rerun",
            "24",
            "--repo",
            plan.unit.repository,
        )

    def test_successful_old_cleanup_is_redispatched_for_recreated_preview(self) -> None:
        unit = coordinator.UNIT_BY_NAME["connector"]
        plan = coordinator.Plan(
            unit,
            "v1.2.3-preview.20260831",
            "main",
            "c" * 40,
            "v1.2.3-preview.20260831",
            "v1.2.3-preview.20260831",
            "cleanup",
        )
        completed = {
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
            "html_url": "https://example.invalid/run/2",
        }
        with (
            mock.patch.object(coordinator, "active_delete_run", return_value=None),
            mock.patch.object(coordinator, "latest_run", return_value=completed),
            mock.patch.object(coordinator, "gh") as gh,
            self.assertRaisesRegex(coordinator.Pending, "redispatched"),
        ):
            coordinator.dispatch_cleanup(plan, "incomplete")
        self.assertEqual(
            gh.call_args.args[:3], ("workflow", "run", "delete-preview.yml")
        )

    def test_successful_old_platform_cleanup_is_redispatched(self) -> None:
        tag = "release-v1.2.3-preview.20260831"
        completed = {
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
            "html_url": "https://example.invalid/run/4",
        }
        with (
            mock.patch.object(coordinator, "active_aggregate_run", return_value=None),
            mock.patch.object(coordinator, "active_delete_run", return_value=None),
            mock.patch.object(coordinator, "latest_run", return_value=completed),
            mock.patch.object(coordinator, "gh") as gh,
            self.assertRaisesRegex(coordinator.Pending, "redispatched"),
        ):
            coordinator.dispatch_platform_cleanup(tag, "d" * 40)
        self.assertEqual(
            gh.call_args.args[:3], ("workflow", "run", "delete-preview.yml")
        )

    def test_active_component_cleanup_blocks_manifest_commit(self) -> None:
        tag = "v1.2.3-preview.20260831"
        plan = coordinator.Plan(
            coordinator.UNIT_BY_NAME["connector"],
            tag,
            "main",
            "c" * 40,
            tag,
            tag,
            "reuse",
        )
        active = {
            "status": "queued",
            "html_url": "https://example.invalid/run/3",
        }

        def delete_state(repository: str, version: str) -> dict[str, object] | None:
            if repository == coordinator.PLATFORM_REPOSITORY:
                return None
            self.assertEqual((repository, version), (plan.unit.repository, tag))
            return active

        with (
            mock.patch.object(coordinator, "PLATFORM_REF", "main"),
            mock.patch.object(coordinator, "validate_environment"),
            mock.patch.object(
                coordinator,
                "manifest_values",
                return_value=(
                    "release-v1.2.3",
                    "release-v1.2.2",
                    "preview.20260831",
                    None,
                    {"connector": tag},
                ),
            ),
            mock.patch.object(
                coordinator, "release_version", return_value="release-v1.2.2"
            ),
            mock.patch.object(
                coordinator,
                "platform_release",
                return_value=coordinator.ReleaseStatus(None, None, False),
            ),
            mock.patch.object(coordinator, "active_aggregate_run", return_value=None),
            mock.patch.object(
                coordinator, "active_delete_run", side_effect=delete_state
            ),
            mock.patch.object(
                coordinator, "plan_units", return_value={"connector": plan}
            ),
            mock.patch.object(coordinator, "active_release_run", return_value=None),
            mock.patch.object(coordinator, "persist_manifest") as persist,
            self.assertRaisesRegex(coordinator.Pending, "cleanup is active"),
        ):
            coordinator.main()
        persist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
