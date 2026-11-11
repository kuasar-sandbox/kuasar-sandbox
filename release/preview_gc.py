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
import time
from dataclasses import dataclass
from typing import Any, Iterable, NoReturn


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "release"))
import preview_coordinator as coordinator  # noqa: E402


GRACE_DAYS = 7
WAIT_SECONDS = 60 * 60
POLL_SECONDS = 30
STABLE_RE = re.compile(r"^release-v[0-9]+\.[0-9]+\.[0-9]+$")
AGGREGATE_PREVIEW_RE = re.compile(
    r"^release-v[0-9]+\.[0-9]+\.[0-9]+-preview\.[0-9]{8}(?:\.[1-9][0-9]*)?$"
)


# Immutable orchestrator go.mod versions reference these Sandboxer releases.
# For pseudo-versions Go needs the preceding Preview tag as well as the commit.
PUBLISHED_GO_DEPENDENCY_TAGS = frozenset({
    ("sandboxer", "v0.1.1-preview.20260826"),      # orchestrator v0.1.0–v0.1.2
    ("sandboxer", "v0.1.13-preview.20261104.1"),   # orchestrator v0.1.12
    ("sandboxer", "v0.1.14-preview.20261107.1"),   # orchestrator v0.1.13
})


@dataclass(frozen=True, order=True)
class Candidate:
    repository: str
    unit: str
    tag: str
    source_sha: str | None
    release_id: int | None
    asset_count: int
    asset_bytes: int


@dataclass(frozen=True)
class CanonicalSnapshot:
    commit: str
    components: dict[str, str]
    delivery: str = "historical"


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
        try:
            result[ref.removeprefix('refs/tags/')] = coordinator.resolve_tag_target(repository, target)
        except coordinator.Deferred as error:
            raise GCError(str(error)) from error
    return result


def platform_release_status(
    tag: str,
    release: dict[str, Any] | None,
    sha: str | None,
    canonical: CanonicalSnapshot | None = None,
) -> coordinator.ReleaseStatus:
    if release is None:
        return coordinator.ReleaseStatus(None, sha, False)
    assets = release.get("assets")
    expected: set[str] | None = None
    if sha is not None:
        try:
            expected = coordinator.platform_asset_names(tag, sha)
        except coordinator.Deferred:
            if canonical is not None:
                validate_platform_ownership(tag, release, sha, canonical)
                expected = coordinator.platform_asset_names_for_components(
                    tag, canonical.components, delivery=canonical.delivery
                )
    complete = (
        release.get("draft") is False
        and release.get("prerelease") is ("-preview." in tag)
        and expected is not None
        and sha is not None
        and coordinator.complete_assets(release, [expected, expected | {
            name.replace('x86_64', 'aarch64') for name in expected}])
    )
    return coordinator.ReleaseStatus(release, sha, complete)


def commit_tree(repository: str, sha: str) -> str | None:
    commit = coordinator.api_optional(f"repos/{repository}/git/commits/{sha}")
    if not isinstance(commit, dict):
        return None
    tree = commit.get("tree")
    if not isinstance(tree, dict):
        return None
    value = tree.get("sha")
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value):
        return value
    return None


def stable_release(
    version: str,
    releases: dict[str, dict[str, Any]],
    tags: dict[str, str],
) -> dict[str, Any]:
    release = releases.get(version)
    if release is None:
        raise GCError(f"Stable aggregate does not exist: {version}")
    tag_sha = tags.get(version)
    status = platform_release_status(version, release, tag_sha)
    target = release.get("target_commitish")
    # Older Stable releases were published from a pre-tag commit and later
    # tagged with a metadata-only commit. Treat that immutable, identical tree
    # as equivalent without changing the Stable release or weakening Preview
    # ownership checks.
    if (
        not status.complete
        and release.get("prerelease") is False
        and tag_sha is not None
        and isinstance(target, str)
        and re.fullmatch(r"[0-9a-f]{40}", target)
        and target != tag_sha
    ):
        target_tree = commit_tree(coordinator.PLATFORM_REPOSITORY, target)
        tag_tree = commit_tree(coordinator.PLATFORM_REPOSITORY, tag_sha)
        if target_tree and target_tree == tag_tree:
            normalized = dict(release)
            normalized["target_commitish"] = tag_sha
            status = platform_release_status(version, normalized, tag_sha)
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
        for asset in assets if asset.get("name") != "SHA256SUMS"
    ):
        raise GCError(f"Stable aggregate assets lack verified digests: {version}")
    return release


