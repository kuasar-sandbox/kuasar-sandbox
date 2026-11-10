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


MODULE_PATH = pathlib.Path(__file__).resolve().parents[2] / "release/preview_coordinator.py"
SPEC = importlib.util.spec_from_file_location("preview_coordinator", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
coordinator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = coordinator
SPEC.loader.exec_module(coordinator)

SELECTION_TEST_SPEC = importlib.util.spec_from_file_location(
    "preview_selection_fixtures", pathlib.Path(__file__).with_name("preview_selection_test.py")
)
assert SELECTION_TEST_SPEC is not None and SELECTION_TEST_SPEC.loader is not None
selection_fixtures = importlib.util.module_from_spec(SELECTION_TEST_SPEC)
SELECTION_TEST_SPEC.loader.exec_module(selection_fixtures)

FORMAL_PATH = pathlib.Path(__file__).resolve().parents[2] / "release/formal_coordinator.py"
FORMAL_SPEC = importlib.util.spec_from_file_location(
    "formal_coordinator_test_subject", FORMAL_PATH
)
assert FORMAL_SPEC is not None and FORMAL_SPEC.loader is not None
formal = importlib.util.module_from_spec(FORMAL_SPEC)
sys.modules[FORMAL_SPEC.name] = formal
FORMAL_SPEC.loader.exec_module(formal)


class PreviewCoordinatorTest(unittest.TestCase):
    def setUp(self):
        coordinator._cleanup_dispatches.clear()
        coordinator._publication_resumes.clear()
        # Selection/dispatch tests have no accepted stage or active cleanup.
        # Actual stage recovery and its artifact/job gates live in test_workbench_resume.py.
        for name, value in (("active_delete_run", None), ("resume_publish", False), ("recover_configured_components", None)):
            patcher = mock.patch.object(coordinator, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

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

    def test_date_rollover_advances_only_a_published_update_baseline(self) -> None:
        base = "release-v1.2.3"
        current = "preview.20260908"
        for complete, prior in (
            (False, None),
            (False, "preview.20260907"),
            (True, None),
            (True, "preview.20260907"),
        ):
            with self.subTest(complete=complete, prior=prior):
                empty = coordinator.ReleaseStatus(None, None, False)
                published = coordinator.ReleaseStatus({}, "a" * 40, True)
                with (
                    mock.patch.object(coordinator, "PLATFORM_REF", "main"),
                    mock.patch.object(coordinator, "PLATFORM_SHA", "a" * 40),
                    mock.patch.object(coordinator, "TODAY", "20260909"),
                    mock.patch.object(coordinator, "validate_environment"),
                    mock.patch.object(coordinator, "manifest_next_versions", return_value={}),
                    mock.patch.object(
                        coordinator, "manifest_values",
                        return_value=(base, "release-v1.2.2", current, prior, {}),
                    ),
                    mock.patch.object(
                        coordinator, "release_version", return_value="release-v1.2.2"
                    ),
                    mock.patch.object(
                        coordinator, "platform_release",
                        side_effect=[empty, published if complete else empty],
                    ),
                    mock.patch.object(coordinator, "active_aggregate_run", return_value=None),
                    mock.patch.object(coordinator, "active_delete_run", return_value=None),
                    mock.patch.object(coordinator, "plan_units", return_value={}),
                    mock.patch.object(coordinator, "platform_changed_since", return_value=True),
                    mock.patch.object(coordinator, "render_manifest", return_value="manifest") as render,
                    mock.patch.object(coordinator, "persist_manifest", return_value="b" * 40),
                    mock.patch.object(coordinator, "converge", return_value=True),
                ):
                    coordinator.main()
                render.assert_called_once_with(
                    base, "release-v1.2.2", "20260909", current if complete else prior, {}, {}
                )

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

    def test_revision_rollover_and_scheduled_resume_preserve_the_baseline(self) -> None:
        for current, requested, selected in (
            ("20260914", "20260914.1", "20260914.1"),
            ("20260914.9", "20260914.10", "20260914.10"),
            ("20260914.10", "20260915", "20260915"),
            ("20260914.1", "20260914", "20260914.1"),
        ):
            with self.subTest(current=current, requested=requested):
                empty = coordinator.ReleaseStatus(None, None, False)
                with (
                    mock.patch.object(coordinator, "PLATFORM_REF", "main"),
                    mock.patch.object(coordinator, "TODAY", requested),
                    mock.patch.object(coordinator, "validate_environment"),
                    mock.patch.object(coordinator, "manifest_next_versions", return_value={}),
                    mock.patch.object(coordinator, "manifest_values", return_value=(
                        "release-v1.2.3", "release-v1.2.2", f"preview.{current}",
                        "preview.20260909", {},
                    )),
                    mock.patch.object(coordinator, "release_version", return_value="release-v1.2.2"),
                    mock.patch.object(coordinator, "platform_release", return_value=empty),
                    mock.patch.object(coordinator, "active_aggregate_run", return_value=None),
                    mock.patch.object(coordinator, "active_delete_run", return_value=None),
                    mock.patch.object(coordinator, "plan_units", return_value={}) as plan,
                    mock.patch.object(coordinator, "render_manifest", return_value="manifest") as render,
                    mock.patch.object(coordinator, "persist_manifest", return_value="b" * 40),
                    mock.patch.object(coordinator, "converge", return_value=True),
                ):
                    coordinator.main()
                plan.assert_called_once_with({}, selected, {})
                self.assertEqual(render.call_args.args[2:4], (selected, "preview.20260909"))

    def test_manifest_revision_order_is_numeric(self) -> None:
        text = "\n".join([
            "version: release-v1.2.3",
            "preview_version: preview.20260914.10",
            "previous_preview_version: preview.20260914.9",
            "components:",
            *[f"  {unit.name}: {coordinator.preview_selection.unit_prefix(unit.name)}1.2.3-preview.20260914.10"
              for unit in coordinator.UNITS],
        ])
        aggregate, previous, _ = coordinator.selection.parse_manifest(text, "fixture", True)
        self.assertEqual(aggregate, "release-v1.2.3-preview.20260914.10")
        self.assertEqual(previous, "release-v1.2.3-preview.20260914.9")
        with self.assertRaisesRegex(coordinator.selection.ManifestError, "must be older"):
            coordinator.selection.parse_manifest(
                text.replace("previous_preview_version: preview.20260914.9", "previous_preview_version: preview.20260914.11"),
                "fixture", True,
            )

    def test_preview_reuse_ignores_obsolete_aggregate_marker(self) -> None:
        tag = "v1.2.3-preview.20260914.1"
        plan = coordinator.Plan(coordinator.UNIT_BY_NAME["accelerator"], tag, "main", "a" * 40, tag, tag, "reuse")
        body = self.binding_body("accelerator", plan.source_sha).replace("20260831", "20260914.1")
        for suffix in ("20260914.1", "20260914x1", "20260914.2"):
            status = coordinator.ReleaseStatus({"body": body.replace("20260914.1", suffix)}, plan.source_sha, True)
            coordinator.validate_release_reuse(plan, status)


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

    def test_validation_titles_are_excluded_without_losing_ordinary_identity(self) -> None:
        version, sha = 'release-v1.2.3-preview.20261015', 'a' * 40
        title = coordinator.aggregate_run_title(version, sha)
        for accepted in (f'Aggregate {version}', title):
            self.assertTrue(coordinator.aggregate_title_matches(accepted, version))
        for rejected in (None, title + ' [validation only]', f'Aggregate {version} [validation only]',
                         title.replace('Aggregate ', 'Validate aggregate ', 1),
                         title.replace(version, version + '.2'), title + 'garbage', title[:-1]):
            with self.subTest(title=rejected):
                self.assertFalse(coordinator.aggregate_title_matches(rejected, version))

    def test_validation_runs_do_not_block_dispatch_or_consume_publication_retries(self) -> None:
        version, sha = 'release-v1.2.3-preview.20261015', 'a' * 40
        title = coordinator.aggregate_run_title(version, sha)
        empty = coordinator.ReleaseStatus(None, None, False)
        for validation_title in (title + ' [validation only]', title.replace('Aggregate ', 'Validate aggregate ', 1)):
            for status in ('queued', 'pending', 'in_progress', 'requested', 'waiting', 'completed'):
                for conclusion in ('success', 'failure', 'cancelled'):
                    validation = dict(id=2, display_title=validation_title, status=status, conclusion=conclusion,
                                      run_attempt=99, created_at='2026-10-15T02:00:00Z')
                    ordinary = dict(id=1, display_title=title, status='completed', conclusion='failure',
                                    run_attempt=2, created_at='2026-10-15T01:00:00Z')
                    for history in ([validation], [validation, ordinary]):
                        coordinator._publication_resumes.clear()
                        with self.subTest(title=validation_title, status=status, conclusion=conclusion, retry=len(history)), \
                             mock.patch.object(coordinator, 'paginated', return_value=history), \
                             mock.patch.object(coordinator, 'platform_release', return_value=empty), \
                             mock.patch.object(coordinator, 'dispatch_aggregate_run') as dispatch:
                            self.assertIsNone(coordinator.active_aggregate_run(version))
                            self.assertFalse(coordinator.ensure_aggregate(version, sha))
                            dispatch.assert_called_once_with(version, sha)

    def test_newer_validation_does_not_hide_active_ordinary_publication(self) -> None:
        version, sha = 'release-v1.2.3-preview.20261015', 'a' * 40
        title = coordinator.aggregate_run_title(version, sha)
        empty = coordinator.ReleaseStatus(None, None, False)
        for status in ('queued', 'pending', 'in_progress'):
            ordinary = dict(id=1, display_title=title, status=status, created_at='2026-10-15T01:00:00Z')
            history = [dict(id=3, display_title=title.replace('Aggregate ', 'Validate aggregate ', 1),
                            status='in_progress', created_at='2026-10-15T03:00:00Z'),
                       dict(id=2, display_title=title + ' [validation only]', status='pending',
                            created_at='2026-10-15T02:00:00Z'), ordinary]
            with self.subTest(status=status), mock.patch.object(coordinator, 'paginated', return_value=history), \
                 mock.patch.object(coordinator, 'platform_release', return_value=empty), \
                 mock.patch.object(coordinator, 'dispatch_aggregate_run') as dispatch:
                self.assertIs(coordinator.active_aggregate_run(version), ordinary)
                self.assertFalse(coordinator.ensure_aggregate(version, sha))
                dispatch.assert_not_called()
                self.assertIsNone(coordinator.active_aggregate_run(version + '.2'))
                self.assertFalse(coordinator.ensure_aggregate(version + '.2', sha))
                dispatch.assert_called_once_with(version + '.2', sha)

    def test_active_aggregate_keeps_current_manifest_immutable(self) -> None:
        empty = coordinator.ReleaseStatus(None, None, False)
        active = {
            "status": "in_progress",
            "html_url": "https://example.invalid/run/19",
        }
        with (
            mock.patch.object(coordinator, "PLATFORM_REF", "main"),
            mock.patch.object(coordinator, "validate_environment"),
            mock.patch.object(coordinator, "TODAY", "20260831"),
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
        targets = {"connector": "v2.0.0"}
        with (
            mock.patch.object(coordinator, "PLATFORM_REF", "main"),
            mock.patch.object(coordinator, "PLATFORM_SHA", selected),
            mock.patch.object(coordinator, "TODAY", "20260831"),
            mock.patch.object(coordinator, "validate_environment"),
            mock.patch.object(coordinator, "manifest_next_versions", return_value=targets),
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
        plan_units.assert_called_once_with({}, "20260831", targets)
        self.assertEqual(render_manifest.call_args.args[-1], targets)
        self.assertEqual(
            render_manifest.call_args.args[2:4],
            ("20260831", "preview.20260829"),
        )

    def test_stale_partial_aggregate_is_recovered_before_rollover(self) -> None:
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
                "ensure_aggregate",
                return_value=False,
            ) as recovery,
            mock.patch.object(coordinator, "plan_units") as plan_units,
            self.assertRaisesRegex(coordinator.Pending, "recover the original"),
        ):
            coordinator.main()
        recovery.assert_called_once_with("release-v1.2.3-preview.20260830", "b" * 40)
        plan_units.assert_not_called()

    def test_same_day_partial_recovers_original_source_before_main_drift(self) -> None:
        empty = coordinator.ReleaseStatus(None, None, False)
        partial = coordinator.ReleaseStatus(
            {"target_commitish": "b" * 40}, "b" * 40, False
        )
        with (
            mock.patch.object(coordinator, "PLATFORM_REF", "main"),
            mock.patch.object(coordinator, "TODAY", "20260830"),
            mock.patch.object(coordinator, "PLATFORM_SHA", "f" * 40),
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
                "ensure_aggregate",
                return_value=False,
            ) as recovery,
            mock.patch.object(coordinator, "plan_units") as plan_units,
            self.assertRaisesRegex(coordinator.Pending, "recover the original"),
        ):
            coordinator.main()
        recovery.assert_called_once_with("release-v1.2.3-preview.20260830", "b" * 40)
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

    def test_aggregate_history_does_not_spend_current_invocation_budget(self):
        version, sha = "release-v0.1.2-preview.20260831", "d" * 40
        failed = dict(id=20, status="completed", conclusion="failure", run_attempt=99,
                      display_title=coordinator.aggregate_run_title(version, sha))
        with mock.patch.object(coordinator, "platform_release", return_value=coordinator.ReleaseStatus(None, None, False)), \
             mock.patch.object(coordinator, "aggregate_runs", return_value=[failed]), \
             mock.patch.object(coordinator, "dispatch_aggregate_run") as dispatch:
            for _ in range(3): self.assertFalse(coordinator.ensure_aggregate(version, sha))
            with self.assertRaisesRegex(RuntimeError, "three attempts in this invocation"):
                coordinator.ensure_aggregate(version, sha)
            self.assertEqual(dispatch.call_count, 3)
            coordinator._publication_resumes.clear()
            self.assertFalse(coordinator.ensure_aggregate(version, sha))
            self.assertEqual(dispatch.call_count, 4)

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
                        "id": 21, "size": 50, "digest": "sha256:" + "a" * 64,
                        "state": "uploaded",
                    },
                ],
            }
        }
        status = state.status("runtime-v1.2.3-preview.20260831")
        self.assertTrue(status.complete)
        # Old checksum attachments and source/dependency prose are irrelevant.
        state._statuses.clear()
        release = state.releases['runtime-v1.2.3-preview.20260831']
        release['assets'] = [row for row in release['assets'] if row['name'] != 'SHA256SUMS']
        release.update(body='<!-- kuasar-preview-binding malformed old data -->', target_commitish='main')
        self.assertTrue(state.status('runtime-v1.2.3-preview.20260831').complete)
        state._statuses.clear()
        release['assets'][0]['digest'] = None
        with self.assertRaisesRegex(coordinator.Deferred, 'retain for inspection'):
            state.status('runtime-v1.2.3-preview.20260831')

    @staticmethod
    def repository_state(unit_name, tags):
        state = coordinator.RepositoryState.__new__(coordinator.RepositoryState)
        state.unit = coordinator.UNIT_BY_NAME[unit_name]
        state._statuses = {}
        state.invalid_tags = set()
        state.tag_shas = {tag: "a" * 40 for tag in tags}
        state.releases = {
            tag: {
                "draft": False,
                "prerelease": "-preview." in tag,
                "assets": [{
                    "name": coordinator.archive_name(unit_name, tag),
                    "id": 1, "size": 50, "digest": "sha256:" + "a" * 64,
                    "state": "uploaded",
                }],
            }
            for tag in tags
        }
        return state

    def test_discovery_filters_other_units_before_asset_admission(self):
        for unit, own, other in (
            ("runtime", "runtime-v1.2.3", "vmlinux-v1.2.3"),
            ("vmlinux", "vmlinux-v1.2.3", "runtime-v1.2.3"),
            ("orchestrator", "v1.2.3", "release-v1.2.3-preview.20260805"),
        ):
            with self.subTest(unit=unit):
                state = self.repository_state(unit, (other, own, "not-a-release"))
                # Even apparently matching assets must not admit another unit.
                with mock.patch.object(state, "status", wraps=state.status) as status:
                    self.assertEqual(state.usable_tags(), [own])
                status.assert_called_once_with(own)

    def test_discovery_retains_legacy_metadata_without_admitting_it(self):
        current, legacy = "v1.2.3", "v1.0.0-preview.20260808"
        state = self.repository_state("accelerator", (current, legacy))
        state.releases[legacy]["assets"].append({"name": "release.json"})
        original = json.dumps(state.releases, sort_keys=True)
        with mock.patch("builtins.print") as output:
            self.assertEqual(state.usable_tags(), [current])
        self.assertIn("kuasar-sandbox/accelerator " + legacy, output.call_args.args[0])
        self.assertEqual(json.dumps(state.releases, sort_keys=True), original)
        # Explicitly configured/selected versions still fail closed, including
        # after discovery. A skipped candidate is not cached as incomplete.
        with self.assertRaises(coordinator.Deferred):
            state.status(legacy)
        with self.assertRaises(coordinator.Deferred):
            coordinator.ensure_configured_state(state.unit, legacy, state)

    def test_discovery_excludes_unavailable_candidates_but_keeps_strict_status(self):
        current, old = "runtime-v1.2.3", "runtime-v1.0.0-preview.20260808"
        for problem in ("legacy_name", "digest", "duplicate", "unresolved", "incomplete"):
            with self.subTest(problem=problem):
                state = self.repository_state("runtime", (old, current))
                assets = state.releases[old]["assets"]
                if problem == "legacy_name":
                    assets[0]["name"] = "sandbox-runtime-x86_64-" + old + ".tar.gz"
                elif problem == "digest":
                    assets[0]["digest"] = None
                elif problem == "duplicate":
                    assets.append(dict(assets[0]))
                elif problem == "unresolved":
                    state.invalid_tags.add(old)
                else:
                    assets.clear()
                with mock.patch("builtins.print"):
                    self.assertEqual(state.usable_tags(), [current])
                if problem == "incomplete":
                    self.assertFalse(state.status(old).complete)
                else:
                    with self.assertRaises(coordinator.Deferred):
                        state.status(old)

    def test_discovery_skipped_selected_tag_still_blocks_planning(self):
        current, selected = "v1.2.3", "v1.2.4-preview.20261106"
        state = self.repository_state("accelerator", (selected, current))
        state.releases[selected]["assets"].append({"name": "release.json"})
        resolution = coordinator.preview_selection.Resolution(
            "main", "a" * 40, current, "a" * 40, "publish", selected,
        )
        with (
            mock.patch.object(coordinator, "RepositoryState", return_value=state),
            mock.patch.object(coordinator, "branch_sha", return_value="a" * 40),
            mock.patch.object(coordinator, "clone_repository"),
            mock.patch.object(coordinator.preview_selection, "resolve", return_value=resolution) as resolve,
            mock.patch.object(coordinator, "gh") as github,
            mock.patch("builtins.print"),
            self.assertRaises(coordinator.Deferred),
        ):
            coordinator.make_plan(state.unit, current, "20261106", pathlib.Path("unused"))
        self.assertEqual(resolve.call_args.args[4], [current])
        github.assert_not_called()

    def test_discovery_does_not_swallow_operational_failures(self):
        state = self.repository_state("connector", ("v1.2.3",))
        with mock.patch.object(state, "status", side_effect=RuntimeError("API unavailable")):
            with self.assertRaisesRegex(RuntimeError, "API unavailable"):
                state.usable_tags()

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
        with mock.patch.object(coordinator, "api_optional", return_value=response) as contents, \
             mock.patch.object(coordinator, "tag_sha", return_value="d" * 40):
            assets = coordinator.platform_asset_names(
                "release-v9.8.7-preview.20260831", "d" * 40
            )
        self.assertIn("?ref=release-v9.8.7-preview.20260831", contents.call_args.args[0])
        self.assertEqual(len(assets), 7)
        self.assertIn("connector-v1.0.2-linux-x86_64.tar.gz", assets)
        self.assertIn("sandbox-runtime-x86_64-v1.0.5.tar.gz", assets)

    def test_tagless_release_target_is_recoverable(self) -> None:
        status = coordinator.ReleaseStatus(
            {"target_commitish": "c" * 40}, None, False
        )
        self.assertEqual(coordinator.recoverable_source_sha(status), "c" * 40)

    def test_configured_tagless_preview_preserves_original_source(self) -> None:
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
            self.assertEqual(coordinator.ensure_configured_state(unit, tag, state), "c" * 40)
        cleanup.assert_not_called()

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
            mock.patch.object(coordinator, "ensure_configured_state", return_value=None),
            mock.patch.object(coordinator, "branch_sha", return_value=None),
        ):
            plan = coordinator.make_plan(
                unit, "v0.3.5", "20260831", pathlib.Path("/unused")
            )
        self.assertIsNone(plan.source_ref)
        self.assertEqual(plan.selected, "v0.3.5")
        self.assertEqual(plan.action, "fixed")

    def test_vmlinux_selection_and_release_binding_use_actual_source_commit(self) -> None:
        unit = coordinator.UNIT_BY_NAME["vmlinux"]
        configured = "vmlinux-v1.2.3-preview.20260831"
        for kernel_changed in (False, True):
            with self.subTest(kernel_changed=kernel_changed):
                repository = selection_fixtures.Repository()
                try:
                    makefile = (
                        "LINUX_TARBALL ?= https://kernel.invalid/linux.tar.gz\n"
                        "LINUX_TARBALL_SHA256 ?= " + "a" * 64 + "\n"
                    )
                    tagged_sha = repository.commit_files(
                        "released kernel", {"native-deps/Makefile": makefile}
                    )
                    repository.git("tag", configured, tagged_sha)
                    files = {"scripts/prepare-sandbox-init.py": "runtime change\n"}
                    if kernel_changed:
                        files["native-deps/Makefile"] = makefile.replace("a" * 64, "b" * 64)
                        files["native-deps/deps/vmlinux/sandbox-common.config"] = "# Rebuild for the new source checksum.\n"
                    head_sha = repository.commit_files("guest-runtime change", files)
                    released = coordinator.ReleaseStatus(
                        {
                            "tag_name": configured,
                            "body": self.binding_body("vmlinux", tagged_sha),
                        },
                        tagged_sha,
                        True,
                    )
                    state = mock.Mock()
                    state.status.side_effect = lambda tag: (
                        released if tag == configured
                        else coordinator.ReleaseStatus(None, None, False)
                    )
                    state.usable_tags.return_value = [configured]
                    with (
                        tempfile.TemporaryDirectory() as directory,
                        mock.patch.object(coordinator, "PLATFORM_REF", "main"),
                        mock.patch.object(coordinator, "RepositoryState", return_value=state),
                        mock.patch.object(coordinator, "branch_sha", return_value=head_sha),
                        mock.patch.object(
                            coordinator, "clone_repository",
                            side_effect=lambda unit, ref, destination: repository.git(
                                "clone", "-q", "--no-local", str(repository.root), str(destination)
                            ),
                        ),
                        mock.patch.object(coordinator, "gh") as github,
                    ):
                        plan = coordinator.make_plan(
                            unit, configured, "20260921.2", pathlib.Path(directory)
                        )
                        if kernel_changed:
                            self.assertEqual(plan.action, "publish")
                            self.assertEqual(plan.source_sha, head_sha)
                            self.assertEqual(plan.selected, "vmlinux-v1.2.3-preview.20260921.2")
                        else:
                            self.assertEqual(plan.action, "reuse")
                            self.assertEqual(plan.selected, configured)
                            self.assertEqual(plan.source_sha, tagged_sha)
                            self.assertTrue(coordinator.ensure_unit(
                                plan, {"vmlinux": plan}, "unused", "f" * 40
                            ))
                            released.release["body"] = self.binding_body("vmlinux", head_sha)
                            self.assertTrue(coordinator.ensure_unit(plan, {"vmlinux": plan}, "unused", "f" * 40))
                        github.assert_not_called()
                finally:
                    repository.close()

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

    def resumed_preview_plan(
        self, unit_name: str, winner: str, configured: str, date: str,
        *, complete: bool = False, matching_dependencies: bool = False, arm: bool = True,
        next_version: str | None = None,
    ) -> coordinator.Plan:
        dependent = coordinator.UNIT_BY_NAME[unit_name]
        names = {unit_name, *dependent.dependencies}
        if "sandboxer" in names:
            names.update(coordinator.UNIT_BY_NAME["sandboxer"].dependencies)
        units = tuple(unit for unit in coordinator.UNITS if unit.name in names)
        configured_versions = {
            unit.name: configured if unit == dependent else "v1.0.0"
            for unit in units
        }
        plans = {}
        states = {}
        for unit in units:
            selected = winner if unit == dependent else configured_versions[unit.name]
            plan = coordinator.Plan(
                unit, configured_versions[unit.name], "main", "a" * 40,
                selected, selected, "reuse",
            )
            plans[unit.name] = plan
            dependencies = ",".join(
                f"{name}={configured_versions[name] if matching_dependencies or name != 'sandboxer' else 'v0.9.0'}"
                for name in unit.dependencies
            )
            body = self.binding_body(unit.name, plan.source_sha, dependencies)
            if "-preview." in selected:
                body = body.replace("20260831", selected.rsplit("-preview.", 1)[1])
            published = coordinator.ReleaseStatus(
                {"tag_name": selected, "body": body, "assets": [
                    {"name": coordinator.archive_name(unit.name, selected, arch)}
                    for arch in (("x86_64", "aarch64") if arm else ("x86_64",))]}, plan.source_sha, True
            )
            statuses = {selected: published}
            if complete:
                configured_tag = configured_versions[unit.name]
                statuses[configured_tag] = coordinator.ReleaseStatus(
                    {**published.release, "tag_name": configured_tag, "assets": [
                        {"name": coordinator.archive_name(unit.name, configured_tag, arch)}
                        for arch in (("x86_64", "aarch64") if arm else ("x86_64",))]}, plan.source_sha, True)
            state = mock.Mock()
            state.status.side_effect = lambda tag, statuses=statuses: statuses.get(
                tag, coordinator.ReleaseStatus(None, None, False)
            )
            states[unit.name] = state
        with (
            mock.patch.object(coordinator, "UNITS", units),
            mock.patch.object(
                coordinator, "make_plan",
                side_effect=lambda unit, *_: plans[unit.name],
            ),
            mock.patch.object(
                coordinator, "RepositoryState",
                side_effect=lambda unit: states[unit.name],
            ),
        ):
            targets = {unit_name: next_version} if next_version is not None else {}
            return coordinator.plan_units(configured_versions, date, targets)[unit_name]

    def test_next_versions_do_not_force_unchanged_dependency_consumers(self) -> None:
        for unit in ("accelerator", "connector", "sandboxer", "orchestrator", "runtime"):
            prefix = coordinator.preview_selection.unit_prefix(unit)
            winner = prefix + "1.0.0"
            with self.subTest(unit=unit):
                plan = self.resumed_preview_plan(
                    unit, winner, winner, "20260831", next_version=prefix + "2.0.0",
                )
                self.assertEqual((plan.action, plan.selected), ("reuse", winner))

    def test_dependency_update_applies_next_version_floor(self) -> None:
        for unit, prefix in (("orchestrator", "v"), ("runtime", "runtime-v")):
            with self.subTest(unit=unit), mock.patch.object(
                coordinator, "dependency_inputs_changed", return_value=True,
            ):
                plan = self.resumed_preview_plan(
                    unit, prefix + "1.0.0", prefix + "1.0.0", "20260831.1",
                    next_version=prefix + "2.0.0",
                )
                self.assertEqual((plan.action, plan.selected),
                                 ("publish", prefix + "2.0.0-preview.20260831.1"))

    def test_pending_dependency_preview_preserves_target_across_restarts(self) -> None:
        for unit, prefix in (("orchestrator", "v"), ("runtime", "runtime-v")):
            for pending_core in ("1.0.1", "2.0.0"):
                for date in ("20260831", "20260831.1", "20260901"):
                    with self.subTest(unit=unit, pending_core=pending_core, date=date):
                        plan = self.resumed_preview_plan(
                            unit, prefix + "1.0.0", prefix + pending_core + "-preview.20260831",
                            date, matching_dependencies=True, next_version=prefix + "2.0.0",
                        )
                        self.assertEqual((plan.action, plan.selected),
                                         ("publish", prefix + "2.0.0-preview." + date))

    def test_next_version_does_not_replace_partial_publication_recovery(self) -> None:
        unit = coordinator.UNIT_BY_NAME["connector"]
        configured = "v1.0.1-preview.20260831"
        state = mock.Mock()
        state.status.return_value = coordinator.ReleaseStatus(None, "c" * 40, False)
        with (
            mock.patch.object(coordinator, "PLATFORM_REF", "main"),
            mock.patch.object(coordinator, "RepositoryState", return_value=state),
            mock.patch.object(coordinator, "active_release_run", return_value=None),
            mock.patch.object(coordinator, "branch_sha", return_value="d" * 40),
            mock.patch.object(coordinator, "clone_repository") as clone,
        ):
            plan = coordinator.make_plan(unit, configured, "20260901", pathlib.Path("unused"), "v2.0.0")
        self.assertEqual((plan.selected, plan.source_sha, plan.action), (configured, "c" * 40, "publish"))
        clone.assert_not_called()

    def test_next_version_keeps_dependency_publication_collision_checks(self) -> None:
        unit = coordinator.UNIT_BY_NAME["sandboxer"]
        plan = coordinator.Plan(unit, "v1.0.0", "main", "a" * 40, "v1.0.0", "v1.0.0", "reuse")
        for complete in (False, True):
            state = mock.Mock()
            state.status.return_value = coordinator.ReleaseStatus({}, "b" * 40, complete)
            with self.subTest(complete=complete), self.assertRaises(coordinator.Deferred):
                coordinator.force_dependency_preview(plan, "20260831", state, "v2.0.0")
            state.status.assert_called_once_with("v2.0.0-preview.20260831")

    def test_make_plan_passes_target_only_after_actual_source_change(self) -> None:
        unit = coordinator.UNIT_BY_NAME["connector"]
        repository = selection_fixtures.Repository()
        self.addCleanup(repository.close)
        winner = "v0.1.9"
        tagged = repository.commit("published")
        repository.git("tag", winner, tagged)
        head = repository.commit_files("guide update", {"README.md": "guide\n"})
        state = mock.Mock()
        state.usable_tags.return_value = [winner]
        state.status.side_effect = lambda tag: (coordinator.ReleaseStatus({}, tagged, True)
                                               if tag == winner else coordinator.ReleaseStatus(None, None, False))
        for changed in (False, True):
            if changed:
                head = repository.commit("changed product")
            with (
                self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory,
                mock.patch.object(coordinator, "PLATFORM_REF", "main"),
                mock.patch.object(coordinator, "RepositoryState", return_value=state),
                mock.patch.object(coordinator, "branch_sha", return_value=head),
                mock.patch.object(coordinator, "clone_repository",
                                  side_effect=lambda unit, source_ref, path: path.symlink_to(repository.root)),
            ):
                plan = coordinator.make_plan(unit, winner, "20260831", pathlib.Path(directory), "v0.2.0")
            self.assertEqual(plan.action, "publish" if changed else "reuse")
            self.assertEqual(plan.selected, "v0.2.0-preview.20260831" if changed else winner)
            self.assertEqual(plan.source_sha, head if changed else tagged)

    def test_first_arm_requires_explicit_initialization_and_a_new_version(self) -> None:
        with mock.patch.object(coordinator, "INITIALIZE_ARM", False):
            with self.assertRaisesRegex(coordinator.Deferred, "valid historical AMD64"):
                self.resumed_preview_plan("accelerator", "v1.0.0", "v1.0.0", "20260921", arm=False)
        with mock.patch.object(coordinator, "INITIALIZE_ARM", True):
            plan = self.resumed_preview_plan("accelerator", "v1.0.0", "v1.0.0", "20260921", arm=False)
            self.assertEqual(plan.action, "publish")
            self.assertEqual(plan.selected, "v1.0.1-preview.20260921")
            self.assertEqual(plan.winner, "v1.0.0")

    def test_published_dependency_reuse_does_not_read_source_or_history(self):
        tag = 'runtime-v1.0.1-preview.20260831'
        plan = coordinator.Plan(coordinator.UNIT_BY_NAME['runtime'], tag, 'main', 'a' * 40, tag, tag, 'reuse')
        for body in ('Human notes.', self.binding_body('runtime', 'b' * 40, 'obsolete=version')):
            status = coordinator.ReleaseStatus({'body': body}, plan.source_sha, True)
            with mock.patch.object(coordinator, 'api', side_effect=AssertionError('historical query')), mock.patch.object(
                    coordinator.preview_selection, 'run_git', side_effect=AssertionError('historical sources')):
                coordinator.validate_release_reuse(plan, status)
        with self.assertRaisesRegex(coordinator.Deferred, 'tag changed'):
            coordinator.validate_release_reuse(plan, coordinator.ReleaseStatus({}, 'b' * 40, True))

    def test_dependency_inputs_handle_an_unpublished_configured_preview(self) -> None:
        repository = selection_fixtures.Repository()
        self.addCleanup(repository.close)
        old = repository.commit_files("published library", {"pkg/flatten/input.go": "old"})
        winner = "v1.0.0"
        repository.git("tag", winner, old)
        pending = "v1.0.1-preview.20260831"
        plan = coordinator.Plan(coordinator.UNIT_BY_NAME["accelerator"], pending, "main", old,
                                winner, pending + ".1", "publish")
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "accelerator").symlink_to(repository.root, target_is_directory=True)
            for files, changed in (({"cmd/cache-ctl/main.go": "CLI"}, False),
                                   ({"pkg/flatten/input_test.go": "test"}, False),
                                   ({"pkg/flatten/input.go": "new library"}, True)):
                head = repository.commit_files("candidate inputs", files)
                plan = coordinator.replace(plan, source_sha=head)
                for consumer in ("sandboxer", "orchestrator", "runtime"):
                    with self.subTest(files=files, consumer=consumer):
                        self.assertEqual(coordinator.dependency_inputs_changed(
                            "accelerator", consumer, {"accelerator": plan},
                            {"accelerator": pending}, root), changed)

            # Once published, the configured tag is the comparison base even
            # when the resolver's older winner would report a library change.
            repository.git("tag", pending, head)
            head = repository.commit_files("later CLI", {"cmd/cache-ctl/main.go": "later"})
            plan = coordinator.replace(plan, source_sha=head)
            self.assertFalse(coordinator.dependency_inputs_changed(
                "accelerator", "runtime", {"accelerator": plan}, {"accelerator": pending}, root))
            with self.assertRaises(coordinator.preview_selection.SelectionError):
                coordinator.dependency_inputs_changed(
                    "accelerator", "runtime", {"accelerator": plan}, {"accelerator": "v9.9.9"}, root)
            repository.git("tag", "-d", pending, winner)
            with self.assertRaises(coordinator.preview_selection.SelectionError):
                coordinator.dependency_inputs_changed(
                    "accelerator", "runtime", {"accelerator": plan}, {"accelerator": pending}, root)

    def test_resumes_unpublished_dependency_preview_after_manifest_commit(self) -> None:
        for unit, prefix in (("orchestrator", "v"), ("runtime", "runtime-v")):
            for winner in (prefix + "1.0.0", prefix + "1.0.1-preview.20260830"):
                for date in ("20260831", "20260831.1", "20260901"):
                    with self.subTest(unit=unit, winner=winner, date=date):
                        plan = self.resumed_preview_plan(
                            unit, winner, prefix + "1.0.1-preview.20260831", date
                        )
                        self.assertEqual(plan.selected, prefix + "1.0.1-preview." + date)
                        self.assertEqual(plan.action, "publish")
                        self.assertEqual(plan.source_sha, "a" * 40)

    def test_resumed_completed_dependency_preview_is_reused_with_matching_binding(self) -> None:
        tag = "v1.0.1-preview.20260831"
        plan = self.resumed_preview_plan(
            "orchestrator", tag, tag, "20260831",
            complete=True, matching_dependencies=True,
        )
        self.assertEqual(plan.selected, tag)
        self.assertEqual(plan.action, "reuse")

    def test_resumed_complete_preview_ignores_old_dependency_marker(self):
        tag = 'v1.0.1-preview.20260831'
        plan = self.resumed_preview_plan('orchestrator', tag, tag, '20260831', complete=True)
        self.assertEqual((plan.selected, plan.action), (tag, 'reuse'))

    def test_pending_preview_does_not_revive_an_older_or_unrelated_version(self):
        winner = 'v1.0.1-preview.20260830'
        for configured in ('v1.0.1-preview.20260829', 'v1.0.2-preview.20260831'):
            with self.subTest(configured=configured):
                plan = self.resumed_preview_plan('orchestrator', winner, configured, '20260831')
                self.assertEqual((plan.selected, plan.action), (winner, 'reuse'))

    def test_pending_preview_does_not_rebuild_a_unit_without_dependencies(self) -> None:
        plan = self.resumed_preview_plan(
            "accelerator", "v1.0.1-preview.20260830",
            "v1.0.1-preview.20260831", "20260831",
        )
        self.assertEqual(plan.selected, "v1.0.1-preview.20260830")
        self.assertEqual(plan.action, "reuse")

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
notes:
  mode: fixed
  body: |
    Maintained notes.
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
        self.assertEqual(branch_sha.call_args_list, [mock.call(coordinator.PLATFORM_REPOSITORY, "main")] * 2)
        api.assert_called_once()
        gh_platform.assert_not_called()

    def test_daily_fixed_notes_follow_new_aggregate_and_components(self):
        manifest = ('version: release-v9.8.7\npreview_version: preview.20990101.1\ncomponents:\n' +
            ''.join(f'  {unit}: {unit + "-" if unit in ("runtime", "vmlinux") else ""}v1.2.3\n'
                    for unit in coordinator.selection.UNITS))
        block = 'notes:\n  mode: fixed\n  body: |\n    Fix #42.\n\n    Keep both paragraphs.\n'
        path = 'releases/daily-preview.yaml'
        current = manifest + block
        self.assertEqual(coordinator.retain_preview_notes(current, manifest, path), current)
        for updated in (manifest.replace('20990101.1', '20990101.2'),
                        manifest.replace('v1.2.3', 'v1.2.4')):
            self.assertEqual(coordinator.retain_preview_notes(current, updated, path), updated + block)

    def test_manifest_commit_is_compare_and_swap_on_whole_branch(self):
        content = 'complete manifest bytes\n'
        for rejected in (False, True):
            def mutate(*args):
                self.assertEqual(args[:3], ('api', 'graphql', '--input'))
                payload = json.loads(pathlib.Path(args[3]).read_text())
                self.assertIn('createCommitOnBranch', payload['query'])
                fields = payload['variables']['input']
                self.assertEqual(fields['expectedHeadOid'], 'a' * 40)
                self.assertEqual(fields['branch'], dict(repositoryNameWithOwner=coordinator.PLATFORM_REPOSITORY, branchName='main'))
                self.assertEqual(fields['fileChanges'], dict(additions=[dict(path='releases/daily-preview.yaml', contents=base64.b64encode(content.encode()).decode())]))
                response = {'errors': [{'message': 'expected head changed'}]} if rejected else {'data': {'createCommitOnBranch': {'commit': {'oid': 'b' * 40}}}}
                return mock.Mock(stdout=json.dumps(response))
            with mock.patch.object(coordinator, 'gh_platform', side_effect=mutate) as write:
                args = ('releases/daily-preview.yaml', content, 'release-v1.2.3-preview.20990101', 'main', 'a' * 40)
                if rejected:
                    with self.assertRaisesRegex(coordinator.Pending, 'not confirmed'):
                        coordinator.commit_manifest(*args)
                else:
                    self.assertEqual(coordinator.commit_manifest(*args), 'b' * 40)
                write.assert_called_once()

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
                source_head=str(index) * 40,
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

    def test_manifest_round_trip_retains_targets_and_reused_components(self) -> None:
        plans = {unit.name: coordinator.Plan(
            unit, coordinator.preview_selection.unit_prefix(unit.name) + "1.0.0", "main", "a" * 40,
            coordinator.preview_selection.unit_prefix(unit.name) + "1.0.0",
            coordinator.preview_selection.unit_prefix(unit.name) + "1.0.0", "reuse",
        ) for unit in coordinator.UNITS}
        targets = {"accelerator": "v2.0.0", "runtime": "runtime-v2.0.0"}
        content = coordinator.render_manifest("release-v2.0.0", None, "20260831", None, plans, targets)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "releases").mkdir()
            (root / "releases/daily-preview.yaml").write_text(content)
            with mock.patch.object(coordinator, "PLATFORM_ROOT", root):
                retained = coordinator.manifest_next_versions()
                selected = coordinator.manifest_values()[-1]
        self.assertEqual(retained, targets)
        self.assertEqual(selected, {name: plan.selected for name, plan in plans.items()})
        self.assertEqual(coordinator.render_manifest(
            "release-v2.0.0", None, "20260831", None, plans, retained,
        ), content)

    def test_e2e_only_change_keeps_tag_selection_and_does_not_select_branch_tests(self) -> None:
        repository = selection_fixtures.Repository()
        self.addCleanup(repository.close)
        product = repository.commit_files("product", {"cmd/main.go": "product bytes\n",
            "test/e2e/run_all.sh": "old test\n"})
        repository.git("tag", "v1.2.3")
        test_head = repository.commit_files("test fix", {"test/e2e/run_all.sh": "fixed test\n"})
        resolution = coordinator.preview_selection.resolve(repository.root, "main", "orchestrator", "v1.2.3",
                                                            {"v1.2.3"}, "20260922.4")
        self.assertEqual((resolution.action, resolution.selected, resolution.winner_commit), ("reuse", "v1.2.3", product))
        self.assertEqual(repository.git("diff", product, test_head, "--", "cmd"), "")
        plans = {unit.name: coordinator.Plan(unit,
                    (unit.name + "-" if unit.name in ("runtime", "vmlinux") else "") + "v1.2.3",
                    "main", product, "v1.2.3",
                    (unit.name + "-" if unit.name in ("runtime", "vmlinux") else "") + "v1.2.3",
                    "reuse", source_head=test_head) for unit in coordinator.UNITS}
        manifest = coordinator.render_manifest("release-v1.2.3", None, "20260922.4", None, plans)
        config = coordinator.selection.read_simple_yaml(manifest, "fixture")
        self.assertNotIn("test_revisions", config)
        self.assertNotIn(test_head, manifest)
        self.assertEqual(config["components"]["orchestrator"], "v1.2.3")
        plans["orchestrator"] = coordinator.replace(plans["orchestrator"], source_ref=None, source_head=None)
        fixed = coordinator.render_manifest("release-v1.2.3", None, "20260922.4", None, plans)
        self.assertEqual(fixed, manifest)

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

    def test_runtime_dependency_binding_matches_embedded_payloads(self) -> None:
        self.assertEqual(
            coordinator.UNIT_BY_NAME["runtime"].dependencies,
            ("accelerator", "sandboxer"),
        )

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


    def test_cleanup_budget_is_local_to_current_invocation(self):
        tag = 'v1.2.3-preview.20260831'
        plan = coordinator.Plan(coordinator.UNIT_BY_NAME['connector'], tag, 'main', 'c'*40, tag, tag, 'cleanup')
        failed = dict(status='completed', conclusion='failure', run_attempt=99)
        with mock.patch.object(coordinator, 'active_delete_run', return_value=None), mock.patch.object(
                coordinator, 'latest_run', return_value=failed), mock.patch.object(coordinator, 'gh') as gh:
            for _ in range(3):
                with self.assertRaises(coordinator.Pending): coordinator.dispatch_cleanup(plan, 'incomplete')
            with self.assertRaisesRegex(RuntimeError, 'three attempts in this invocation'):
                coordinator.dispatch_cleanup(plan, 'incomplete')
            self.assertEqual(gh.call_count, 3)
            coordinator._cleanup_dispatches.clear()
            with self.assertRaises(coordinator.Pending): coordinator.dispatch_cleanup(plan, 'incomplete')
            self.assertEqual(gh.call_count, 4)
            self.assertEqual(gh.call_args.args[:3], ('workflow', 'run', 'delete-preview.yml'))

    def test_component_history_does_not_spend_current_invocation_budget(self):
        tag = "v1.2.3-preview.20260909"
        plan = coordinator.Plan(coordinator.UNIT_BY_NAME["accelerator"], tag, "main", "a" * 40, tag, tag, "publish")
        state = mock.Mock()
        state.status.return_value = coordinator.ReleaseStatus(None, None, False)
        failed = dict(id=10, status="completed", conclusion="failure", run_attempt=99,
                      display_title=coordinator.release_run_title(plan, {"accelerator": plan}))
        with mock.patch.object(coordinator, "RepositoryState", return_value=state), \
             mock.patch.object(coordinator, "active_release_run", return_value=None), \
             mock.patch.object(coordinator, "release_runs", return_value=[failed]), \
             mock.patch.object(coordinator, "gh") as gh:
            for _ in range(3):
                self.assertFalse(coordinator.ensure_unit(plan, {"accelerator": plan}, "unused", "b" * 40))
            with self.assertRaisesRegex(RuntimeError, "three attempts in this invocation"):
                coordinator.ensure_unit(plan, {"accelerator": plan}, "unused", "b" * 40)
            self.assertEqual(gh.call_count, 3)
            coordinator._publication_resumes.clear()
            self.assertFalse(coordinator.ensure_unit(plan, {"accelerator": plan}, "unused", "b" * 40))

    def test_platform_cleanup_old_failures_do_not_prevent_current_attempts(self):
        tag = 'release-v1.2.3-preview.20260909'
        failed = dict(status='completed', conclusion='failure', run_attempt=99)
        with mock.patch.object(coordinator, 'active_aggregate_run', return_value=None), mock.patch.object(
                coordinator, 'active_delete_run', return_value=None), mock.patch.object(
                coordinator, 'latest_run', return_value=failed), mock.patch.object(coordinator, 'gh') as gh:
            for _ in range(3):
                with self.assertRaises(coordinator.Pending): coordinator.dispatch_platform_cleanup(tag, 'b'*40)
            with self.assertRaisesRegex(RuntimeError, 'three attempts in this invocation'):
                coordinator.dispatch_platform_cleanup(tag, 'b'*40)
            self.assertEqual(gh.call_count, 3)

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
            self.assertRaisesRegex(coordinator.Pending, "redispatched"),
        ):
            coordinator.dispatch_cleanup(plan, "incomplete")
        gh.assert_called_once_with('workflow', 'run', 'delete-preview.yml', '--repo', plan.unit.repository,
            '--ref', 'main', '-f', f'version={plan.selected}', '-f', f'source_sha={plan.source_sha}', '-f', 'mode=incomplete')

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
            mock.patch.object(coordinator, "release_runs", return_value=[cancelled]),
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
        gh.assert_called_once_with("workflow", "run", plan.unit.workflow, "--repo", plan.unit.repository,
                                   "--ref", "main", *coordinator.release_inputs(plan, {"accelerator": plan},
                                   "release-v9.8.7-preview.20260831", "d" * 40))

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
            mock.patch.object(coordinator, "TODAY", "20260831"),
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



