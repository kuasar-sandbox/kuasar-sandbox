#!/usr/bin/env python3

"""Resolve a release unit from one selected component source branch."""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
from dataclasses import dataclass
from typing import Iterable, NoReturn


COMPONENT_UNITS = {"accelerator", "connector", "sandboxer", "orchestrator"}
ALL_UNITS = COMPONENT_UNITS | {"runtime", "vmlinux"}
PLATFORM_RELEASE_BRANCH_RE = re.compile(
    r"^release/v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.x$"
)
TAG_RE = re.compile(
    r"^(?P<prefix>(?:runtime-|vmlinux-)?v)"
    r"(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-preview\.(?P<preview>[0-9]{8}))?$"
)


class SelectionError(ValueError):
    """A source branch cannot produce a safe Daily Preview selection."""


@dataclass(frozen=True)
class UnitTag:
    raw: str
    prefix: str
    major: int
    minor: int
    patch: int
    preview: int | None

    @property
    def is_preview(self) -> bool:
        return self.preview is not None

    @property
    def version_line(self) -> tuple[int, int]:
        return self.major, self.minor

    @property
    def same_commit_order(self) -> tuple[int, int, int, int, int]:
        # A stable tag sorts after previews of the same core version.
        stable = 1 if self.preview is None else 0
        preview = self.preview if self.preview is not None else 0
        return self.major, self.minor, self.patch, stable, preview


@dataclass(frozen=True)
class Resolution:
    source_ref: str
    head: str
    winner: str
    winner_commit: str
    action: str
    selected: str


def fail(message: str) -> NoReturn:
    raise SystemExit(f"preview-selection: {message}")


def unit_prefix(unit: str) -> str:
    if unit in COMPONENT_UNITS:
        return "v"
    if unit == "runtime":
        return "runtime-v"
    if unit == "vmlinux":
        return "vmlinux-v"
    raise SelectionError(f"unknown release unit: {unit}")


def parse_tag(unit: str, value: str) -> UnitTag:
    expected_prefix = unit_prefix(unit)
    match = TAG_RE.fullmatch(value)
    if match is None or match.group("prefix") != expected_prefix:
        raise SelectionError(f"invalid {unit} tag: {value}")
    preview = match.group("preview")
    return UnitTag(
        raw=value,
        prefix=expected_prefix,
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        preview=int(preview) if preview is not None else None,
    )


def component_source_ref(platform_ref: str, unit: str, configured: str) -> str:
    parsed = parse_tag(unit, configured)
    if platform_ref == "main":
        return "main"
    if PLATFORM_RELEASE_BRANCH_RE.fullmatch(platform_ref) is None:
        raise SelectionError(f"unsupported platform source ref: {platform_ref}")
    return f"release/v{parsed.major}.{parsed.minor}.x"


def preview_candidate(unit: str, winner: str, date: str) -> str:
    parsed = parse_tag(unit, winner)
    if re.fullmatch(r"[0-9]{8}", date) is None:
        raise SelectionError("preview date must match YYYYMMDD")
    patch = parsed.patch if parsed.is_preview else parsed.patch + 1
    return f"{parsed.prefix}{parsed.major}.{parsed.minor}.{patch}-preview.{date}"


def run_git(root: pathlib.Path, *args: str, allow_failure: bool = False) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0 and not allow_failure:
        detail = result.stderr.strip() or "git command failed"
        raise SelectionError(detail)
    return result.stdout.strip() if result.returncode == 0 else ""


def first_parent_commits(root: pathlib.Path, source_ref: str) -> list[str]:
    output = run_git(root, "rev-list", "--first-parent", source_ref)
    commits = output.splitlines()
    if not commits:
        raise SelectionError(f"source ref has no commits: {source_ref}")
    return commits


def tags_at(root: pathlib.Path, commit: str) -> list[str]:
    output = run_git(root, "tag", "--points-at", commit)
    return output.splitlines() if output else []