def parse_snapshot(commit: str) -> tuple[str, dict[str, str], str] | None:
    content = git("show", f"{commit}:releases/daily-preview.yaml", check=False)
    if not content:
        return None
    try:
        raw = coordinator.selection.read_simple_yaml(
            content, f"{commit}:releases/daily-preview.yaml"
        )
        if "preview_version" not in raw or "components" not in raw:
            return None
        aggregate, _, components = coordinator.selection.parse_historical_manifest(
            content, f"{commit}:releases/daily-preview.yaml", True
        )
    except coordinator.selection.ManifestError as error:
        raise GCError(str(error)) from error
    return aggregate, components, coordinator.selection.delivery(raw, commit)


def canonical_snapshots(
    stable_sha: str, version: str
) -> dict[str, list[CanonicalSnapshot]]:
    commits = git(
        "log",
        "--first-parent",
        "--format=%H",
        stable_sha,
        "--",
        "releases/daily-preview.yaml",
    ).splitlines()
    snapshots: dict[str, list[CanonicalSnapshot]] = {}
    for commit in commits:
        snapshot = parse_snapshot(commit)
        if snapshot is None:
            continue
        aggregate, components, delivery = snapshot
        if aggregate.startswith(f"{version}-preview."):
            # Keep newest-first history: an existing tag can predate a later
            # edit of the same version, including historical metadata cleanup.
            snapshots.setdefault(aggregate, []).append(
                CanonicalSnapshot(commit, components, delivery)
            )
    return snapshots


def first_parent_contains(source_sha: str, canonical_sha: str) -> bool:
    return source_sha in set(git("rev-list", "--first-parent", canonical_sha).splitlines())


def validate_platform_ownership(
    tag: str,
    release: dict[str, Any] | None,
    source_sha: str,
    canonical: CanonicalSnapshot,
) -> None:
    if release is not None and release.get("target_commitish") != source_sha:
        raise GCError(f"platform Preview Release and Tag disagree: {tag}")
    source_snapshot = parse_snapshot(source_sha)
    if source_snapshot is not None:
        if source_snapshot[0] != tag:
            raise GCError(f"platform Preview Tag selects another manifest: {tag}")
        if source_snapshot[1] != canonical.components or source_snapshot[2] != canonical.delivery:
            raise GCError(f'platform Preview tag manifest differs from canonical selection; retain: {tag}')
        associated = first_parent_contains(canonical.commit, source_sha)
    else:
        # The oldest aggregate tags precede the commit that introduced their
        # canonical two-file selection. They must still be on that commit's
        # first-parent line; an unrelated or side-branch target is never owned.
        associated = first_parent_contains(source_sha, canonical.commit)
    if not associated:
        raise GCError(f"platform Preview Tag is outside its canonical history: {tag}")


def resolve_canonical_snapshot(
    tag: str,
    release: dict[str, Any] | None,
    source_sha: str | None,
    snapshots: list[CanonicalSnapshot],
    *, stable_sha: str | None = None,
) -> CanonicalSnapshot:
    if not snapshots:
        raise GCError(f"platform Preview has no canonical snapshot: {tag}")
    # A surviving tagless Release still names a concrete source. Validate it
    # against the same canonical history used by the deletion executor.
    if source_sha is None and release is not None:
        source_sha = coordinator.recoverable_source_sha(coordinator.ReleaseStatus(release, None, False))
        if source_sha is None:
            raise GCError(f'tagless Preview source is unknown; retain: {tag}')
    canonical = snapshots[0]
    if source_sha is not None:
        if stable_sha is not None and not first_parent_contains(source_sha, stable_sha):
            raise GCError(f'platform Preview source is outside the Stable canonical history; retain: {tag}')
        if parse_snapshot(source_sha) is not None:
            reachable = next(
                (snapshot for snapshot in snapshots
                 if first_parent_contains(snapshot.commit, source_sha)),
                None,
            )
            if reachable is None:
                raise GCError(f"platform Preview Tag is outside its canonical history: {tag}")
            canonical = reachable
        # Pre-manifest tags retain the narrowly checked reverse-ancestry rule; they
        # must not gain a general fallback through older selections.
        validate_platform_ownership(tag, release, source_sha, canonical)
    return canonical


