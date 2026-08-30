#!/usr/bin/env python3

"""Resolve platform aggregate selections from the two maintained manifests."""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import NoReturn


UNITS = ("accelerator", "connector", "sandboxer", "orchestrator", "runtime", "vmlinux")
STABLE_RE = re.compile(
    r"^release-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
AGGREGATE_RE = re.compile(
    r"^release-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-preview\.[0-9]{8})?$"
)
PREVIEW_RE = re.compile(r"^preview\.[0-9]{8}$")
COMPONENT_RE = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-preview\.[0-9]{8})?$"
)
RUNTIME_RE = re.compile(
    r"^runtime-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-preview\.[0-9]{8})?$"
)
VMLINUX_RE = re.compile(
    r"^vmlinux-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-preview\.[0-9]{8})?$"
)


@dataclass(frozen=True, order=True)
class AggregateVersion:
    """A stable aggregate version used for maintained-state ordering."""

    major: int
    minor: int
    patch: int


class ManifestError(ValueError):
    """A maintained manifest does not satisfy the release-state schema."""


def fail(message: str) -> NoReturn:
    raise SystemExit(f"selection: {message}")


def read_simple_yaml(text: str, source: str) -> dict[str, object]:
    result: dict[str, object] = {}
    components: dict[str, str] = {}
    section = ""
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        if line == "components:":
            section = "components"
            continue
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*):[ ]+([^ ]+)", line.lstrip())
        if match is None:
            raise ManifestError(f"unsupported syntax in {source}:{number}")
        key, value = match.groups()
        if raw.startswith("  ") and section == "components":
            if key in components:
                raise ManifestError(f"duplicate component {key} in {source}")
            components[key] = value
        elif not raw.startswith((" ", "\t")):
            if key in result:
                raise ManifestError(f"duplicate key {key} in {source}")
            result[key] = value
            section = ""
        else:
            raise ManifestError(f"unexpected indentation in {source}:{number}")
    result["components"] = components
    return result


def require_stable(value: object, field: str, source: str) -> str:
    if not isinstance(value, str) or STABLE_RE.fullmatch(value) is None:
        raise ManifestError(f"{field} in {source} must be a stable aggregate version")
    return value


def aggregate_version(value: str) -> AggregateVersion:
    match = re.fullmatch(r"release-v([0-9]+)\.([0-9]+)\.([0-9]+)", value)
    if match is None:
        raise ManifestError(f"invalid stable aggregate version: {value}")
    return AggregateVersion(*(int(part) for part in match.groups()))


def validate_components(
    config: dict[str, object], source: str, preview: bool
) -> dict[str, str]:
    components = config["components"]
    assert isinstance(components, dict)
    if set(components) != set(UNITS):
        raise ManifestError(f"components in {source} must be exactly: {', '.join(UNITS)}")
    selected = {unit: str(components[unit]) for unit in UNITS}
    for unit, value in selected.items():
        pattern = (
            COMPONENT_RE
            if unit in ("accelerator", "connector", "sandboxer", "orchestrator")
            else RUNTIME_RE
            if unit == "runtime"
            else VMLINUX_RE
        )
        if pattern.fullmatch(value) is None:
            raise ManifestError(f"invalid {unit} version in {source}: {value}")
        if not preview and "-preview." in value:
            raise ManifestError(
                f"formal selection in {source} must use a stable {unit} version"
            )
    return selected


