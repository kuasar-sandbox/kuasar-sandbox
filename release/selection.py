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
TEST_OWNERS = ("accelerator", "connector", "guest-runtime", "sandboxer", "orchestrator")
STABLE_RE = re.compile(
    r"^release-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
AGGREGATE_RE = re.compile(
    r"^release-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-preview\.[0-9]{8}(?:\.[1-9][0-9]*)?)?$"
)
PREVIEW_RE = re.compile(r"^preview\.[0-9]{8}(?:\.[1-9][0-9]*)?$")
COMPONENT_RE = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-preview\.[0-9]{8}(?:\.[1-9][0-9]*)?)?$"
)
RUNTIME_RE = re.compile(
    r"^runtime-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-preview\.[0-9]{8}(?:\.[1-9][0-9]*)?)?$"
)
VMLINUX_RE = re.compile(
    r"^vmlinux-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-preview\.[0-9]{8}(?:\.[1-9][0-9]*)?)?$"
)


@dataclass(frozen=True, order=True)
class AggregateVersion:
    """A stable aggregate version used for maintained-state ordering."""

    major: int
    minor: int
    patch: int


class ManifestError(ValueError):
    """A maintained manifest does not satisfy the release-state schema."""


def check_notes_input(config: dict[str, object], source: str) -> None:
    """Check an editable text input, never its relationship to acceptance."""
    fields = config.get('notes', {})
    validate_complete_notes(fields, source)
    if not fields.get('body'):
        raise ManifestError('release manifest has no notes.body')


def fail(message: str) -> NoReturn:
    raise SystemExit(f"selection: {message}")


def split_notes(text: str, source: str) -> tuple[str, str]:
    """Keep literal Markdown separate from the selection's scalar reader."""
    lines = re.findall(r'[^\n]*\n|[^\n]+$', text)
    starts = [i for i, line in enumerate(lines)
              if re.fullmatch(r"notes:[ ]*(?:#.*)?", line.rstrip("\r\n"))]
    if not starts:
        return text, ""
    if len(starts) != 1:
        raise ManifestError(f"duplicate notes section in {source}")
    start = starts[0]
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "#"))):
        end += 1
    return "".join(lines[:start] + lines[end:]), "".join(lines[start:end])


def parse_notes(block: str, source: str) -> dict[str, str]:
    # YAML normalizes Windows line endings; match Path.read_text preparation
    # when publication reads raw committed bytes through git show.
    block = block.replace('\r\n', '\n')
    fields = {}
    lines = re.findall(r'[^\n]*\n|[^\n]+$', block)
    i = 1
    while i < len(lines):
        line = lines[i].removesuffix('\n')
        i += 1
        if not line.strip() and line.strip(' '):
            raise ManifestError(f'invalid whitespace in notes in {source}')
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"  (body|upgrade_notice|aggregate_sha|mode|selection_sha256): +([^ #]+)(?: +#.*)? *", line)
        if not match:
            raise ManifestError(f"invalid notes field in {source}")
        key, value = match.groups()
        if key in fields:
            raise ManifestError(f"duplicate notes.{key} in {source}")
        if key == "mode":
            if value not in ("auto", "fixed"):
                raise ManifestError(f"notes.mode in {source} must be auto or fixed")
        elif key in ("aggregate_sha", "selection_sha256"):
            # Historical bookkeeping has no generation, text-routing or
            # publication authority. Never require or emit these fields.
            continue
        else:
            if value not in ("|", "|-"):
                raise ManifestError(f"notes.{key} in {source} requires a literal Markdown block")
            body = []
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith("    ")):
                raw = lines[i]
                content = raw.removesuffix('\n')
                if not content.strip() and content.strip(' '):
                    raise ManifestError(f'invalid whitespace in notes.{key} in {source}')
                body.append(raw[4:] if raw.startswith('    ') else ('\n' if raw.endswith('\n') else ''))
                i += 1
            literal = ''.join(body)
            value = literal.rstrip('\n') + ('\n' if value == '|' and literal.endswith('\n') else '')
            if not value.strip() or any(marker in value for marker in (
                    "<!-- kuasar-integration-validation", "<!-- kuasar-release-notice", "<!-- /kuasar-release-notice")):
                raise ManifestError(f"empty or reserved marker in notes.{key} in {source}")
        fields[key] = value
    if not ({"body", "upgrade_notice"} & fields.keys()):
        raise ManifestError(f"notes in {source} requires body or upgrade_notice")
    validate_complete_notes(fields, source)
    return fields


