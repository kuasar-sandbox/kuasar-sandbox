#!/usr/bin/env python3

"""Converge one committed Stable aggregate selection."""

from __future__ import annotations

import base64
from enum import Enum
import os
import pathlib
import re
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "release"))
import preview_coordinator as coordinator  # noqa: E402


class PublicationState(Enum):
    NEW = 'new'
    COMPLETE = 'complete'
    RESUME = 'resume'


def validated_stage(state: dict, version: str) -> bool:
    """Protect actual retained acceptance, not every failed workflow attempt."""
    endpoint = f"repos/{coordinator.PLATFORM_REPOSITORY}/actions/runs/{state['id']}"
    jobs = coordinator.latest_actual_jobs(coordinator.paginated(endpoint + '/jobs?filter=all&per_page=100', 'jobs'))
    if not all(jobs.get(name, {}).get('status') == 'completed' and
               jobs[name].get('conclusion') == 'success'
               for name in ('prepare', 'stage', 'release-asset-validation / results')):
        return False
    artifacts = coordinator.paginated(endpoint + '/artifacts?per_page=100', 'artifacts')
    stages = [item for item in artifacts if item.get('name') == f"aggregate-stage-{version}-{state['id']}"]
    return len(stages) == 1 and stages[0].get('expired') is False


def existing_publication(version: str, text: str, source_sha: str) -> PublicationState:
    """Do not prepare a replacement source after this version was admitted."""
    status = coordinator.platform_release(version)
    if status.complete:
        if status.tag_sha is None:
            raise coordinator.Deferred('complete Stable release has no immutable source')
        remote = coordinator.api(f'repos/{coordinator.PLATFORM_REPOSITORY}/contents/releases/release.yaml?ref={status.tag_sha}')
        original = base64.b64decode(str(remote['content']).replace('\n', '')).decode()
        selected = coordinator.selection.read_simple_yaml(text, 'current Stable selection')
        admitted = coordinator.selection.read_simple_yaml(original, 'published Stable selection')
        if ({k: v for k, v in selected.items() if k != 'notes'} !=
                {k: v for k, v in admitted.items() if k != 'notes'}):
            raise coordinator.Deferred('Stable version is already published with another selection; select a new version')
        print(f'==> Stable {version} is already complete at {status.tag_sha}; retaining its source and notes')
        return PublicationState.COMPLETE
    active = coordinator.active_aggregate_run(version)
    if active is not None:
        raise coordinator.Deferred(f"Stable publication is active; retain its source and notes: {active.get('html_url')}")
    if status.partial:
        raise coordinator.Deferred('Stable publication is partial; recover its original validated stage before selecting a new source')
    # A failed publisher may not have created a Release yet. The original run's
    # immutable stage remains authoritative; a notes correction is not permission
    # to admit a substitute source for that same attempted publication.
    retained = False
    for state in coordinator.aggregate_runs(version):
        title = state.get('display_title', '')
        match = re.fullmatch(re.escape(f'Aggregate {version} @') + r'([0-9a-f]{40})', title)
        if validated_stage(state, version):
            if match is None or match.group(1) != source_sha:
                raise coordinator.Deferred('Stable version has an earlier validated source; recover its original run or select a new version')
            # Resume the same accepted stage without preparing a new source.
            # The publisher obtains human text independently of acceptance.
            retained = True
    return PublicationState.RESUME if retained else PublicationState.NEW


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
    if coordinator.run(['git', 'show', f'{source_sha}:releases/release.yaml'], cwd=ROOT).stdout != path.read_text(encoding='utf-8'):
        coordinator.fail('release.yaml has uncommitted edits; review and commit before release admission')

    publication = existing_publication(version, path.read_text(encoding='utf-8'), source_sha)
    if publication is PublicationState.COMPLETE:
        return
    plans: dict[str, coordinator.Plan] = {}
    for unit in coordinator.UNITS:
        tag = configured[unit.name]
        state = coordinator.RepositoryState(unit)
        status = state.status(tag)
        if status.complete and status.tag_sha is not None:
            if not coordinator.has_arm_archive(unit.name, tag, status):
                raise coordinator.Deferred(
                    f"{unit.name} {tag} is a valid historical AMD64 release; "
                    "select a new dual-architecture unit version before publishing a new Stable aggregate"
                )
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

    # Stable and Daily share one pre-freeze preparation transaction. It never
    # waits for the future component tags and every dispatch uses this exact
    # persisted source, including the complete editable release notes.
    if publication is PublicationState.NEW:
        source_sha = coordinator.persist_manifest(path.read_text(encoding='utf-8'), version,
            channel='release', root=ROOT, source_ref=source_ref, source_sha=source_sha)
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
