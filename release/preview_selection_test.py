#!/usr/bin/env python3

"""Unit tests for branch-aware Daily Preview source selection."""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest


MODULE_PATH = pathlib.Path(__file__).with_name("preview-selection.py")
SPEC = importlib.util.spec_from_file_location("preview_selection", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
preview_selection = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preview_selection
SPEC.loader.exec_module(preview_selection)


class Repository:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "release-test")
        self.git("config", "user.email", "release-test@example.invalid")

    def close(self) -> None:
        self.temporary.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    def commit(self, value: str) -> str:
        (self.root / "state").write_text(value, encoding="utf-8")
        self.git("add", "state")
        self.git("commit", "-qm", value)
        return self.git("rev-parse", "HEAD")


class PreviewSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = Repository()

    def tearDown(self) -> None:
        self.repository.close()

    def resolve(
        self,
        source_ref: str,
        configured: str,
        usable: list[str],
    ) -> object:
        return preview_selection.resolve(
            self.repository.root,
            source_ref,
            "sandboxer",
            configured,
            usable,
            "20260830",
        )

    def test_source_branch_is_derived_per_component_version(self) -> None:
        self.assertEqual(
            preview_selection.component_source_ref(
                "release/v0.5.x", "sandboxer", "v0.3.5"
            ),
            "release/v0.3.x",
        )
        self.assertEqual(
            preview_selection.component_source_ref(
                "release/v0.5.x", "runtime", "runtime-v0.1.0"
            ),
            "release/v0.1.x",
        )
        self.assertEqual(
            preview_selection.component_source_ref("main", "sandboxer", "v9.8.7"),
            "main",
        )

    def test_newest_first_parent_tag_commit_wins_not_largest_version(self) -> None:
        old = self.repository.commit("old")
        self.repository.git("tag", "v1.2.10", old)
        newer = self.repository.commit("newer")
        self.repository.git("tag", "v1.2.3", newer)
        head = self.repository.commit("head")

        result = self.resolve("main", "v1.2.10", ["v1.2.10", "v1.2.3"])

        self.assertEqual(result.head, head)
        self.assertEqual(result.winner, "v1.2.3")
        self.assertEqual(result.winner_commit, newer)
        self.assertEqual(result.action, "publish")
        self.assertEqual(result.selected, "v1.2.4-preview.20260830")

    def test_multiple_tags_on_winning_commit_use_semver_order(self) -> None:
        commit = self.repository.commit("tagged")
        for tag in ("v1.2.3", "v1.2.4-preview.20260829", "v1.2.4-preview.20260830"):
            self.repository.git("tag", tag, commit)

        result = self.resolve(
            "main",
            "v1.2.3",
            ["v1.2.3", "v1.2.4-preview.20260829", "v1.2.4-preview.20260830"],
        )

        self.assertEqual(result.winner, "v1.2.4-preview.20260830")
        self.assertEqual(result.action, "reuse")

    def test_preview_behind_head_keeps_core_and_changes_date(self) -> None:
        tagged = self.repository.commit("preview")
        self.repository.git("tag", "v0.3.6-preview.20260829", tagged)
        self.repository.commit("head")

        result = self.resolve(
            "main",
            "v0.3.6-preview.20260829",
            ["v0.3.6-preview.20260829"],
        )

        self.assertEqual(result.selected, "v0.3.6-preview.20260830")

    def test_release_branch_filters_tags_to_its_version_line(self) -> None:
        matching = self.repository.commit("matching")
        self.repository.git("tag", "v1.2.4", matching)
        other = self.repository.commit("other line")
        self.repository.git("tag", "v1.3.0", other)
        head = self.repository.commit("head")
        self.repository.git("branch", "release/v1.2.x", head)

        result = self.resolve(
            "release/v1.2.x",
            "v1.2.4",
            ["v1.2.4", "v1.3.0"],
        )

        self.assertEqual(result.winner, "v1.2.4")
        self.assertEqual(result.selected, "v1.2.5-preview.20260830")

    def test_configured_tag_must_be_on_first_parent_line(self) -> None:
        base = self.repository.commit("base")
        self.repository.git("tag", "v1.2.0", base)
        self.repository.git("switch", "-qc", "side")
        side = self.repository.commit("side")
        self.repository.git("tag", "v1.2.1", side)
        self.repository.git("switch", "main")
        self.repository.commit("main")

        with self.assertRaisesRegex(
            preview_selection.SelectionError, "not on the first-parent source line"
        ):
            self.resolve("main", "v1.2.1", ["v1.2.0", "v1.2.1"])


if __name__ == "__main__":
    unittest.main()
