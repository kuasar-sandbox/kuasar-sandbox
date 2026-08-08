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


def resolve(root: pathlib.Path, version: str) -> dict[str, str]:
    match = AGGREGATE_RE.fullmatch(version)
    if match is None:
        fail("invalid aggregate version")
    base, preview_date = match.groups()
    if preview_date:
        suffix = f"-preview.{preview_date}"
        return {
            "accelerator": f"{base}{suffix}",
            "connector": f"{base}{suffix}",
            "sandboxer": f"{base}{suffix}",
            "orchestrator": f"{base}{suffix}",
            "runtime": f"runtime-{base}{suffix}",
            "vmlinux": f"vmlinux-{base}{suffix}",
        }

    path = root / "releases" / f"{version}.yaml"
    if not path.is_file():
        fail(f"stable selection not found: {path}")
    config = read_simple_yaml(path)
    if config.get("version") != version:
        fail(f"version in {path} does not match {version}")
    components = config["components"]
    assert isinstance(components, dict)
    if set(components) != set(UNITS):
        fail(f"components in {path} must be exactly: {', '.join(UNITS)}")
    return {unit: str(components[unit]) for unit in UNITS}


def main() -> None:
    if len(sys.argv) != 3:
        fail("usage: selection.py <platform-root> <release-version>")
    selected = resolve(pathlib.Path(sys.argv[1]), sys.argv[2])
    for unit in UNITS:
        print(f"{unit}\t{selected[unit]}")


if __name__ == "__main__":
    main()