def retained_snapshot(
    tag: str,
    release: dict[str, Any] | None,
    platform_tags: dict[str, str],
    history_cache: dict[str, dict[str, list[CanonicalSnapshot]]],
) -> CanonicalSnapshot:
    source_sha = platform_tags.get(tag)
    if source_sha is None:
        raise GCError(f"retained aggregate Preview has no tag: {tag}")
    current = parse_snapshot(source_sha)
    if current is not None:
        if current[0] != tag:
            raise GCError(f'retained aggregate tag selects another version: {tag}')
        return CanonicalSnapshot(source_sha, current[1], current[2])
    stable = tag.split("-preview.", 1)[0]
    stable_sha = platform_tags.get(stable)
    if stable_sha is not None:
        snapshots = history_cache.setdefault(
            stable, canonical_snapshots(stable_sha, stable)
        )
        choices = snapshots.get(tag)
        if not choices:
            raise GCError(f"retained aggregate Preview has no canonical manifest: {tag}")
    else:
        snapshot = parse_snapshot(source_sha)
        if snapshot is None or snapshot[0] != tag:
            raise GCError(f"retained pre-contract Preview has no Stable history: {tag}")
        choices = [CanonicalSnapshot(source_sha, snapshot[1], snapshot[2])]
    return resolve_canonical_snapshot(tag, release, source_sha, choices, stable_sha=stable_sha)


def all_platform_previews(
    releases: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        tag: item
        for tag, item in releases.items()
        if isinstance(item.get("tag_name"), str)
        and AGGREGATE_PREVIEW_RE.fullmatch(item["tag_name"])
    }


def protected_component_tags(
    platform_previews: dict[str, dict[str, Any]],
    platform_tags: dict[str, str],
    candidates: set[str],
) -> set[tuple[str, str]]:
    protected = set(PUBLISHED_GO_DEPENDENCY_TAGS)
    history_cache: dict[str, dict[str, list[CanonicalSnapshot]]] = {}
    for tag in set(platform_previews) | {tag for tag in platform_tags if AGGREGATE_PREVIEW_RE.fullmatch(tag)}:
        if tag in candidates:
            continue
        snapshot = retained_snapshot(
            tag, platform_previews.get(tag), platform_tags, history_cache
        )
        for unit, component_tag in snapshot.components.items():
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


def live_manifest_protection(collecting: set[str] = frozenset()) -> set[tuple[str, str]]:
    platform_releases = {
        item["tag_name"]: item
        for item in coordinator.paginated(
            f"repos/{coordinator.PLATFORM_REPOSITORY}/releases?per_page=100"
        )
        if isinstance(item.get("tag_name"), str)
    }
    platform_tags = tag_index(coordinator.PLATFORM_REPOSITORY)
    closed_stables = {
        tag
        for tag, item in platform_releases.items()
        if STABLE_RE.fullmatch(tag)
        and item.get("draft") is False
        and item.get("prerelease") is False
        and platform_release_status(tag, item, platform_tags.get(tag)).complete
    }
    return current_manifest_protection(closed_stables) | protected_component_tags(
        all_platform_previews(platform_releases), platform_tags, collecting)


def release_candidate(
    repository: str,
    unit: str,
    tag: str,
    component_state: coordinator.RepositoryState | None = None,
    platform_release: dict[str, Any] | None = None,
    platform_sha: str | None = None,
    platform_snapshot: CanonicalSnapshot | None = None,
) -> Candidate | None:
    spec = coordinator.UNIT_BY_NAME.get(unit)
    if unit == "platform":
        release = platform_release
        sha = platform_sha
        status = platform_release_status(tag, release, sha, platform_snapshot)
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
        if sha is not None and release.get("target_commitish") != sha:
            raise GCError(f"Preview Release and Tag disagree: {repository} {tag}")
    if unit == "platform" and sha is not None:
        if platform_snapshot is None:
            raise GCError(f"platform Preview has no canonical snapshot: {tag}")
        validate_platform_ownership(tag, release, sha, platform_snapshot)
    source_sha = coordinator.recoverable_source_sha(status)
    if source_sha is None:
        raise GCError(f"Preview candidate has no recoverable source target: {repository} {tag}")
    assets = release.get("assets", []) if release is not None else []
    if not isinstance(assets, list) or any(not isinstance(asset, dict) for asset in assets):
        raise GCError(f"Preview candidate has invalid asset metadata: {repository} {tag}")
    assets = [asset for asset in assets if asset.get('name') != 'SHA256SUMS']
    if any(type(asset.get('size')) is not int or asset['size'] < 0 for asset in assets):
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
    history = canonical_snapshots(stable_sha, version)
    snapshots = {
        tag: resolve_canonical_snapshot(
            tag, platform_releases.get(tag), platform_tags.get(tag), choices, stable_sha=stable_sha
        )
        for tag, choices in history.items()
    }
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
    for snapshot in snapshots.values():
        for unit, tag in snapshot.components.items():
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
            if any(
                coordinator.release_title_matches(title, tag)
                for title in active_titles[(spec.repository, spec.workflow)]
            ):
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
            platform_snapshot=snapshots[tag],
        )
        if candidate is not None:
            if any(
                coordinator.aggregate_title_matches(title, tag)
                for title in platform_active
            ):
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


