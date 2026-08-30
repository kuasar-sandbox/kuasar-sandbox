#!/usr/bin/env python3

"""Plan and converge canonical Daily Preview garbage collection."""

from __future__ import annotations

import datetime as dt
import base64
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Iterable, NoReturn


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "release"))
import preview_coordinator as coordinator  # noqa: E402


GRACE_DAYS = 7
STABLE_RE = re.compile(r"^release-v[0-9]+\.[0-9]+\.[0-9]+$")
AGGREGATE_PREVIEW_RE = re.compile(
    r"^release-v[0-9]+\.[0-9]+\.[0-9]+-preview\.[0-9]{8}$"
)


@dataclass(frozen=True, order=True)
class Candidate:
    repository: str
    unit: str
    tag: str
    source_sha: str | None
    release_id: int | None
    asset_count: int
    asset_bytes: int


class GCError(RuntimeError):
    pass


def fail(message: str) -> NoReturn:
    raise SystemExit(f"preview-gc: {message}")


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        raise GCError(result.stderr.strip() or "git command failed")
    return result.stdout.strip() if result.returncode == 0 else ""


def tag_index(repository: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in coordinator.paginated(
        f"repos/{repository}/git/matching-refs/tags/"
    ):
        ref = item.get("ref")
        target = item.get("object")
        if not isinstance(ref, str) or not ref.startswith("refs/tags/"):
            continue
        if not isinstance(target, dict) or target.get("type") != "commit":
            continue
        sha = target.get("sha")
        if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha):
            result[ref.removeprefix("refs/tags/")] = sha
    return result


def platform_release_status(
    tag: str,
    release: dict[str, Any] | None,
    sha: str | None,
) -> coordinator.ReleaseStatus:
    if release is None:
        return coordinator.ReleaseStatus(None, sha, False)
    assets = release.get("assets")
    expected: set[str] | None = None
    if sha is not None:
        try:
            expected = coordinator.platform_asset_names(tag, sha)
        except coordinator.Deferred:
            # Pre-contract aggregate Previews remain collectable only when the
            # canonical manifest history independently proves ownership.
            expected = None
    complete = (
        release.get("draft") is False
        and release.get("prerelease") is ("-preview." in tag)
        and isinstance(assets, list)
        and expected is not None
        and {str(asset.get("name")) for asset in assets} == expected
        and all(asset.get("state") == "uploaded" for asset in assets)
        and sha is not None
        and release.get("target_commitish") == sha
    )
    return coordinator.ReleaseStatus(release, sha, complete)