class BranchOutcomeTest(unittest.TestCase):
    def test_inner_convergence_sleep_respects_deadline(self):
        with (mock.patch.object(coordinator, "converge", side_effect=coordinator.Pending("healthy aggregate")) as converge,
              mock.patch.object(coordinator.time, "monotonic", side_effect=[9, 9, 10]),
              mock.patch.object(coordinator.time, "sleep") as sleep):
            self.assertEqual(coordinator.converge_until_deadline({}, "fixture", 10), "pending")
        converge.assert_called_once()
        sleep.assert_called_once_with(1)

    def test_expired_planning_budget_does_not_dispatch(self):
        with (mock.patch.object(coordinator, "converge") as converge,
              mock.patch.object(coordinator.time, "monotonic", return_value=10)):
            self.assertEqual(coordinator.converge_until_deadline({}, "fixture", 10), "pending")
        converge.assert_not_called()

    def test_pending_is_rechecked_until_actual_completion(self):
        with (mock.patch.object(coordinator, "main", side_effect=[coordinator.Pending("healthy aggregate"), "published"]) as main,
              mock.patch.object(coordinator.time, "monotonic", return_value=0),
              mock.patch.object(coordinator.time, "sleep") as sleep,
              mock.patch.dict(coordinator.os.environ, {}, clear=True)):
            result = coordinator.run_branch()
        self.assertEqual(result["status"], "published")
        self.assertEqual(main.call_count, 2)
        sleep.assert_called_once()

    def test_budget_exhaustion_is_pending_not_publication(self):
        with (mock.patch.object(coordinator, "main", side_effect=coordinator.Pending("healthy aggregate")),
              mock.patch.object(coordinator, "WAIT_SECONDS", 10),
              mock.patch.object(coordinator.time, "monotonic", side_effect=[0, 10]),
              mock.patch.object(coordinator.time, "sleep") as sleep,
              mock.patch.dict(coordinator.os.environ, {}, clear=True)):
            result = coordinator.run_branch()
        self.assertEqual(result["status"], "pending")
        sleep.assert_not_called()

    def test_sleep_exhaustion_does_not_start_another_pass(self):
        with (mock.patch.object(coordinator, "main", side_effect=coordinator.Pending("healthy aggregate")) as main,
              mock.patch.object(coordinator, "WAIT_SECONDS", 10),
              mock.patch.object(coordinator.time, "monotonic", side_effect=[0, 9, 9, 10]),
              mock.patch.object(coordinator.time, "sleep") as sleep,
              mock.patch.dict(coordinator.os.environ, {}, clear=True)):
            result = coordinator.run_branch()
        self.assertEqual(result["status"], "pending")
        main.assert_called_once()
        sleep.assert_called_once_with(1)

    def test_deferred_selection_is_not_converged(self):
        with (mock.patch.object(coordinator, "main", side_effect=coordinator.Deferred("branch moved")),
              mock.patch.dict(coordinator.os.environ, {}, clear=True)):
            self.assertEqual(coordinator.run_branch()["status"], "deferred")

    def test_receipt_binds_input_head_and_selected_manifest_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "result.json"
            def finish(**kwargs):
                coordinator.PLATFORM_SHA = "b" * 40
                return "published"
            with (mock.patch.object(coordinator, "PLATFORM_SHA", "a" * 40),
                  mock.patch.object(coordinator, "PLATFORM_REF", "main"),
                  mock.patch.object(coordinator, "TODAY", "20261008"),
                  mock.patch.object(coordinator, "main", side_effect=finish),
                  mock.patch.dict(coordinator.os.environ, {"PREVIEW_RESULT_PATH": str(path)})):
                coordinator.run_branch()
            result = json.loads(path.read_text())
        self.assertEqual(result["selected_sha"], "a" * 40)
        self.assertEqual(result["final_sha"], "b" * 40)
        self.assertEqual(result["platform_ref"], "main")
        self.assertEqual(result["date"], "20261008")
        self.assertEqual(result["status"], "published")

    def test_failure_never_emits_a_success_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "result.json"
            with (mock.patch.object(coordinator, "main", side_effect=RuntimeError("aggregate failed")),
                  mock.patch.dict(coordinator.os.environ, {"PREVIEW_RESULT_PATH": str(path)}),
                  self.assertRaisesRegex(RuntimeError, "aggregate failed")):
                coordinator.run_branch()
            self.assertFalse(path.exists())

if __name__ == "__main__":
    unittest.main()