def validate_complete_notes(fields: dict[str, str], source: str) -> None:
    """Explicit-mode notes are a complete authored prefix, never an insertion."""
    if "mode" not in fields:
        return
    if fields["mode"] not in ("auto", "fixed"):
        raise ManifestError(f"notes.mode in {source} must be auto or fixed")
    if "upgrade_notice" in fields:
        raise ManifestError(f"migrate notes.upgrade_notice into notes.body before setting notes.mode in {source}")
    body = fields.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ManifestError(f"notes.mode in {source} requires a nonempty complete notes.body")
    if any(marker in body for marker in ("<!-- kuasar-integration-validation",
            "<!-- kuasar-release-notice", "<!-- /kuasar-release-notice")):
        raise ManifestError(f"reserved marker in notes.body in {source}")


def read_simple_yaml(text: str, source: str) -> dict[str, object]:
    text = text.replace('\r\n', '\n')
    result: dict[str, object] = {}
    text, block = split_notes(text, source)
    if block:
        result["notes"] = parse_notes(block, source)
    section = ""
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        if line in ("components:", "component_next_versions:", "test_revisions:"):
            section = line[:-1]
            if section in result:
                raise ManifestError(f"duplicate section {section} in {source}")
            result[section] = {}
            continue
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*):[ ]+([^ ]+)", line.lstrip())
        if match is None:
            raise ManifestError(f"unsupported syntax in {source}:{number}")
        key, value = match.groups()
        if key == "notes":
            raise ManifestError(f"notes must be a mapping in {source}")
        if raw.startswith("  ") and section:
            entries = result[section]
            if key in entries:
                raise ManifestError(f"duplicate {section} entry {key} in {source}")
            entries[key] = value
        elif not raw.startswith((" ", "\t")):
            if key in result:
                raise ManifestError(f"duplicate key {key} in {source}")
            result[key] = value
            section = ""
        else:
            raise ManifestError(f"unexpected indentation in {source}:{number}")
    result.setdefault("components", {})
    return result


def historical_test_revisions(config: dict[str, object], source: str) -> dict[str, str]:
    """Read immutable legacy evidence, never a new release source selection."""
    pins = config.get("test_revisions")
    if not isinstance(pins, dict) or set(pins) != set(TEST_OWNERS):
        raise ManifestError(f"test_revisions in {source} must pin every component test owner")
    for owner, sha in pins.items():
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            raise ManifestError(f"test revision for {owner} in {source} must be an exact SHA")
    return dict(pins)


DELIVERY = 'workbench-v1'
NODE_INFRA_DELIVERY = 'node-infra-v2'
NODE_INFRA_DELIVERIES = ('node-infra-v1', NODE_INFRA_DELIVERY)
MODERN_DELIVERIES = (DELIVERY, *NODE_INFRA_DELIVERIES)


def delivery(config: dict[str, object], source: str) -> str:
    """Read the committed contract, never infer history from missing assets."""
    value = config.get('delivery', 'historical')
    if value not in ('historical', *MODERN_DELIVERIES) or ('delivery' in config and value == 'historical'):
        raise ManifestError(f'unsupported delivery contract in {source}: {value}')
    return str(value)


def workbench_archive(version: str, arch: str) -> str:
    if AGGREGATE_RE.fullmatch(version) is None or arch not in ('x86_64', 'aarch64'):
        raise ManifestError('invalid workbench aggregate version or architecture')
    return f'workbench-{arch}-{version.removeprefix("release-")}.tar.gz'


def node_infra_archive(version: str, arch: str) -> str:
    """The aggregate owns this image; it is not a seventh component unit."""
    if AGGREGATE_RE.fullmatch(version) is None or arch not in ('x86_64', 'aarch64'):
        raise ManifestError('invalid node-infra version or architecture')
    return f'node-infra-{version}-linux-' + {'x86_64': 'amd64', 'aarch64': 'arm64'}[arch] + '.oci.tar.gz'


