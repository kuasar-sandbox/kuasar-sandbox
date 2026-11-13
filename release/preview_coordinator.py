#!/usr/bin/env python3

"""Converge one platform branch to its maintained Daily Preview selection."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from typing import Any, NoReturn
from urllib.parse import quote


CONTROL_ROOT = pathlib.Path(__file__).resolve().parent.parent
PLATFORM_ROOT = pathlib.Path(os.environ.get("PLATFORM_ROOT", CONTROL_ROOT)).resolve()
PLATFORM_REPOSITORY = "kuasar-sandbox/kuasar-sandbox"
PLATFORM_REF = os.environ.get("PLATFORM_REF", "main")
PLATFORM_SHA = os.environ.get("PLATFORM_SHA", "")
TODAY = os.environ.get("PREVIEW_DATE", "")
WAIT_SECONDS = int(os.environ.get("PREVIEW_WAIT_SECONDS", "3000"))
INITIALIZE_ARM = os.environ.get("INITIALIZE_ARM", "false") == "true"
POLL_SECONDS = int(os.environ.get("PREVIEW_POLL_SECONDS", "120"))
# Bounded only within this coordinator invocation; old failed workflows never ban retries.
_cleanup_dispatches: dict[tuple[str, str, str, str], int] = {}
_publication_resumes: dict[tuple[str, str, str], int] = {}


def load_module(name: str, path: pathlib.Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


selection = load_module("release_selection", CONTROL_ROOT / "release/selection.py")
sys.path.insert(0, str(CONTROL_ROOT / 'release'))
import prepare_notes  # noqa: E402
import go_dependency_alignment  # noqa: E402
preview_selection = load_module(
    "preview_selection", CONTROL_ROOT / "release/preview-selection.py"
)


@dataclass(frozen=True)
class Unit:
    name: str
    repository: str
    workflow: str
    dependencies: tuple[str, ...] = ()


UNITS = (
    Unit("accelerator", "kuasar-sandbox/accelerator", "release.yml"),
    Unit("connector", "kuasar-sandbox/connector", "release.yml"),
    Unit("vmlinux", "kuasar-sandbox/guest-runtime", "release-vmlinux.yml"),
    Unit(
        "runtime",
        "kuasar-sandbox/guest-runtime",
        "release-runtime.yml",
        ("accelerator",),
    ),
    Unit(
        "sandboxer",
        "kuasar-sandbox/sandboxer",
        "release.yml",
        ("accelerator", "connector", "runtime"),
    ),
    Unit(
        "orchestrator",
        "kuasar-sandbox/orchestrator",
        "component-release.yml",
        ("accelerator", "connector", "sandboxer", "runtime"),
    ),
)
UNIT_BY_NAME = {unit.name: unit for unit in UNITS}


@dataclass(frozen=True)
class ReleaseStatus:
    release: dict[str, Any] | None
    tag_sha: str | None
    complete: bool

    @property
    def partial(self) -> bool:
        return self.release is not None or self.tag_sha is not None


@dataclass(frozen=True)
class Plan:
    unit: Unit
    configured: str
    source_ref: str | None
    source_sha: str
    winner: str
    selected: str
    action: str
    source_head: str | None = None


class Pending(RuntimeError):
    """Convergence is safely deferred until another workflow completes."""


class Deferred(RuntimeError):
    """Convergence cannot mutate ambiguous external state."""


def fail(message: str) -> NoReturn:
    raise SystemExit(f"preview-coordinator: {message}")


def run(
    command: list[str],
    *,
    cwd: pathlib.Path | None = None,
    check: bool = True,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    git_operation = command[0] == 'git' or command[:3] == ['gh', 'repo', 'clone']
    if git_operation:
        environment = dict(os.environ if environment is None else environment,
                           GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
    try:
        result = subprocess.run(
            command, cwd=cwd, check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
            **({'timeout': 120} if git_operation else {}),
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('Git source operation timed out') from error
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(f"{' '.join(command)}: {detail}")
    return result


def git_auth_environment() -> dict[str, str]:
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise RuntimeError("GH_TOKEN is required for private component Git access")
    environment = os.environ.copy()
    try:
        index = int(environment.get("GIT_CONFIG_COUNT", "0"))
    except ValueError as error:
        raise RuntimeError("GIT_CONFIG_COUNT must be numeric") from error
    credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    environment["GIT_CONFIG_COUNT"] = str(index + 1)
    environment[f"GIT_CONFIG_KEY_{index}"] = (
        "http.https://github.com/.extraheader"
    )
    environment[f"GIT_CONFIG_VALUE_{index}"] = (
        f"AUTHORIZATION: basic {credential}"
    )
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["gh", *args], check=check)


def gh_platform(*args: str) -> subprocess.CompletedProcess[str]:
    token = os.environ.get("PLATFORM_TOKEN")
    if not token:
        raise RuntimeError("PLATFORM_TOKEN is required for platform mutations")
    environment = os.environ.copy()
    environment["GH_TOKEN"] = token
    result = subprocess.run(
        ["gh", *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "platform GitHub API mutation failed")
    return result


def api(endpoint: str) -> dict[str, Any]:
    result = gh("api", endpoint)
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError(f"expected an object from {endpoint}")
    return value


def api_optional(endpoint: str) -> dict[str, Any] | None:
    result = gh("api", endpoint, check=False)
    if result.returncode == 0:
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise RuntimeError(f"expected an object from {endpoint}")
        return value
    if "(HTTP 404)" in result.stderr:
        return None
    raise RuntimeError(result.stderr.strip() or f"cannot query {endpoint}")


def paginated(endpoint: str, key: str | None = None) -> list[dict[str, Any]]:
    result = gh("api", "--paginate", "--slurp", endpoint)
    pages = json.loads(result.stdout)
    values: list[dict[str, Any]] = []
    for page in pages:
        entries = page[key] if key is not None else page
        if not isinstance(entries, list):
            raise RuntimeError(f"invalid paginated response from {endpoint}")
        values.extend(entries)
    return values


def archive_name(unit: str, tag: str, arch: str = "x86_64") -> str:
    if arch not in ("x86_64", "aarch64"):
        raise ValueError("unsupported release architecture")
    if unit == "runtime":
        return f"sandbox-runtime-{arch}-{tag.removeprefix('runtime-')}.tar.gz"
    if unit == "vmlinux":
        return f"vmlinux-{arch}-{tag.removeprefix('vmlinux-')}.tar.gz"
    if unit == "platform":
        return f"platform-{tag}.tar.gz"
    return f"{unit}-{tag}-linux-{arch}.tar.gz"


def platform_asset_names_for_components(
    tag: str, components: dict[str, str], architectures: tuple[str, ...] = ("x86_64",),
    delivery: str = "historical",
) -> set[str]:
    if delivery in selection.MODERN_DELIVERIES:
        architectures = ('x86_64', 'aarch64')
    elif delivery != 'historical':
        raise ValueError('unsupported aggregate delivery contract')
    return {
        archive_name("platform", tag),
        *(archive_name(unit, components[unit], arch) for unit in selection.UNITS for arch in architectures),
    } | selection.delivery_assets(tag, delivery, architectures)



def platform_asset_names(tag: str, sha: str) -> set[str]:
    relative = (
        "releases/daily-preview.yaml"
        if "-preview." in tag
        else "releases/release.yaml"
    )
    published = tag_sha(PLATFORM_REPOSITORY, tag)
    source_ref = tag if published is not None else PLATFORM_REF
    observed = published if published is not None else branch_sha(PLATFORM_REPOSITORY, source_ref)
    if observed != sha:
        raise Deferred(f'platform {source_ref} moved before manifest admission')
    state = api_optional(
        f"repos/{PLATFORM_REPOSITORY}/contents/{relative}?ref={quote(source_ref, safe='')}"
    )
    after = tag_sha(PLATFORM_REPOSITORY, tag) if published is not None else branch_sha(PLATFORM_REPOSITORY, source_ref)
    if after != sha:
        raise Deferred(f'platform {source_ref} moved during manifest admission')
    if state is None or not isinstance(state.get("content"), str):
        raise Deferred(f"platform {tag} has no release manifest at {sha}")
    try:
        content = base64.b64decode(state["content"].replace("\n", "")).decode()
        aggregate, _, components = selection.parse_historical_manifest(
            content, f"{sha}:{relative}", "-preview." in tag
        )
    except (ValueError, UnicodeDecodeError, selection.ManifestError) as error:
        raise Deferred(f"platform {tag} has an invalid release manifest at {sha}") from error
    if aggregate != tag:
        raise Deferred(f"platform manifest at {sha} selects {aggregate}, not {tag}")
    contract = selection.delivery(selection.read_simple_yaml(content, relative), relative)
    return platform_asset_names_for_components(tag, components, delivery=contract)


def tag_sha(repository: str, tag: str) -> str | None:
    state = api_optional(f"repos/{repository}/git/ref/tags/{quote(tag, safe='')}")
    if state is None:
        return None
    return resolve_tag_target(repository, state.get('object'))


def resolve_tag_target(repository, target):
    seen = set()
    while isinstance(target, dict):
        sha, kind = target.get('sha'), target.get('type')
        if not isinstance(sha, str) or re.fullmatch(r'[0-9a-f]{40}', sha) is None or sha in seen:
            break
        seen.add(sha)
        if kind == 'commit':
            return sha
        if kind != 'tag' or len(seen) >= 16:
            break
        target = api(f'repos/{repository}/git/tags/{sha}').get('object')
    raise Deferred(f'{repository} has an unresolved tag target; retain for inspection')


def complete_assets(release, expected_sets):
    """Check package availability, never historical qualification records.

    Unknown metadata must not turn a complete public package into an automatic
    incomplete-release deletion candidate. Historical checksum files are ignored.
    """
    rows = release.get('assets')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise Deferred('Release asset inventory is unavailable')
    rows = [row for row in rows if row.get('name') != 'SHA256SUMS']
    names = [row.get('name') for row in rows]
    allowed = set().union(*expected_sets)
    if any(not isinstance(name, str) or name not in allowed for name in names) or len(names) != len(set(names)):
        raise Deferred('Release asset inventory is unexpected or duplicated; retain for inspection')
    ids = []
    for row in rows:
        if (type(row.get('id')) is not int or row['id'] <= 0 or type(row.get('size')) is not int
                or row['size'] <= 0 or not isinstance(row.get('digest'), str)
                or re.fullmatch(r'sha256:[0-9a-f]{64}', row['digest']) is None
                or row.get('state') not in ('uploaded', 'starter')):
            raise Deferred('Release asset size/digest/identity/state is unavailable; retain for inspection')
        ids.append(row['id'])
    if len(set(ids)) != len(ids):
        raise Deferred('Release asset IDs are duplicated; retain for inspection')
    return set(names) in expected_sets and all(row.get('state') == 'uploaded' for row in rows)


class RepositoryState:
    def __init__(self, unit: Unit) -> None:
        self.unit = unit
        self.releases = {
            item["tag_name"]: item
            for item in paginated(f"repos/{unit.repository}/releases?per_page=100")
            if isinstance(item.get("tag_name"), str)
        }
        refs = paginated(
            f"repos/{unit.repository}/git/matching-refs/tags/"
        )
        self.tag_shas: dict[str, str] = {}
        self.invalid_tags: set[str] = set()
        for item in refs:
            ref = item.get("ref")
            target = item.get("object")
            if not isinstance(ref, str) or not ref.startswith("refs/tags/"):
                continue
            tag = ref.removeprefix("refs/tags/")
            try:
                self.tag_shas[tag] = resolve_tag_target(unit.repository, target)
            except Deferred:
                self.invalid_tags.add(tag)
        self._statuses: dict[str, ReleaseStatus] = {}

    def status(self, tag: str) -> ReleaseStatus:
        if tag in self._statuses:
            return self._statuses[tag]
        release = self.releases.get(tag)
        if tag in self.invalid_tags:
            raise Deferred(
                f"{self.unit.repository} {tag} has an unresolved tag target"
            )
        sha = self.tag_shas.get(tag)
        prerelease = "-preview." in tag
        archive = archive_name(self.unit.name, tag)
        complete = False
        if release is not None:
            complete = (
                release.get("draft") is False
                and release.get("prerelease") is prerelease
                and complete_assets(release, [{archive}, {archive, archive_name(self.unit.name, tag, "aarch64")}])
                and sha is not None
            )
        status = ReleaseStatus(release, sha, complete)
        self._statuses[tag] = status
        return status

    def usable_tags(self) -> list[str]:
        # Discovery is not admission of a configured or selected publication.
        # Shared repositories can contain other units and obsolete contracts;
        # retain those releases without letting them block valid candidates.
        usable = []
        for tag in self.releases:
            try:
                preview_selection.parse_tag(self.unit.name, tag)
            except preview_selection.SelectionError:
                continue
            try:
                status = self.status(tag)
            except Deferred as error:
                print(f"==> skip unusable candidate {self.unit.repository} {tag}: {error}")
                continue
            if status.complete:
                usable.append(tag)
        return usable


def recoverable_source_sha(status: ReleaseStatus) -> str | None:
    if status.tag_sha is not None:
        return status.tag_sha
    if status.release is None:
        return None
    target = status.release.get("target_commitish")
    if isinstance(target, str) and re.fullmatch(r"[0-9a-f]{40}", target):
        return target
    return None


def dependency_binding(plan: Plan, plans: dict[str, Plan]) -> str:
    return ",".join(
        f"{name}={plans[name].selected}" for name in plan.unit.dependencies
    )


def validate_release_reuse(plan: Plan, status: ReleaseStatus) -> None:
    """A published tag is a finished product, independent of old build records."""
    if not status.complete or status.tag_sha != plan.source_sha:
        raise Deferred(f"{plan.unit.name} {plan.selected} is incomplete or its tag changed")


def release_run_title(plan: Plan, plans: dict[str, Plan]) -> str:
    title = f"Release {plan.selected} @{plan.source_sha}"
    dependencies = dependency_binding(plan, plans)
    return f"{title} [{dependencies}]" if dependencies else title


def branch_sha(repository: str, source_ref: str) -> str | None:
    state = api_optional(f"repos/{repository}/git/ref/heads/{source_ref}")
    if state is None:
        return None
    target = state.get("object")
    if not isinstance(target, dict) or target.get("type") != "commit":
        raise RuntimeError(f"{repository}:{source_ref} is not a commit branch ref")
    sha = target.get("sha")
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise RuntimeError(f"invalid source SHA for {repository}:{source_ref}")
    return sha


def clone_repository(unit: Unit, source_ref: str, destination: pathlib.Path) -> None:
    environment = git_auth_environment()
    gh(
        "repo",
        "clone",
        unit.repository,
        str(destination),
        "--",
        "--filter=blob:none",
        "--no-checkout",
    )
    run(
        [
            "git",
            "fetch",
            "--update-head-ok",
            "--force",
            "origin",
            f"refs/heads/{source_ref}:refs/heads/{source_ref}",
            "+refs/tags/*:refs/tags/*",
        ],
        cwd=destination,
        environment=environment,
    )


def latest_run(repository: str, workflow: str, title: str) -> dict[str, Any] | None:
    runs = paginated(
        f"repos/{repository}/actions/workflows/{workflow}/runs?event=workflow_dispatch&per_page=100",
        "workflow_runs",
    )
    matching = [item for item in runs if item.get("display_title") == title]
    return max(matching, key=lambda item: str(item.get("created_at", "")), default=None)


def release_title_matches(title: object, version: str) -> bool:
    legacy = f"Release {version}"
    return title == legacy or str(title).startswith(f"{legacy} @")


def release_runs(
    repository: str, workflow: str, version: str
) -> list[dict[str, Any]]:
    runs = paginated(
        f"repos/{repository}/actions/workflows/{workflow}/runs?event=workflow_dispatch&per_page=100",
        "workflow_runs",
    )
    return [
        item
        for item in runs
        if release_title_matches(item.get("display_title"), version)
    ]


def active_release_run(
    repository: str, workflow: str, version: str
) -> dict[str, Any] | None:
    active = [
        item
        for item in release_runs(repository, workflow, version)
        if run_active(item)
    ]
    return max(active, key=lambda item: str(item.get("created_at", "")), default=None)


def aggregate_run_title(version: str, source_sha: str) -> str:
    return f"Aggregate {version} @{source_sha}"


def aggregate_title_matches(title: object, version: str) -> bool:
    # Validation runs do not own publication, retries, recovery or GC locks.
    # Exclude the earlier suffix identity as well as the distinct new prefix.
    if not isinstance(title, str) or title.endswith(" [validation only]"):
        return False
    legacy = f"Aggregate {version}"
    return title == legacy or re.fullmatch(re.escape(legacy) + r" @[0-9a-f]{40}", title) is not None


def aggregate_runs(version: str) -> list[dict[str, Any]]:
    runs = paginated(
        f"repos/{PLATFORM_REPOSITORY}/actions/workflows/aggregate-release.yml/runs?"
        "event=workflow_dispatch&per_page=100",
        "workflow_runs",
    )
    return [
        item
        for item in runs
        if aggregate_title_matches(item.get("display_title"), version)
    ]


def active_aggregate_run(version: str) -> dict[str, Any] | None:
    active = [item for item in aggregate_runs(version) if run_active(item)]
    return max(active, key=lambda item: str(item.get("created_at", "")), default=None)


def dispatch_aggregate_run(version: str, source_sha: str) -> None:
    gh(
        "workflow",
        "run",
        "aggregate-release.yml",
        "--repo",
        PLATFORM_REPOSITORY,
        "--ref",
        "main",
        "-f",
        f"version={version}",
        "-f",
        f"source_ref={PLATFORM_REF}",
        "-f",
        f"source_sha={source_sha}",
    )


def run_active(state: dict[str, Any]) -> bool:
    return state.get("status") in {
        "queued",
        "in_progress",
        "pending",
        "requested",
        "waiting",
    }


def active_delete_run(repository: str, version: str) -> dict[str, Any] | None:
    titles = {f"Delete {version} (gc)", f"Delete {version} (incomplete)"}
    runs = paginated(
        f"repos/{repository}/actions/workflows/delete-preview.yml/runs?"
        "event=workflow_dispatch&per_page=100",
        "workflow_runs",
    )
    active = [
        item
        for item in runs
        if item.get("display_title") in titles and run_active(item)
    ]
    return max(active, key=lambda item: str(item.get("created_at", "")), default=None)


def count_cleanup_dispatch(repository, version, source_sha, mode):
    key = (repository, version, source_sha, mode)
    count = _cleanup_dispatches.get(key, 0)
    if count >= 3:
        raise RuntimeError(f'cleanup did not converge after three attempts in this invocation: {version}')
    _cleanup_dispatches[key] = count + 1


def dispatch_cleanup(plan: Plan, mode: str) -> None:
    active = active_delete_run(plan.unit.repository, plan.selected)
    if active is not None:
        raise Pending(f"cleanup is active: {active.get('html_url')}")
    title = f"Delete {plan.selected} ({mode})"
    state = latest_run(plan.unit.repository, "delete-preview.yml", title)
    if state is not None and run_active(state):
        raise Pending(f"cleanup is active: {state.get('html_url')}")
    count_cleanup_dispatch(plan.unit.repository, plan.selected, plan.source_sha, mode)
    args = [
        "workflow",
        "run",
        "delete-preview.yml",
        "--repo",
        plan.unit.repository,
        "--ref",
        "main",
        "-f",
        f"version={plan.selected}",
        "-f",
        f"source_sha={plan.source_sha}",
        "-f",
        f"mode={mode}",
    ]
    if plan.unit.repository.endswith("/guest-runtime"):
        args.extend(("-f", f"unit={plan.unit.name}"))
    gh(*args)
    action = "redispatched" if state is not None else "dispatched"
    raise Pending(f"{action} {mode} cleanup for {plan.unit.name} {plan.selected}")


def ensure_configured_state(unit: Unit, configured: str, state: RepositoryState) -> str | None:
    """Keep an incomplete configured publication available for stage recovery."""
    status = state.status(configured)
    if not status.partial or status.complete:
        return None
    if "-preview." not in configured:
        raise Deferred(f"fixed Stable selection is incomplete: {unit.name} {configured}")
    for active in (active_release_run(unit.repository, unit.workflow, configured),
                   active_delete_run(unit.repository, configured)):
        if active is not None:
            raise Pending(f"publication or cleanup is active: {active.get('html_url')}")
    source_sha = recoverable_source_sha(status)
    if source_sha is None:
        raise Deferred(f"{unit.name} {configured} has an ambiguous source; retain it and select a fresh Preview")
    return source_sha


def recover_configured_components(configured: dict[str, str], aggregate: str, aggregate_sha: str) -> None:
    """Finish partial components against the unchanged channel dependency tuple."""
    states = {name: RepositoryState(UNIT_BY_NAME[name]).status(tag) for name, tag in configured.items()}
    if not any(status.partial and not status.complete for status in states.values()):
        return
    plans = {}
    for name, tag in configured.items():
        unit, status = UNIT_BY_NAME[name], states[name]
        source = status.tag_sha if status.complete else recoverable_source_sha(status)
        if source is None or (not status.complete and '-preview.' not in tag):
            raise Deferred(f"configured {name} {tag} is unavailable; retain the channel and recover its original publication")
        source_ref = None if status.complete else preview_selection.component_source_ref(PLATFORM_REF, name, tag)
        plans[name] = Plan(unit, tag, source_ref, source, tag, tag, 'reuse' if status.complete else 'publish')
    for unit in UNITS:
        if unit.name in plans and not ensure_unit(plans[unit.name], plans, aggregate, aggregate_sha):
            raise Pending(f"recover configured {unit.name} before selecting new component versions")


def make_plan(
    unit: Unit,
    configured: str,
    date: str,
    temporary: pathlib.Path,
    next_version: str | None = None,
) -> Plan:
    state = RepositoryState(unit)
    recovery_sha = ensure_configured_state(unit, configured, state)
    source_ref = preview_selection.component_source_ref(
        PLATFORM_REF, unit.name, configured
    )
    preview_selection.validate_next_version(unit.name, next_version, source_ref)
    source_sha = branch_sha(unit.repository, source_ref)
    if recovery_sha is not None:
        return Plan(unit, configured, source_ref, recovery_sha, configured, configured,
                    "publish", source_sha)
    if source_sha is None:
        if PLATFORM_REF == "main":
            raise Deferred(f"required component main branch is missing: {unit.repository}")
        fixed = state.status(configured)
        if not fixed.complete or fixed.tag_sha is None:
            raise Deferred(
                f"{unit.name} has no {source_ref}; fixed selection {configured} is unavailable"
            )
        return Plan(
            unit, configured, None, fixed.tag_sha, configured, configured, "fixed"
        )

    repository = temporary / unit.name
    clone_repository(unit, source_ref, repository)
    usable = state.usable_tags()
    resolution = preview_selection.resolve(
        repository, source_ref, unit.name, configured, usable, date, next_version
    )
    if resolution.head != source_sha:
        raise Deferred(f"{unit.repository}:{source_ref} moved while selecting")
    selected_state = state.status(resolution.selected)
    selected_source_sha = (
        resolution.winner_commit if resolution.action == "reuse" else source_sha
    )
    if selected_state.partial and not selected_state.complete:
        raise Deferred(f"incomplete publication occupies {unit.name} {resolution.selected}; "
                       "retain it and select a fresh Preview or recover its original stage")
    if selected_state.complete and selected_state.tag_sha != selected_source_sha:
        raise Deferred(
            f"{unit.name} {resolution.selected} exists on another source commit"
        )
    return Plan(
        unit,
        configured,
        source_ref,
        selected_source_sha,
        resolution.winner,
        resolution.selected,
        resolution.action,
        source_sha,
    )


def force_dependency_preview(
    plan: Plan, date: str, state: RepositoryState, next_version: str | None = None,
) -> Plan:
    if plan.source_ref is None:
        raise Deferred(
            f"{plan.unit.name} has no derived source branch for a required "
            "dependency rebuild"
        )
    # A reused unit can predate documentation-only HEAD commits. A new version
    # forced by dependencies/architecture uses the already resolved branch HEAD.
    plan = replace(plan, source_sha=plan.source_head or plan.source_sha)
    selected = preview_selection.preview_candidate(
        plan.unit.name, plan.winner, date, next_version, plan.source_ref,
    )
    status = state.status(selected)
    if status.partial and not status.complete:
        if recoverable_source_sha(status) != plan.source_sha:
            raise Deferred(f"dependency rebuild conflicts with incomplete {plan.unit.name} {selected}; retain it")
        return replace(plan, selected=selected, action="publish")
    if status.complete and status.tag_sha != plan.source_sha:
        raise Deferred(f"dependency rebuild tag collision: {plan.unit.name} {selected}")
    if status.complete and selected == plan.winner:
        raise Deferred(
            f"{plan.unit.name} needs another dependency rebuild on the same Preview date and revision"
        )
    action = "reuse" if status.complete else "publish"
    return replace(plan, selected=selected, action=action)


def plan_units(
    configured: dict[str, str], date: str, next_versions: dict[str, str] | None = None,
) -> dict[str, Plan]:
    next_versions = next_versions or {}
    plans: dict[str, Plan] = {}
    with tempfile.TemporaryDirectory(prefix="preview-source-") as directory:
        temporary = pathlib.Path(directory)
        for unit in UNITS:
            next_version = next_versions.get(unit.name)
            plan = make_plan(unit, configured[unit.name], date, temporary, next_version)
            state = RepositoryState(unit)
            status = state.status(plan.selected)
            dependency_changed = any(dependency_inputs_changed(name, unit.name, plans, configured, temporary)
                                     for name in unit.dependencies)
            # Persisting the manifest records a dependency rebuild before its
            # Release exists. On restart, the dependencies can already match
            # that manifest while source selection still finds the old winner.
            configured_tag = preview_selection.parse_tag(unit.name, plan.configured)
            winner_tag = preview_selection.parse_tag(unit.name, plan.winner)
            pending_dependency_preview = (
                bool(unit.dependencies)
                and "-preview." in plan.configured
                and configured_tag.same_commit_order > winner_tag.same_commit_order
                and plan.configured in {
                    preview_selection.preview_candidate(
                        unit.name, plan.winner, plan.configured.rsplit("-preview.", 1)[1],
                        target, plan.source_ref or "main",
                    ) for target in (None, next_version)
                }
                and not state.status(plan.configured).complete
            )
            if (
                dependency_changed or pending_dependency_preview
            ) and plan.action != "publish":
                plan = force_dependency_preview(plan, date, state, next_version)
                status = state.status(plan.selected)
            if status.complete and not has_arm_archive(unit.name, plan.selected, status):
                if not INITIALIZE_ARM:
                    raise Deferred(f"{unit.name} {plan.selected} is a valid historical AMD64 release; "
                                   "explicit INITIALIZE_ARM with a new Preview date/version is required")
                plan = force_dependency_preview(plan, date, state, next_version)
                status = state.status(plan.selected)
                if status.complete and not has_arm_archive(unit.name, plan.selected, status):
                    raise Deferred("ARM initialization must choose a new unit version; historical assets cannot be amended")
            plans[unit.name] = plan
            if status.complete:
                validate_release_reuse(plan, status)
    return plans


def has_arm_archive(unit: str, tag: str, status: ReleaseStatus) -> bool:
    return bool(status.release and archive_name(unit, tag, "aarch64") in
                {asset["name"] for asset in status.release.get("assets", [])})


def dependency_paths(dependency, consumer):
    paths = ["go.mod", "go.sum", "pkg", "internal"]
    if dependency == "runtime" and consumer in ("sandboxer", "orchestrator"):
        paths = ["go.mod", "go.sum", "pkg/guestabi"]
    elif consumer == "runtime" and dependency == "accelerator":
        paths = ["go.mod", "go.sum", "pkg/flatten", "pkg/image", "pkg/manifest", "pkg/remote", "pkg/sparse", "pkg/tailzip", "pkg/tarstream",
                 "pkg/tar", "pkg/cache", "pkg/store", "pkg/readerr", "internal/util"]
    return paths


def dependency_inputs_changed(dependency, consumer, plans, configured, temporary):
    plan = plans[dependency]
    if plan.selected == configured[dependency]:
        return False
    repository = temporary / dependency
    previous = configured[dependency]
    # A persisted Preview selection can precede publication of its tag.
    # The source resolver has already validated the published winner.
    if "-preview." in previous and not preview_selection.run_git(repository, "tag", "--list", previous):
        previous = plan.winner
    changed = preview_selection.run_git(repository, "diff", "--name-only",
                                        f"{previous}..{plan.source_sha}", "--", *dependency_paths(dependency, consumer))
    return any(not path.endswith("_test.go") for path in changed.splitlines())



def manifest_values() -> tuple[str, str | None, str, str | None, dict[str, str]]:
    path = PLATFORM_ROOT / "releases/daily-preview.yaml"
    config = selection.read_simple_yaml(path.read_text(encoding="utf-8"), str(path))
    aggregate, _, components = selection.parse_manifest(
        path.read_text(encoding="utf-8"), str(path), True
    )
    version = str(config["version"])
    preview = str(config["preview_version"])
    previous = str(config["previous_version"]) if "previous_version" in config else None
    previous_preview = (
        str(config["previous_preview_version"])
        if "previous_preview_version" in config
        else None
    )
    if aggregate != f"{version}-{preview}":
        raise RuntimeError("daily manifest aggregate mismatch")
    return version, previous, preview, previous_preview, components


def release_version() -> str:
    path = PLATFORM_ROOT / "releases/release.yaml"
    config = selection.read_simple_yaml(path.read_text(encoding="utf-8"), str(path))
    return str(config["version"])


def manifest_next_versions() -> dict[str, str]:
    path = PLATFORM_ROOT / "releases/daily-preview.yaml"
    config = selection.read_simple_yaml(path.read_text(encoding="utf-8"), str(path))
    return selection.component_next_versions(config, str(path))


def platform_release(tag: str) -> ReleaseStatus:
    release = api_optional(f"repos/{PLATFORM_REPOSITORY}/releases/tags/{quote(tag, safe='')}")
    sha = tag_sha(PLATFORM_REPOSITORY, tag)
    if release is None:
        return ReleaseStatus(None, sha, False)
    expected = platform_asset_names(tag, sha) if sha is not None else set()
    dual = expected | {name.replace("x86_64", "aarch64") for name in expected if "x86_64" in name}
    complete = (
        release.get("draft") is False
        and release.get("prerelease") is ("-preview." in tag)
        and sha is not None
        and complete_assets(release, [expected, dual])
    )
    return ReleaseStatus(release, sha, complete)


def platform_changed_since(tag: str) -> bool:
    status = platform_release(tag)
    if not status.complete or status.tag_sha is None:
        return True
    result = run(
        ["git", "merge-base", "--is-ancestor", status.tag_sha, PLATFORM_SHA],
        cwd=PLATFORM_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise Deferred(f"{tag} is not on the first-parent platform source line")
    return status.tag_sha != PLATFORM_SHA


def render_manifest(
    base: str,
    previous: str | None,
    date: str,
    previous_preview: str | None,
    plans: dict[str, Plan],
    next_versions: dict[str, str] | None = None,
) -> str:
    contract = selection.NODE_INFRA_DELIVERY if (PLATFORM_ROOT / 'node-infra/image/build.py').is_file() else selection.DELIVERY
    lines = [f"version: {base}", f"delivery: {contract}"]
    if previous is not None:
        lines.append(f"previous_version: {previous}")
    lines.append(f"preview_version: preview.{date}")
    if previous_preview is not None:
        lines.append(f"previous_preview_version: {previous_preview}")
    lines.append("components:")
    for name in selection.UNITS:
        lines.append(f"  {name}: {plans[name].selected}")
    if next_versions:
        selection.component_next_versions(
            {"component_next_versions": next_versions}, "generated Preview selection",
        )
        lines.append("component_next_versions:")
        for name in selection.UNITS:
            if name in next_versions:
                lines.append(f"  {name}: {next_versions[name]}")
    content = "\n".join(lines) + "\n"
    selection.parse_manifest(content, "generated Preview selection", True)
    return content


def retain_preview_notes(current: str, generated: str, relative: str) -> str:
    """Generate or retain the channel's editable prose."""
    content = prepare_notes.prepare(current, generated, relative, PLATFORM_ROOT)
    return current if selection.read_simple_yaml(current, relative) == selection.read_simple_yaml(content, relative) else content