def stable_release(
    version: str,
    releases: dict[str, dict[str, Any]],
    tags: dict[str, str],
) -> dict[str, Any]:
    release = releases.get(version)
    if release is None:
        raise GCError(f"Stable aggregate does not exist: {version}")
    status = platform_release_status(version, release, tags.get(version))
    if not status.complete or release.get("prerelease") is not False:
        raise GCError(f"Stable aggregate violates the release contract: {version}")
    if status.tag_sha != git("rev-list", "-n", "1", version):
        raise GCError(f"Stable aggregate tag target is inconsistent: {version}")
    assets = release.get("assets")
    assert isinstance(assets, list)
    if not all(
        isinstance(asset.get("digest"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", asset["digest"])
        and isinstance(asset.get("size"), int)
        and asset["size"] > 0
        for asset in assets
    ):
        raise GCError(f"Stable aggregate assets lack verified digests: {version}")
    return release


def parse_snapshot(commit: str) -> tuple[str, dict[str, str]] | None:
    content = git("show", f"{commit}:releases/daily-preview.yaml", check=False)
    if not content:
        return None
    try:
        raw = coordinator.selection.read_simple_yaml(
            content, f"{commit}:releases/daily-preview.yaml"
        )
        if "preview_version" not in raw or "components" not in raw:
            return None
        aggregate, _, components = coordinator.selection.parse_manifest(
            content, f"{commit}:releases/daily-preview.yaml", True
        )
    except coordinator.selection.ManifestError as error:
        raise GCError(str(error)) from error
    return aggregate, components


def canonical_snapshots(stable_sha: str, version: str) -> dict[str, dict[str, str]]:
    commits = git(
        "log",
        "--first-parent",
        "--format=%H",
        stable_sha,
        "--",
        "releases/daily-preview.yaml",
    ).splitlines()
    snapshots: dict[str, dict[str, str]] = {}
    for commit in commits:
        snapshot = parse_snapshot(commit)
        if snapshot is None:
            continue
        aggregate, components = snapshot
        if aggregate.startswith(f"{version}-preview."):
            # git log is newest first; keep the final canonical selection when
            # one Preview date was committed more than once.
            snapshots.setdefault(aggregate, components)
    return snapshots


def all_platform_previews(
    releases: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        tag: item
        for tag, item in releases.items()
        if isinstance(item.get("tag_name"), str)
        and AGGREGATE_PREVIEW_RE.fullmatch(item["tag_name"])
        and item.get("draft") is False
        and item.get("prerelease") is True
    }


def protected_component_tags(
    platform_previews: dict[str, dict[str, Any]],
    platform_tags: dict[str, str],
    candidates: set[str],
) -> set[tuple[str, str]]:
    protected: set[tuple[str, str]] = set()
    for tag in platform_previews:
        if tag in candidates:
            continue
        sha = platform_tags.get(tag)
        if sha is None:
            raise GCError(f"retained aggregate Preview has no tag: {tag}")
        snapshot = parse_snapshot(sha)
        if snapshot is None or snapshot[0] != tag:
            raise GCError(f"retained aggregate Preview has no canonical manifest: {tag}")
        for unit, component_tag in snapshot[1].items():
            if "-preview." in component_tag:
                protected.add((unit, component_tag))
    return protected


def current_manifest_protection(
    closed_stables: set[str],
) -> set[tuple[str, str]]:
    protected: set[tuple[str, str]] = set()
    branches = coordinator.paginated(
        f"repos/{coordinator.PLATFORM_REPOSITORY}/branches?per_page=100"
    )
    for branch in branches:
        name = branch.get("name")
        if not isinstance(name, str) or (
            name != "main"
            and coordinator.preview_selection.PLATFORM_RELEASE_BRANCH_RE.fullmatch(name)
            is None
        ):
            continue
        state = coordinator.api(
            f"repos/{coordinator.PLATFORM_REPOSITORY}/contents/"
            f"releases/daily-preview.yaml?ref={coordinator.quote(name, safe='')}"
        )
        content = base64.b64decode(str(state["content"]).replace("\n", "")).decode()
        aggregate, _, components = coordinator.selection.parse_manifest(
            content, f"{name}:releases/daily-preview.yaml", True
        )
        if aggregate.split("-preview.", 1)[0] in closed_stables:
            continue
        for unit, tag in components.items():
            if "-preview." in tag:
                protected.add((unit, tag))
    return protected


def release_candidate(
    repository: str,
    unit: str,
    tag: str,
    component_state: coordinator.RepositoryState | None = None,
    platform_release: dict[str, Any] | None = None,
    platform_sha: str | None = None,
) -> Candidate | None:
    spec = coordinator.UNIT_BY_NAME.get(unit)
    if unit == "platform":
        release = platform_release
        sha = platform_sha
        status = platform_release_status(tag, release, sha)
    else:
        if spec is None:
            raise GCError(f"unknown release unit: {unit}")
        state = component_state or coordinator.RepositoryState(spec)
        status = state.status(tag)
        release = status.release
        sha = status.tag_sha
    if release is None and sha is None:
        return None
    if release is not None:
        draft = release.get("draft")
        prerelease = release.get("prerelease")
        if draft is not True and not (draft is False and prerelease is True):
            raise GCError(f"refusing non-Preview GC candidate: {repository} {tag}")
        if release.get("immutable") is True:
            raise GCError(f"immutable Preview cannot be collected: {repository} {tag}")
    source_sha = coordinator.recoverable_source_sha(status)
    if source_sha is None:
        raise GCError(f"Preview candidate has no recoverable source target: {repository} {tag}")
    assets = release.get("assets", []) if release is not None else []
    if not isinstance(assets, list) or any(
        not isinstance(asset, dict)
        or not isinstance(asset.get("size"), int)
        or asset["size"] < 0
        for asset in assets
    ):
        raise GCError(f"Preview candidate has invalid asset metadata: {repository} {tag}")
    release_id = release.get("id") if release is not None else None
    if release_id is not None and (not isinstance(release_id, int) or release_id <= 0):
        raise GCError(f"Preview has an invalid Release ID: {repository} {tag}")
    return Candidate(
        repository,
        unit,
        tag,
        source_sha,
        release_id,
        len(assets),
        sum(int(asset.get("size", 0)) for asset in assets),
    )


def active_release_titles(repository: str, workflow: str) -> set[str]:
    return {
        str(item["display_title"])
        for item in coordinator.paginated(
            f"repos/{repository}/actions/workflows/{workflow}/runs?"
            "event=workflow_dispatch&per_page=100",
            "workflow_runs",
        )
        if isinstance(item.get("display_title"), str)
        and coordinator.run_active(item)
    }


def plan(version: str) -> tuple[list[Candidate], dict[str, Any]]:
    if STABLE_RE.fullmatch(version) is None:
        raise GCError("stable_version must match release-vMAJOR.MINOR.PATCH")
    platform_releases = {
        item["tag_name"]: item
        for item in coordinator.paginated(
            f"repos/{coordinator.PLATFORM_REPOSITORY}/releases?per_page=100"
        )
        if isinstance(item.get("tag_name"), str)
    }
    platform_tags = tag_index(coordinator.PLATFORM_REPOSITORY)
    stable = stable_release(version, platform_releases, platform_tags)
    stable_sha = platform_tags.get(version)
    assert stable_sha is not None
    snapshots = canonical_snapshots(stable_sha, version)
    platform_previews = all_platform_previews(platform_releases)
    canonical_aggregates = set(snapshots)
    protected = protected_component_tags(
        platform_previews, platform_tags, canonical_aggregates
    )
    closed_stables = {
        tag
        for tag, item in platform_releases.items()
        if STABLE_RE.fullmatch(tag)
        and item.get("prerelease") is False
        and platform_release_status(tag, item, platform_tags.get(tag)).complete
    }
    protected.update(current_manifest_protection(closed_stables))

    component_refs: set[tuple[str, str]] = set()
    for components in snapshots.values():
        for unit, tag in components.items():
            if "-preview." in tag and (unit, tag) not in protected:
                component_refs.add((unit, tag))

    candidates: list[Candidate] = []
    component_states = {
        unit: coordinator.RepositoryState(coordinator.UNIT_BY_NAME[unit])
        for unit in {name for name, _ in component_refs}
    }
    active_titles = {
        (spec.repository, spec.workflow): active_release_titles(
            spec.repository, spec.workflow
        )
        for spec in {
            coordinator.UNIT_BY_NAME[name]
            for name, _ in component_refs
        }
    }
    for unit, tag in sorted(component_refs):
        spec = coordinator.UNIT_BY_NAME[unit]
        candidate = release_candidate(
            spec.repository, unit, tag, component_states[unit]
        )
        if candidate is not None:
            if f"Release {tag}" in active_titles[(spec.repository, spec.workflow)]:
                raise GCError(f"release workflow is active: {spec.repository} {tag}")
            candidates.append(candidate)
    platform_active = active_release_titles(
        coordinator.PLATFORM_REPOSITORY, "aggregate-release.yml"
    )
    for tag in sorted(canonical_aggregates):
        candidate = release_candidate(
            coordinator.PLATFORM_REPOSITORY,
            "platform",
            tag,
            platform_release=platform_releases.get(tag),
            platform_sha=platform_tags.get(tag),
        )
        if candidate is not None:
            if f"Aggregate {tag}" in platform_active:
                raise GCError(f"aggregate release workflow is active: {tag}")
            candidates.append(candidate)
    metadata = {
        "stable_version": version,
        "stable_sha": stable_sha,
        "published_at": stable["published_at"],
        "canonical_aggregate_count": len(canonical_aggregates),
        "protected_component_count": len(protected),
    }
    return sorted(candidates), metadata


def digest(candidates: Iterable[Candidate]) -> str:
    payload = "\n".join(
        "\t".join(
            (
                item.repository,
                item.unit,
                item.tag,
                item.source_sha or "",
                str(item.release_id or ""),
                str(item.asset_count),
                str(item.asset_bytes),
            )
        )
        for item in candidates
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def grace_elapsed(published_at: str) -> tuple[bool, dt.datetime]:
    published = dt.datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    eligible = published + dt.timedelta(days=GRACE_DAYS)
    return dt.datetime.now(dt.timezone.utc) >= eligible, eligible


def print_plan(candidates: list[Candidate], metadata: dict[str, Any]) -> None:
    print(f"stable_version\t{metadata['stable_version']}")
    print(f"stable_sha\t{metadata['stable_sha']}")
    print(f"plan_digest\t{digest(candidates)}")
    print("repository\tunit\ttag\tsource_sha\trelease_id\tassets\tbytes")
    for item in candidates:
        print(
            f"{item.repository}\t{item.unit}\t{item.tag}\t{item.source_sha}\t"
            f"{item.release_id or ''}\t{item.asset_count}\t{item.asset_bytes}"
        )
    print(
        f"total\t\t\t\t\t{sum(item.asset_count for item in candidates)}\t"
        f"{sum(item.asset_bytes for item in candidates)}"
    )


def dispatch_component(candidate: Candidate) -> None:
    args = [
        "workflow",
        "run",
        "delete-preview.yml",
        "--repo",
        candidate.repository,
        "--ref",
        "main",
        "-f",
        f"version={candidate.tag}",
        "-f",
        f"source_sha={candidate.source_sha}",
        "-f",
        "mode=gc",
    ]
    if candidate.repository.endswith("/guest-runtime"):
        args.extend(("-f", f"unit={candidate.unit}"))
    coordinator.gh(*args)


def apply(candidates: list[Candidate]) -> bool:
    components = [item for item in candidates if item.unit != "platform"]
    aggregates = [item for item in candidates if item.unit == "platform"]
    pending = False
    for item in components:
        title = f"Delete {item.tag} (gc)"
        state = coordinator.latest_run(item.repository, "delete-preview.yml", title)
        if state is not None and coordinator.run_active(state):
            print(f"==> pending {state.get('html_url')}")
            pending = True
            continue
        if state is not None and state.get("conclusion") != "success":
            attempt = int(state.get("run_attempt", 1))
            if attempt >= 3:
                raise GCError(f"component GC failed: {state.get('html_url')}")
            coordinator.gh(
                "run", "rerun", str(state["id"]), "--repo", item.repository, "--failed"
            )
            pending = True
            continue
        if state is None:
            dispatch_component(item)
            print(f"==> dispatched component GC: {item.repository} {item.tag}")
            pending = True
            continue
        # A successful run with a still-visible candidate is treated as eventual consistency.
        pending = True
    if pending or components:
        return False

    for item in aggregates:
        title = f"Delete {item.tag} (gc)"
        state = coordinator.latest_run(
            coordinator.PLATFORM_REPOSITORY, "delete-preview.yml", title
        )
        if state is not None and coordinator.run_active(state):
            return False
        if state is None:
            coordinator.gh(
                "workflow",
                "run",
                "delete-preview.yml",
                "--repo",
                coordinator.PLATFORM_REPOSITORY,
                "--ref",
                "main",
                "-f",
                f"version={item.tag}",
                "-f",
                f"source_sha={item.source_sha}",
                "-f",
                "mode=gc",
            )
            print(f"==> dispatched aggregate GC: {item.tag}")
            return False
        if state.get("conclusion") != "success":
            raise GCError(f"aggregate GC failed: {state.get('html_url')}")
        return False
    return True


def stable_versions(requested: str) -> list[str]:
    if requested:
        return [requested]
    return sorted(
        item["tag_name"]
        for item in coordinator.paginated(
            f"repos/{coordinator.PLATFORM_REPOSITORY}/releases?per_page=100"
        )
        if isinstance(item.get("tag_name"), str)
        and STABLE_RE.fullmatch(item["tag_name"])
        and item.get("draft") is False
        and item.get("prerelease") is False
    )


def stable_published_at(version: str) -> str:
    release = coordinator.api_optional(
        f"repos/{coordinator.PLATFORM_REPOSITORY}/releases/tags/{version}"
    )
    if (
        release is None
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or not isinstance(release.get("published_at"), str)
    ):
        raise GCError(f"Stable aggregate is not published: {version}")
    return str(release["published_at"])


def main() -> None:
    requested = os.environ.get("STABLE_VERSION", "")
    dry_run = os.environ.get("DRY_RUN", "true") == "true"
    if requested and STABLE_RE.fullmatch(requested) is None:
        raise GCError("stable_version must match release-vMAJOR.MINOR.PATCH")
    for version in stable_versions(requested):
        if not dry_run:
            elapsed, eligible = grace_elapsed(stable_published_at(version))
            if not elapsed:
                print(f"stable_version\t{version}")
                print(f"eligible_at\t{eligible.isoformat()}")
                if requested:
                    raise GCError(f"grace period has not elapsed for {version}")
                print(f"==> skip {version}: grace period has not elapsed")
                continue
        candidates, metadata = plan(version)
        print_plan(candidates, metadata)
        elapsed, eligible = grace_elapsed(str(metadata["published_at"]))
        print(f"eligible_at\t{eligible.isoformat()}")
        if dry_run:
            continue
        if not elapsed:
            raise GCError(f"Stable publication timestamp changed for {version}")
        if apply(candidates):
            print(f"==> Preview GC converged for {version}")
        else:
            print(f"==> Preview GC remains pending for {version}")


if __name__ == "__main__":
    try:
        main()
    except (GCError, coordinator.Deferred, RuntimeError) as error:
        fail(str(error))