def usable_tags_at(
    root: pathlib.Path,
    commit: str,
    unit: str,
    source_ref: str,
    usable: set[str],
) -> list[UnitTag]:
    release_line: tuple[int, int] | None = None
    if source_ref != "main":
        match = PLATFORM_RELEASE_BRANCH_RE.fullmatch(source_ref)
        if match is None:
            raise SelectionError(f"invalid component release branch: {source_ref}")
        branch_version = re.search(r"v([0-9]+)\.([0-9]+)\.x$", source_ref)
        assert branch_version is not None
        release_line = int(branch_version.group(1)), int(branch_version.group(2))

    result: list[UnitTag] = []
    for value in tags_at(root, commit):
        if value not in usable:
            continue
        try:
            parsed = parse_tag(unit, value)
        except SelectionError:
            continue
        if release_line is not None and parsed.version_line != release_line:
            continue
        result.append(parsed)
    return result


def resolve(
    root: pathlib.Path,
    source_ref: str,
    unit: str,
    configured: str,
    usable_tags: Iterable[str],
    date: str,
) -> Resolution:
    parse_tag(unit, configured)
    usable = set(usable_tags)
    commits = first_parent_commits(root, source_ref)
    positions = {commit: index for index, commit in enumerate(commits)}
    head = commits[0]

    winner: UnitTag | None = None
    winner_commit = ""
    for commit in commits:
        candidates = usable_tags_at(root, commit, unit, source_ref, usable)
        if candidates:
            winner = max(candidates, key=lambda item: item.same_commit_order)
            winner_commit = commit
            break
    if winner is None:
        raise SelectionError(f"no usable {unit} release tag is reachable from {source_ref}")

    if configured in usable:
        configured_commit = run_git(
            root, "rev-list", "-n", "1", configured, allow_failure=True
        )
        if configured_commit:
            if configured_commit not in positions:
                raise SelectionError(
                    f"configured {unit} tag is not on the first-parent source line: {configured}"
                )
            configured_parsed = parse_tag(unit, configured)
            configured_position = positions[configured_commit]
            winner_position = positions[winner_commit]
            if configured_position < winner_position:
                winner = configured_parsed
                winner_commit = configured_commit
            elif configured_position == winner_position:
                winner = max(
                    (winner, configured_parsed),
                    key=lambda item: item.same_commit_order,
                )

    if winner_commit == head:
        return Resolution(source_ref, head, winner.raw, winner_commit, "reuse", winner.raw)
    selected = preview_candidate(unit, winner.raw, date)
    return Resolution(source_ref, head, winner.raw, winner_commit, "publish", selected)


def read_usable_tags(path: pathlib.Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    source = subparsers.add_parser("source-ref")
    source.add_argument("platform_ref")
    source.add_argument("unit")
    source.add_argument("configured")

    candidate = subparsers.add_parser("candidate")
    candidate.add_argument("unit")
    candidate.add_argument("winner")
    candidate.add_argument("date")

    resolver = subparsers.add_parser("resolve")
    resolver.add_argument("repository", type=pathlib.Path)
    resolver.add_argument("source_ref")
    resolver.add_argument("unit")
    resolver.add_argument("configured")
    resolver.add_argument("usable_tags", type=pathlib.Path)
    resolver.add_argument("date")

    args = parser.parse_args()
    try:
        if args.command == "source-ref":
            print(component_source_ref(args.platform_ref, args.unit, args.configured))
        elif args.command == "candidate":
            print(preview_candidate(args.unit, args.winner, args.date))
        else:
            result = resolve(
                args.repository,
                args.source_ref,
                args.unit,
                args.configured,
                read_usable_tags(args.usable_tags),
                args.date,
            )
            print(
                "\t".join(
                    (
                        result.source_ref,
                        result.head,
                        result.winner,
                        result.winner_commit,
                        result.action,
                        result.selected,
                    )
                )
            )
    except SelectionError as error:
        fail(str(error))


if __name__ == "__main__":
    main()