def commit_manifest(relative: str, content: str, aggregate: str, source_ref: str, source_sha: str) -> str:
    """Atomically create a single-file commit only on the exact selected head.

    GitHub's ordinary force:false ref update is not an expected-parent CAS: a
    concurrent branch rewind could still be fast-forwarded. GraphQL checks the
    expectedHeadOid inside the commit transaction, including unrelated edits.
    A lost response must stop dispatch. A fresh checkout/rescan sees an applied
    commit's prepared body and does not create another one.
    """
    query = """mutation($input: CreateCommitOnBranchInput!) {
      createCommitOnBranch(input: $input) { commit { oid } }
    }"""
    payload = {'query': query, 'variables': {'input': {
        'branch': {'repositoryNameWithOwner': PLATFORM_REPOSITORY, 'branchName': source_ref},
        'expectedHeadOid': source_sha,
        'message': {'headline': f"release: select {'daily preview' if 'preview.' in aggregate else 'stable'} {aggregate.removeprefix('release-')}"},
        'fileChanges': {'additions': [{'path': relative,
            'contents': base64.b64encode(content.encode()).decode()}]},
    }}}
    with tempfile.NamedTemporaryFile('w', encoding='utf-8') as handle:
        json.dump(payload, handle)
        handle.flush()
        result = gh_platform('api', 'graphql', '--input', handle.name)
    response = json.loads(result.stdout)
    if response.get('errors'):
        raise Pending('manifest commit was not confirmed; rescan the branch before publication')
    sha = response.get('data', {}).get('createCommitOnBranch', {}).get('commit', {}).get('oid')
    if not isinstance(sha, str) or re.fullmatch(r'[0-9a-f]{40}', sha) is None:
        raise Pending('manifest commit returned no valid source identity; rescan before publication')
    return sha


