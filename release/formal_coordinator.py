#!/usr/bin/env python3

"""Converge one committed Stable aggregate selection."""

from __future__ import annotations

import os
import pathlib
import re
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "release"))
import preview_coordinator as coordinator  # noqa: E402


def main() -> None:
    version = os.environ.get("RELEASE_VERSION", "")
    source_ref = os.environ.get("PLATFORM_REF", "")
    source_sha = os.environ.get("PLATFORM_SHA", "")
    if re.fullmatch(r"release-v[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        coordinator.fail("RELEASE_VERSION must match release-vMAJOR.MINOR.PATCH")
    if source_ref != "main" and coordinator.preview_selection.PLATFORM_RELEASE_BRANCH_RE.fullmatch(
        source_ref
    ) is None:
        coordinator.fail("PLATFORM_REF must be main or release/vMAJOR.MINOR.x")
    if source_ref != "main":
        branch = re.fullmatch(r"release/v([0-9]+)\.([0-9]+)\.x", source_ref)
        assert branch is not None
        if re.fullmatch(
            rf"release-v{branch.group(1)}\.{branch.group(2)}\.[0-9]+", version
        ) is None:
            coordinator.fail(f"{version} does not belong to {source_ref}")
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        coordinator.fail("PLATFORM_SHA must be a full lowercase SHA")
    local_sha = coordinator.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT
    ).stdout.strip()
    if local_sha != source_sha:
        coordinator.fail("platform checkout does not match PLATFORM_SHA")
    if coordinator.branch_sha(coordinator.PLATFORM_REPOSITORY, source_ref) != source_sha:
        raise coordinator.Deferred(f"platform {source_ref} moved after selection")
    coordinator.selection.validate_current_manifests(ROOT)

    path = ROOT / "releases/release.yaml"
    aggregate, _, configured = coordinator.selection.parse_manifest(
        path.read_text(encoding="utf-8"), str(path), False
    )
    if aggregate != version:
        coordinator.fail(f"release.yaml selects {aggregate}, not {version}")

    plans: dict[str, coordinator.Plan] = {}
    for unit in coordinator.UNITS:
        tag = configured[unit.name]
        state = coordinator.RepositoryState(unit)
        status = state.status(tag)
        if status.complete and status.tag_sha is not None:
            plans[unit.name] = coordinator.Plan(
                unit, tag, None, status.tag_sha, tag, tag, "fixed"
            )
            continue
        candidate_ref = coordinator.preview_selection.component_source_ref(
            source_ref, unit.name, tag
        )
        candidate_sha = coordinator.branch_sha(unit.repository, candidate_ref)
        if candidate_sha is None:
            raise coordinator.Deferred(
                f"cannot publish {unit.name} {tag}: source branch {candidate_ref} is missing"
            )
        plans[unit.name] = coordinator.Plan(
            unit, tag, candidate_ref, candidate_sha, tag, tag, "publish"
        )

    wait_for_convergence(plans, version, source_sha)


def wait_for_convergence(
    plans: dict[str, coordinator.Plan], version: str, source_sha: str
) -> None:
    deadline = time.monotonic() + int(
        os.environ.get("RELEASE_WAIT_SECONDS", "10800")
    )
    poll = int(os.environ.get("RELEASE_POLL_SECONDS", "120"))
    while True:
        try:
            if coordinator.converge(plans, version, source_sha):
                return
        except coordinator.Pending as error:
            print(f"==> pending: {error}")
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Stable convergence remains pending; rerun the same committed selection"
            )
        time.sleep(poll)


if __name__ == "__main__":
    try:
        main()
    except coordinator.Deferred as error:
        coordinator.fail(f"deferred without mutation: {error}")
    except RuntimeError as error:
        coordinator.fail(str(error))