def apply(candidates: list[Candidate], attempts: dict | None = None) -> bool:
    if attempts is None:
        attempts = {}
    def dispatched(item):
        key = (item.repository, item.unit, item.tag)
        if attempts.get(key, 0) >= 3:
            raise GCError(f'cleanup did not converge after three dispatches in this GC run: {item.tag}')
        attempts[key] = attempts.get(key, 0) + 1
    components = [item for item in candidates if item.unit != "platform"]
    aggregates = [item for item in candidates if item.unit == "platform"]
    protected = live_manifest_protection({item.tag for item in aggregates}) if components else set()
    newly_protected = sorted(
        (item.unit, item.tag)
        for item in components
        if (item.unit, item.tag) in protected
    )
    if newly_protected:
        rendered = ", ".join(f"{unit}={tag}" for unit, tag in newly_protected)
        raise GCError(f"a retained reference now protects GC candidate: {rendered}")

    pending = False
    for item in components:
        active = coordinator.active_delete_run(item.repository, item.tag)
        if active is not None:
            print(f"==> pending {active.get('html_url')}")
            pending = True
            continue
        title = f"Delete {item.tag} (gc)"
        state = coordinator.latest_run(item.repository, "delete-preview.yml", title)
        if state is not None and coordinator.run_active(state):
            print(f"==> pending {state.get('html_url')}")
            pending = True
            continue
        dispatched(item)
        dispatch_component(item)
        print(f"==> dispatched current component cleanup: {item.repository} {item.tag}")
        pending = True
    if pending or components:
        return False

    for item in aggregates:
        active = coordinator.active_delete_run(
            coordinator.PLATFORM_REPOSITORY, item.tag
        )
        if active is not None:
            print(f"==> pending {active.get('html_url')}")
            return False
        title = f"Delete {item.tag} (gc)"
        state = coordinator.latest_run(
            coordinator.PLATFORM_REPOSITORY, "delete-preview.yml", title
        )
        if state is not None and coordinator.run_active(state):
            return False
        dispatched(item)
        coordinator.gh(
            'workflow', 'run', 'delete-preview.yml', '--repo', coordinator.PLATFORM_REPOSITORY,
            '--ref', 'main', '-f', f'version={item.tag}', '-f', f'source_sha={item.source_sha}', '-f', 'mode=gc')
        print(f'==> dispatched current aggregate cleanup: {item.tag}')
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
    deadline = time.monotonic() + WAIT_SECONDS
    attempts = {}
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
        while True:
            # Re-plan from live Releases, tags and protected references after
            # every asynchronous deletion. A successful dispatch is not GC
            # completion, and a stale plan must never authorize the next stage.
            candidates, metadata = plan(version)
            print_plan(candidates, metadata)
            elapsed, eligible = grace_elapsed(str(metadata["published_at"]))
            print(f"eligible_at\t{eligible.isoformat()}")
            if dry_run:
                break
            if not elapsed:
                raise GCError(f"Stable publication timestamp changed for {version}")
            if not candidates:
                print(f"==> Preview GC converged for {version}")
                break
            if time.monotonic() >= deadline:
                raise GCError(f"Preview GC timed out with pending candidates for {version}")
            apply(candidates, attempts)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GCError(f"Preview GC timed out with pending candidates for {version}")
            print(f"==> Preview GC remains pending for {version}; waiting to re-plan")
            time.sleep(min(POLL_SECONDS, remaining))


if __name__ == "__main__":
    try:
        main()
    except (GCError, coordinator.Deferred, RuntimeError) as error:
        fail(str(error))