def persist_manifest(content: str, aggregate: str, *, channel: str = 'daily-preview',
                     root: pathlib.Path | None = None, source_ref: str | None = None,
                     source_sha: str | None = None, source_shas: dict[str, str] | None = None) -> str:
    if channel not in ('release', 'daily-preview'):
        raise ValueError('unknown release manifest channel')
    root = PLATFORM_ROOT if root is None else root
    source_ref = PLATFORM_REF if source_ref is None else source_ref
    source_sha = PLATFORM_SHA if source_sha is None else source_sha
    relative = f'releases/{channel}.yaml'
    local_path = root / relative
    current = local_path.read_text(encoding="utf-8")
    if branch_sha(PLATFORM_REPOSITORY, source_ref) != source_sha:
        raise Pending(f"platform {source_ref} moved; rescan the new head")
    remote = api(f"repos/{PLATFORM_REPOSITORY}/contents/{relative}?ref={source_sha}")
    decoded = base64.b64decode(str(remote["content"]).replace("\n", "")).decode()
    if decoded != current:
        raise Pending(f"{channel}.yaml differs from the selected committed source; review and commit local edits")
    prepared = prepare_notes.prepare(current, content, relative, root, source_shas=source_shas)
    # Preserve comments and layout on exact semantic retries as well as prose.
    if selection.read_simple_yaml(current, relative) == selection.read_simple_yaml(prepared, relative):
        if branch_sha(PLATFORM_REPOSITORY, source_ref) != source_sha:
            raise Pending('platform branch moved during manifest check; rescan before publication')
        return source_sha
    sha = commit_manifest(relative, prepared, aggregate, source_ref, source_sha)
    if branch_sha(PLATFORM_REPOSITORY, source_ref) != sha:
        raise Pending('platform branch moved after manifest commit; rescan before publication')
    local_path.write_text(prepared, encoding="utf-8")
    print(f"==> committed {source_ref}:{relative} at {sha}")
    return sha