def node_infra_docker_archive(version: str, arch: str) -> str:
    return node_infra_archive(version, arch).replace(".oci.tar.gz", ".docker.tar.gz")


def node_infra_deploy_archive(version: str) -> str:
    if AGGREGATE_RE.fullmatch(version) is None:
        raise ManifestError('invalid node-infra aggregate version')
    return f'node-infra-deploy-{version}.tar.gz'


def delivery_assets(version: str, contract: str, arches=('x86_64', 'aarch64')) -> set[str]:
    """Additional aggregate assets, preserving immutable historical contracts."""
    if contract == 'historical':
        return set()
    if contract not in MODERN_DELIVERIES:
        raise ManifestError('unsupported aggregate delivery contract: ' + contract)
    names = {workbench_archive(version, arch) for arch in arches}
    if contract in NODE_INFRA_DELIVERIES:
        names |= {node_infra_archive(version, arch) for arch in arches}
        names.add(node_infra_deploy_archive(version))
        if contract == NODE_INFRA_DELIVERY:
            names |= {node_infra_docker_archive(version, arch) for arch in arches}
    return names


def require_stable(value: object, field: str, source: str) -> str:
    if not isinstance(value, str) or STABLE_RE.fullmatch(value) is None:
        raise ManifestError(f"{field} in {source} must be a stable aggregate version")
    return value


def aggregate_version(value: str) -> AggregateVersion:
    match = re.fullmatch(r"release-v([0-9]+)\.([0-9]+)\.([0-9]+)", value)
    if match is None:
        raise ManifestError(f"invalid stable aggregate version: {value}")
    return AggregateVersion(*(int(part) for part in match.groups()))


def preview_order(value: str) -> tuple[int, int]:
    if PREVIEW_RE.fullmatch(value) is None:
        raise ManifestError(f"invalid Preview suffix: {value}")
    parts = value.split(".")
    return int(parts[1]), int(parts[2]) if len(parts) == 3 else 0


def component_next_versions(config: dict[str, object], source: str) -> dict[str, str]:
    """Optional core floors for future Preview publication, never selected tags."""
    values = config.get("component_next_versions", {})
    if not isinstance(values, dict) or not set(values) <= set(UNITS) - {"vmlinux"}:
        raise ManifestError(f"component_next_versions in {source} must name only non-kernel units")
    targets = {unit: str(value) for unit, value in values.items()}
    for unit, value in targets.items():
        pattern = RUNTIME_RE if unit == "runtime" else COMPONENT_RE
        if pattern.fullmatch(value) is None or "-preview." in value:
            raise ManifestError(f"invalid {unit} next version in {source}: expected a stable core")
    return targets


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
    """Validate a NEW maintained selection. There is no test source override."""
    return _parse_manifest(text, source, preview, historical=False)


def parse_historical_manifest(
    text: str, source: str, preview: bool
) -> tuple[str, str | None, dict[str, str]]:
    """Read an immutable historical record without rewriting its provenance.

    Publication/packaging must use parse_manifest, even for an old version name.
    This reader does not authorize retrieving legacy test sources.
    """
    return _parse_manifest(text, source, preview, historical=True)


def _parse_manifest(text: str, source: str, preview: bool, *, historical: bool):
    config = read_simple_yaml(text, source)
    required = {"version", "components"}
    optional = {"previous_version", "delivery", "notes"}
    if historical:
        optional.add("test_revisions")
    elif "test_revisions" in config:
        raise ManifestError(f"test_revisions is historical evidence only, forbidden in new selection {source}")
    if preview:
        required.add("preview_version")
        optional.add("previous_preview_version")
        optional.add("component_next_versions")
    keys = set(config)
    if not required <= keys or not keys <= required | optional:
        expected = ", ".join(sorted(required | optional))
        raise ManifestError(f"top-level keys in {source} must be: {expected}")
    delivery(config, source)
    component_next_versions(config, source)

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
            raise ManifestError(f"preview_version in {source} must match preview.YYYYMMDD[.N]")
        aggregate = f"{version}-{preview_version}"
        if "previous_preview_version" in config:
            previous_preview = config["previous_preview_version"]
            if not isinstance(previous_preview, str) or PREVIEW_RE.fullmatch(previous_preview) is None:
                raise ManifestError(
                    f"previous_preview_version in {source} must match preview.YYYYMMDD[.N]"
                )
            if previous_preview == preview_version:
                raise ManifestError(
                    f"previous_preview_version in {source} must differ from preview_version"
                )
            if preview_order(previous_preview) >= preview_order(preview_version):
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


