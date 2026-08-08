#!/usr/bin/env python3

"""Resolve a platform aggregate version to its six component tags."""

from __future__ import annotations

import pathlib
import re
import sys


UNITS = ("accelerator", "connector", "sandboxer", "orchestrator", "runtime", "vmlinux")
AGGREGATE_RE = re.compile(
    r"^release-(v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))"
    r"(?:-preview\.([0-9]{8}))?$"
)


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"selection: {message}")


def read_simple_yaml(path: pathlib.Path) -> dict[str, object]:
    result: dict[str, object] = {}
    components: dict[str, str] = {}
    section = ""
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        if line == "components:":
            section = "components"
            continue
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*):[ ]+([^ ]+)", line.lstrip())
        if match is None:
            fail(f"unsupported syntax in {path}:{number}")
        key, value = match.groups()
        if raw.startswith("  ") and section == "components":
            if key in components:
                fail(f"duplicate component {key} in {path}")
            components[key] = value
        elif not raw.startswith((" ", "\t")):
            if key in result:
                fail(f"duplicate key {key} in {path}")
            result[key] = value
            section = ""
        else:
            fail(f"unexpected indentation in {path}:{number}")
    result["components"] = components
    return result


def resolve(root: pathlib.Path, version: str) -> tuple[str, dict[str, str]]:
    if AGGREGATE_RE.fullmatch(version) is None:
        fail("invalid aggregate version")
    path = root / "releases" / f"{version}.yaml"
    if not path.is_file():
        path = root / "releases" / "history" / f"{version}.yaml"
    if not path.is_file():
        fail(f"release manifest not found: {path}")
    config = read_simple_yaml(path)
    if set(config) != {"version", "previous", "components"}:
        fail(f"top-level keys in {path} must be exactly: version, previous, components")
    if config.get("version") != version:
        fail(f"version in {path} does not match {version}")
    previous = config.get("previous")
    if not isinstance(previous, str) or AGGREGATE_RE.fullmatch(previous) is None:
        fail(f"previous in {path} must be an aggregate version")
    if previous == version:
        fail(f"previous in {path} must differ from version")
    components = config["components"]
    assert isinstance(components, dict)
    if set(components) != set(UNITS):
        fail(f"components in {path} must be exactly: {', '.join(UNITS)}")
    return previous, {unit: str(components[unit]) for unit in UNITS}


def main() -> None:
    if len(sys.argv) not in (3, 4):
        fail("usage: selection.py <platform-root> <release-version> [--previous]")
    if len(sys.argv) == 4 and sys.argv[3] != "--previous":
        fail("usage: selection.py <platform-root> <release-version> [--previous]")
    previous, selected = resolve(pathlib.Path(sys.argv[1]), sys.argv[2])
    if len(sys.argv) == 4:
        print(previous)
        return
    for unit in UNITS:
        print(f"{unit}\t{selected[unit]}")


if __name__ == "__main__":
    main()