def release_inputs(
    plan: Plan, plans: dict[str, Plan], aggregate: str, aggregate_sha: str
) -> list[str]:
    if plan.source_ref is None:
        return []
    values = [
        "-f",
        f"version={plan.selected}",
        "-f",
        f"source_ref={plan.source_ref}",
        "-f",
        f"source_sha={plan.source_sha}",
        "-f",
        f"aggregate_version={aggregate}",
        "-f",
        f"aggregate_sha={aggregate_sha}",
    ]
    for dependency in plan.unit.dependencies:
        values.extend(("-f", f"{dependency}_version={plans[dependency].selected}"))
    return values


def ensure_unit(
    plan: Plan, plans: dict[str, Plan], aggregate: str, aggregate_sha: str
) -> bool:
    state = RepositoryState(plan.unit)
    status = state.status(plan.selected)
    if status.complete:
        if status.tag_sha != plan.source_sha:
            raise Deferred(f"{plan.unit.name} {plan.selected} moved to another commit")
        validate_release_reuse(plan, status)
        print(f"==> reuse {plan.unit.repository} {plan.selected}")
        return True
    active_release = active_release_run(
        plan.unit.repository, plan.unit.workflow, plan.selected
    )
    if active_release is not None:
        print(f"==> pending {active_release.get('html_url')}")
        return False
    if plan.source_ref is None:
        raise Deferred(f"fixed selection is unavailable: {plan.unit.name} {plan.selected}")
    if active_delete_run(plan.unit.repository, plan.selected) is not None:
        raise Pending(f"cleanup is active for {plan.unit.name} {plan.selected}")
    if status.partial and recoverable_source_sha(status) != plan.source_sha:
        raise Deferred(f"incomplete {plan.unit.name} {plan.selected} has another or ambiguous source; retain it")
    title = release_run_title(plan, plans)
    runs = release_runs(plan.unit.repository, plan.unit.workflow, plan.selected)
    exact = [item for item in runs if item.get("display_title") == title]
    if resume_publish(plan.unit.repository, plan.unit.name, plan.selected, exact):
        return False
    if status.partial:
        raise Deferred(f"incomplete {plan.unit.name} {plan.selected} has no recoverable stage; "
                       "retain it and select a fresh Preview")
    if any(item.get("conclusion") == "success" for item in exact):
        raise Pending(f"successful run is waiting for Release API convergence: {title}")
    count_publication(plan.unit.repository, plan.unit.name, plan.selected + ":build")
    # No accepted stage or published bytes exist. A new run avoids replacing
    # successful architecture artifacts from an earlier failed build attempt.
    gh("workflow", "run", plan.unit.workflow, "--repo", plan.unit.repository,
       "--ref", "main", *release_inputs(plan, plans, aggregate, aggregate_sha))
    print(f"==> dispatched {plan.unit.name} {plan.selected} from {plan.source_ref}")
    return False