def git_snapshots(
    root: pathlib.Path, relative: pathlib.Path, ref: str = "HEAD"
) -> list[tuple[str, str]]:
    log = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            ref,
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


def git_commit(root: pathlib.Path, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else None


def resolve_record(
    root: pathlib.Path, version: str
) -> tuple[str, str | None, dict[str, str]]:
    if AGGREGATE_RE.fullmatch(version) is None:
        fail("invalid aggregate version")
    preview = '-preview.' in version
    relative = pathlib.Path('releases/daily-preview.yaml' if preview else 'releases/release.yaml')
    tag_commit = git_commit(root, 'refs/tags/' + version)
    if tag_commit is not None:
        source = f'refs/tags/{version}:{relative.as_posix()}'
        text = subprocess.check_output(['git', '-C', str(root), 'show', source], text=True)
        selected, previous, units = parse_historical_manifest(text, source, preview)
        if selected != version:
            fail('aggregate tag does not select its own version: ' + version)
        return tag_commit, previous, units
    # Unpublished assembly may use the current admitted source tree. Historical
    # versions must be materialized by their own tag, never first-parent main.
    validate_current_manifests(root)
    path = root / relative
    current = parse_manifest(path.read_text(encoding='utf-8'), str(path), preview)
    if current[0] != version:
        fail(f'aggregate tag is not available locally: {version}; fetch that exact tag before resolving it')
    return git_commit(root, 'HEAD') or '', current[1], current[2]


def resolve(root: pathlib.Path, version: str) -> tuple[str | None, dict[str, str]]:
    _, previous, components = resolve_record(root, version)
    return previous, components


def resolve_config(root: pathlib.Path, version: str) -> dict[str, object]:
    commit, _, _ = resolve_record(root, version)
    preview = "-preview." in version
    relative = "releases/daily-preview.yaml" if preview else "releases/release.yaml"
    if git_commit(root, 'refs/tags/' + version) is not None:
        text = subprocess.check_output(['git', '-C', str(root), 'show', f'refs/tags/{version}:{relative}'], text=True)
    else:
        text = (root / relative).read_text(encoding='utf-8')
    return read_simple_yaml(text, relative)


def resolve_new_selection(root: pathlib.Path, version: str) -> dict[str, str]:
    """Only a current tag-only manifest can authorize new source assembly."""
    validate_current_manifests(root)
    preview = "-preview." in version
    path = root / ("releases/daily-preview.yaml" if preview else "releases/release.yaml")
    selected, _, units = parse_manifest(path.read_text(encoding="utf-8"), str(path), preview)
    if selected != version:
        raise ManifestError("new assembly requires the current maintained selection; consume historical packages by tag")
    return units


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
            "<release-version> [--previous|--commit|--new|--delivery] | --validate-current"
        )
    if len(sys.argv) == 4 and sys.argv[3] not in ("--previous", "--commit", "--new", "--delivery"):
        fail(
            "usage: selection.py <platform-root> "
            "<release-version> [--previous|--commit|--new|--delivery]"
        )
    if len(sys.argv) == 4 and sys.argv[3] == '--delivery':
        try:
            print(delivery(resolve_config(pathlib.Path(sys.argv[1]), sys.argv[2]), sys.argv[2]))
        except ManifestError as error:
            fail(str(error))
        return
    if len(sys.argv) == 4 and sys.argv[3] == "--new":
        try:
            for unit, tag in resolve_new_selection(pathlib.Path(sys.argv[1]), sys.argv[2]).items():
                print(f"{unit}\t{tag}")
        except ManifestError as error:
            fail(str(error))
        return
    commit, previous, selected = resolve_record(
        pathlib.Path(sys.argv[1]), sys.argv[2]
    )
    if len(sys.argv) == 4:
        if sys.argv[3] == "--commit":
            if not commit:
                fail("cannot resolve current platform commit")
            print(commit)
        elif previous is not None:
            print(previous)
        return
    for unit in UNITS:
        print(f"{unit}\t{selected[unit]}")


if __name__ == "__main__":
    main()