def parse_manifest(
    text: str, source: str, preview: bool
) -> tuple[str, str | None, dict[str, str]]:
    config = read_simple_yaml(text, source)
    required = {"version", "components"}
    optional = {"previous_version"}
    if preview:
        required.add("preview_version")
        optional.add("previous_preview_version")
    keys = set(config)
    if not required <= keys or not keys <= required | optional:
        expected = ", ".join(sorted(required | optional))
        raise ManifestError(f"top-level keys in {source} must be: {expected}")

    version = require_stable(config.get("version"), "version", source)
    previous_version: str | None = None
    if "previous_version" in config:
        previous_version = require_stable(config["previous_version"], "previous_version", source)
        if aggregate_version(previous_version) >= aggregate_version(version):
            raise ManifestError(f"previous_version in {source} must be older than version")

    aggregate = version
    previous = previous_version
    if preview:
        preview_version = config.get("preview_version")
        if not isinstance(preview_version, str) or PREVIEW_RE.fullmatch(preview_version) is None:
            raise ManifestError(f"preview_version in {source} must match preview.YYYYMMDD")
        aggregate = f"{version}-{preview_version}"
        if "previous_preview_version" in config:
            previous_preview = config["previous_preview_version"]
            if not isinstance(previous_preview, str) or PREVIEW_RE.fullmatch(previous_preview) is None:
                raise ManifestError(
                    f"previous_preview_version in {source} must match preview.YYYYMMDD"
                )
            if previous_preview == preview_version:
                raise ManifestError(
                    f"previous_preview_version in {source} must differ from preview_version"
                )
            if previous_preview >= preview_version:
                raise ManifestError(
                    f"previous_preview_version in {source} must be older than preview_version"
                )
            previous = f"{version}-{previous_preview}"

    return aggregate, previous, validate_components(config, source, preview)


def validate_current_manifests(root: pathlib.Path) -> None:
    release_path = root / "releases/release.yaml"
    preview_path = root / "releases/daily-preview.yaml"
    if not release_path.is_file() or not preview_path.is_file():
        raise ManifestError("both maintained release manifests must exist")
    release = parse_manifest(
        release_path.read_text(encoding="utf-8"), str(release_path), False
    )
    preview = parse_manifest(
        preview_path.read_text(encoding="utf-8"), str(preview_path), True
    )
    release_base = release[0]
    preview_base = preview[0].split("-preview.", 1)[0]
    if aggregate_version(preview_base) < aggregate_version(release_base):
        raise ManifestError(
            "daily-preview.yaml version must not be older than release.yaml version"
        )


def git_snapshots(root: pathlib.Path, relative: pathlib.Path) -> list[tuple[str, str]]:
    log = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            "--first-parent",
            "--format=%H",
            "--",
            relative.as_posix(),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if log.returncode != 0:
        return []
    snapshots: list[tuple[str, str]] = []
    for commit in log.stdout.splitlines():
        shown = subprocess.run(
            ["git", "-C", str(root), "show", f"{commit}:{relative.as_posix()}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if shown.returncode == 0:
            snapshots.append((commit, shown.stdout))
    return snapshots


def resolve(root: pathlib.Path, version: str) -> tuple[str | None, dict[str, str]]:
    if AGGREGATE_RE.fullmatch(version) is None:
        fail("invalid aggregate version")
    try:
        validate_current_manifests(root)
    except ManifestError as error:
        fail(str(error))
    preview = "-preview." in version
    relative = pathlib.Path("releases/daily-preview.yaml" if preview else "releases/release.yaml")
    path = root / relative
    if not path.is_file():
        fail(f"maintained release manifest is missing: {path}")

    try:
        current = parse_manifest(path.read_text(encoding="utf-8"), str(path), preview)
    except ManifestError as error:
        fail(str(error))
    if current[0] == version:
        return current[1], current[2]

    for commit, text in git_snapshots(root, relative):
        source = f"{commit}:{relative.as_posix()}"
        try:
            historical = parse_manifest(text, source, preview)
        except ManifestError:
            # Commits before the two-file model are not release-state snapshots.
            continue
        if historical[0] == version:
            return historical[1], historical[2]
    fail(f"aggregate selection not found in {relative.as_posix()} history: {version}")


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[2] == "--validate-current":
        try:
            validate_current_manifests(pathlib.Path(sys.argv[1]))
        except ManifestError as error:
            fail(str(error))
        return
    if len(sys.argv) not in (3, 4):
        fail(
            "usage: selection.py <platform-root> "
            "<release-version> [--previous] | --validate-current"
        )
    if len(sys.argv) == 4 and sys.argv[3] != "--previous":
        fail("usage: selection.py <platform-root> <release-version> [--previous]")
    previous, selected = resolve(pathlib.Path(sys.argv[1]), sys.argv[2])
    if len(sys.argv) == 4:
        if previous is not None:
            print(previous)
        return
    for unit in UNITS:
        print(f"{unit}\t{selected[unit]}")


if __name__ == "__main__":
    main()