def dispatch_platform_cleanup(tag: str, source_sha: str) -> None:
    release_state = active_aggregate_run(tag)
    if release_state is not None:
        raise Pending(f"aggregate release is active: {release_state.get('html_url')}")
    active_cleanup = active_delete_run(PLATFORM_REPOSITORY, tag)
    if active_cleanup is not None:
        raise Pending(f"platform cleanup is active: {active_cleanup.get('html_url')}")
    title = f"Delete {tag} (incomplete)"
    state = latest_run(PLATFORM_REPOSITORY, "delete-preview.yml", title)
    if state is not None and run_active(state):
        raise Pending(f"platform cleanup is active: {state.get('html_url')}")
    count_cleanup_dispatch(PLATFORM_REPOSITORY, tag, source_sha, 'incomplete')
    gh(
        "workflow",
        "run",
        "delete-preview.yml",
        "--repo",
        PLATFORM_REPOSITORY,
        "--ref",
        "main",
        "-f",
        f"version={tag}",
        "-f",
        f"source_sha={source_sha}",
        "-f",
        "mode=incomplete",
    )
    action = "redispatched" if state is not None else "dispatched"
    raise Pending(f"{action} incomplete aggregate cleanup for {tag}")


def latest_actual_jobs(attempts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """A publish-only rerun retains earlier successful prerequisite jobs."""
    latest = {}
    for job in attempts:
        previous = latest.get(job['name'])
        if previous is None or job['id'] > previous['id']:
            latest[job['name']] = job
    return latest


def count_publication(repository: str, unit: str, identity: str) -> None:
    key = (repository, unit, identity)
    count = _publication_resumes.get(key, 0)
    if count >= 3:
        raise RuntimeError(f"publication failed after three attempts in this invocation: {repository} {identity}")
    _publication_resumes[key] = count + 1


def resume_publish(repository: str, unit: str, version: str, exact: list[dict[str, Any]],
                   *, expected_title: str | None = None) -> bool:
    """Resume an unfinished publication, never qualify an already released asset."""
    if len(exact) > 20:
        raise Deferred('too many exact publication runs to resolve safely; retain their stages and select a fresh Preview')
    candidates = []
    for state in exact:
        if run_active(state):
            raise Pending(f"publication is active: {state.get('html_url')}")
        if state.get('conclusion') == 'success':
            raise Pending('successful run is waiting for Release API convergence')
        attempts = paginated(f"repos/{repository}/actions/runs/{state['id']}/jobs?filter=all&per_page=100", 'jobs')
        latest = latest_actual_jobs(attempts)
        publisher = latest.get('publish', {})
        if publisher.get('status') == 'completed' and publisher.get('conclusion') == 'success':
            raise Pending('successful publisher is waiting for Release API convergence')
        if publisher.get('status') != 'completed' or publisher.get('conclusion') not in {'failure', 'cancelled', 'timed_out'}:
            continue  # A newer run cancelled before publishing cannot hide an older accepted stage.
        if unit == 'platform':
            required = ['prepare', 'stage', 'release-asset-validation / results']
            import sys
            sys.path.insert(0, str(CONTROL_ROOT / 'ci/integration'))
            import workbench_groups
            workbench_groups.recovery_order(attempts)
            grouped = any(re.search(r'workbench-release \(x86_64, [AB]\)$', name) for name in latest)
            for arch in ('x86_64', 'aarch64'):
                groups = ('A', 'B') if grouped and arch == 'x86_64' else ('all',)
                for group in groups:
                    name = f'release-asset-validation / workbench-release ({arch}, {group})'
                    required.append(name)
            expected = [f'aggregate-stage-{version}-{state["id"]}',
                        f'integration-validation-{state["id"]}']
            # Grouped results are collected inside integration-validation. The
            # ordinary publisher downloads the two original architecture results.
            if not grouped:
                expected += [f'integration-workbench-result-{arch}-{state["id"]}'
                             for arch in ('x86_64', 'aarch64')]
        else:
            required = ['preflight']
            for arch in ('x86_64', 'aarch64'):
                names = [name for name in latest if re.fullmatch(r'build \(' + arch + r'(?:, [^)]+)?\)', name)]
                if len(names) != 1:
                    raise Deferred(f"original {unit} stage lacks an unambiguous {arch} build; retain it")
                required.extend(names)
            prefix = unit + '-release' if unit in {'runtime', 'vmlinux'} else 'component-release'
            expected = [f'workbench-selection-{state["id"]}'] + [
                f'{prefix}-{version}-{arch}-{state["id"]}' for arch in ('x86_64', 'aarch64')]
        for name in required:
            job = latest.get(name, {})
            if job.get('status') != 'completed' or job.get('conclusion') != 'success':
                raise Deferred(f"cannot resume original publication without successful {name}; retain its stage")
        available = paginated(f"repos/{repository}/actions/runs/{state['id']}/artifacts?per_page=100", 'artifacts')
        for name in expected:
            rows = [item for item in available if item.get('name') == name]
            if len(rows) != 1 or rows[0].get('expired') is not False:
                raise Deferred(f"original publication artifact is missing or expired: {name}; "
                               "retain partial bytes and select a fresh Preview")
        if expected_title is not None and state.get('display_title') != expected_title:
            raise Deferred(f'{unit} {version} has a different validated source; recover its original publisher before Go alignment')
        candidates.append((state, publisher))
    if len(candidates) > 1:
        raise Deferred('multiple accepted stages could own this publication; retain them and resolve the conflict')
    if not candidates:
        return False
    state, publisher = candidates[0]
    count_publication(repository, unit, str(state['id']))
    # The publisher revalidates the original stage, version, current-run results,
    # actual package bytes and any already uploaded assets before writing.
    gh('api', '--method', 'POST', f"repos/{repository}/actions/jobs/{publisher['id']}/rerun", '--silent')
    print(f"==> resumed original {unit} publication: {state.get('html_url')}")
    return True


def ensure_aggregate(version: str, source_sha: str) -> bool:
    status = platform_release(version)
    if status.complete:
        if status.tag_sha != source_sha:
            raise Deferred(f"aggregate {version} exists on another platform commit")
        print(f"==> aggregate {version} is complete")
        return True
    if active_delete_run(PLATFORM_REPOSITORY, version) is not None:
        raise Pending(f"aggregate cleanup is active: {version}")
    if status.partial and recoverable_source_sha(status) != source_sha:
        raise Deferred(f"incomplete aggregate {version} has another or ambiguous source; retain it")
    runs = aggregate_runs(version)
    active = [item for item in runs if run_active(item)]
    if active:
        print(f"==> pending {active[0].get('html_url')}")
        return False
    exact = [item for item in runs if item.get('display_title') == aggregate_run_title(version, source_sha)]
    if resume_publish(PLATFORM_REPOSITORY, 'platform', version, exact):
        return False
    if status.partial:
        raise Deferred(f"incomplete aggregate {version} has no recoverable stage; retain it and select a fresh Preview")
    count_publication(PLATFORM_REPOSITORY, 'platform', version + ':build')
    dispatch_aggregate_run(version, source_sha)
    print(f"==> dispatched aggregate {version} with a fresh stage")
    return False


def converge(plans: dict[str, Plan], aggregate: str, source_sha: str) -> bool:
    ready: dict[str, bool] = {}
    for unit in UNITS:
        if any(not ready.get(dependency, False) for dependency in unit.dependencies):
            ready[unit.name] = False
            continue
        ready[unit.name] = ensure_unit(
            plans[unit.name], plans, aggregate, source_sha
        )
    if not all(ready.values()):
        return False
    return ensure_aggregate(aggregate, source_sha)


def validate_environment() -> None:
    if PLATFORM_REF != "main" and preview_selection.PLATFORM_RELEASE_BRANCH_RE.fullmatch(
        PLATFORM_REF
    ) is None:
        fail("PLATFORM_REF must be main or release/vMAJOR.MINOR.x")
    if re.fullmatch(r"[0-9a-f]{40}", PLATFORM_SHA) is None:
        fail("PLATFORM_SHA must be a full lowercase SHA")
    if re.fullmatch(r"[0-9]{8}(?:\.[1-9][0-9]*)?", TODAY) is None:
        fail("PREVIEW_DATE must match YYYYMMDD[.N]")
    if run(["git", "rev-parse", "HEAD"], cwd=PLATFORM_ROOT).stdout.strip() != PLATFORM_SHA:
        fail("platform checkout does not match PLATFORM_SHA")
    if branch_sha(PLATFORM_REPOSITORY, PLATFORM_REF) != PLATFORM_SHA:
        raise Deferred(f"platform {PLATFORM_REF} moved after the scanner selected it")
    selection.validate_current_manifests(PLATFORM_ROOT)


def main(deadline: float | None = None) -> str:
    global PLATFORM_SHA
    if deadline is None:
        deadline = time.monotonic() + WAIT_SECONDS
    validate_environment()
    base, previous, preview, previous_preview, configured = manifest_values()
    next_versions = manifest_next_versions()
    if PLATFORM_REF != "main":
        branch = re.fullmatch(r"release/v([0-9]+)\.([0-9]+)\.x", PLATFORM_REF)
        version = re.fullmatch(r"release-v([0-9]+)\.([0-9]+)\.[0-9]+", base)
        assert branch is not None and version is not None
        if branch.groups() != version.groups():
            raise RuntimeError(f"{base} does not belong to {PLATFORM_REF}")
    current_date = preview.removeprefix("preview.")
    current_aggregate = f"{base}-{preview}"
    formal = release_version()
    formal_status = platform_release(formal)
    if formal == base and formal_status.release is not None:
        if not formal_status.complete:
            raise Deferred(
                f"{base} has an incomplete Stable Release; its Preview line is closed"
            )
        print(f"==> {base} is closed by its Stable aggregate; no Preview may be published")
        return "closed"

    current_status = platform_release(current_aggregate)
    if current_status.partial and not current_status.complete:
        recovery_sha = recoverable_source_sha(current_status)
        if recovery_sha is None:
            raise Deferred(f"{current_aggregate} has ambiguous partial bytes; retain it and select a fresh Preview")
        if not ensure_aggregate(current_aggregate, recovery_sha):
            raise Pending(f"recover the original {current_aggregate} before changing the channel")
        current_status = platform_release(current_aggregate)
        if not current_status.complete:
            raise Pending(f"recovered {current_aggregate} is waiting for Release API convergence")
    requested_order = selection.preview_order(f"preview.{TODAY}")
    current_order = selection.preview_order(preview)
    date = current_date
    next_previous_preview = previous_preview
    if current_status.complete:
        if requested_order <= current_order:
            print(f"==> maintained preview is current: {current_aggregate}")
            return "published"
        date = TODAY
        next_previous_preview = preview
    elif requested_order > current_order:
        date = TODAY
        # An unpublished selection may contain component tags that never existed.
        # Keep its previous published baseline rather than comparing against it.

    active_aggregate = active_aggregate_run(current_aggregate)
    if active_aggregate is not None:
        raise Pending(
            f"aggregate release is active; keep its manifest immutable: "
            f"{active_aggregate.get('html_url')}"
        )
    active_aggregate_cleanup = active_delete_run(
        PLATFORM_REPOSITORY, current_aggregate
    )
    if active_aggregate_cleanup is not None:
        raise Pending(
            f"aggregate cleanup is active; keep its manifest immutable: "
            f"{active_aggregate_cleanup.get('html_url')}"
        )

    for name, configured_version in configured.items():
        unit = UNIT_BY_NAME[name]
        active_release = active_release_run(
            unit.repository, unit.workflow, configured_version
        )
        if active_release is not None:
            raise Pending(
                f"configured component release is active; keep its manifest immutable: "
                f"{active_release.get('html_url')}"
            )
        active_cleanup = active_delete_run(unit.repository, configured_version)
        if active_cleanup is not None:
            raise Pending(
                f"configured component cleanup is active; keep its manifest immutable: "
                f"{active_cleanup.get('html_url')}"
            )

    recover_configured_components(configured, current_aggregate, PLATFORM_SHA)
    plans = plan_units(configured, date, next_versions)
    for plan in plans.values():
        active_release = active_release_run(
            plan.unit.repository, plan.unit.workflow, plan.selected
        )
        if active_release is not None:
            raise Pending(
                f"component release is active; keep its manifest immutable: "
                f"{active_release.get('html_url')}"
            )
        active_cleanup = active_delete_run(plan.unit.repository, plan.selected)
        if active_cleanup is not None:
            raise Pending(
                f"component cleanup is active; keep its manifest immutable: "
                f"{active_cleanup.get('html_url')}"
            )
    def retain_existing_publishers():
        # A failed publisher may already own accepted bytes without a tag/Release.
        # Recover that frozen tuple before opening a PR that changes its source.
        if not current_status.complete:
            runs = [run for run in aggregate_runs(current_aggregate)
                    if aggregate_title_matches(run.get('display_title'), current_aggregate)]
            if resume_publish(PLATFORM_REPOSITORY, 'platform', current_aggregate, runs,
                              expected_title=aggregate_run_title(current_aggregate, PLATFORM_SHA)):
                raise Pending(f'recover the original {current_aggregate} publisher before Go alignment')
        for name, plan in plans.items():
            if plan.action != 'publish' or plan.selected != configured[name]:
                continue
            runs = release_runs(plan.unit.repository, plan.unit.workflow, plan.selected)
            if resume_publish(plan.unit.repository, name, plan.selected, runs,
                              expected_title=release_run_title(plan, plans)):
                raise Pending(f'recover the original {name} {plan.selected} publisher before Go alignment')

    go_dependency_alignment.check_plans(sys.modules[__name__], plans, next_versions,
                                       date=date, before_alignment=retain_existing_publishers)
    changed = any(plans[name].selected != configured[name] for name in configured)
    if current_status.complete:
        changed = changed or platform_changed_since(current_aggregate)
        if not changed:
            print(f"==> no source changes since {current_aggregate}; keep maintained preview")
            return "unchanged"

    if time.monotonic() >= deadline:
        print("==> selection exhausted the branch budget; resume before manifest mutation")
        return "pending"
    aggregate = f"{base}-preview.{date}"
    go_dependency_alignment.confirm_inputs(sys.modules[__name__], plans)
    content = render_manifest(base, previous, date, next_previous_preview, plans, next_versions)
    PLATFORM_SHA = persist_manifest(content, aggregate,
                                    source_shas={name: plan.source_sha for name, plan in plans.items()})
    return converge_until_deadline(plans, aggregate, deadline)


def converge_until_deadline(plans: dict[str, Plan], aggregate: str, deadline: float) -> str:
    while time.monotonic() < deadline:
        try:
            if converge(plans, aggregate, PLATFORM_SHA):
                return "published"
        except Pending as error:
            print(f"==> pending: {error}")
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(POLL_SECONDS, remaining))
    print("==> Daily Preview remains pending; scanner must continue convergence")
    return "pending"


def run_branch() -> dict[str, str]:
    """Keep healthy preflight work alive and report its actual terminal state."""
    _cleanup_dispatches.clear()
    _publication_resumes.clear()
    selected_sha = PLATFORM_SHA
    deadline = time.monotonic() + WAIT_SECONDS
    reason = ""
    while True:
        try:
            outcome = main(deadline=deadline)
            break
        except Pending as error:
            reason = str(error)
            print(f"==> pending: {reason}")
            if time.monotonic() >= deadline:
                outcome = "pending"
                break
            time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                outcome = "pending"
                break
        except Deferred as error:
            outcome, reason = "deferred", str(error)
            print(f"==> deferred without mutation: {reason}")
            break
    if outcome not in {"published", "unchanged", "closed", "pending", "deferred"}:
        raise RuntimeError(f"invalid Preview outcome: {outcome!r}")
    result = dict(status=outcome, reason=reason, platform_ref=PLATFORM_REF,
                  selected_sha=selected_sha, final_sha=PLATFORM_SHA, date=TODAY)
    path = os.environ.get("PREVIEW_RESULT_PATH")
    if path:
        pathlib.Path(path).write_text(json.dumps(result, sort_keys=True) + "\n")
    return result


if __name__ == "__main__":
    try:
        run_branch()
    except (RuntimeError, selection.ManifestError, preview_selection.SelectionError) as error:
        fail(str(error))
