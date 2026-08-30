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
POLL_SECONDS = int(os.environ.get("PREVIEW_POLL_SECONDS", "120"))


def load_module(name: str, path: pathlib.Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


selection = load_module("release_selection", CONTROL_ROOT / "release/selection.py")
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
        "sandboxer",
        "kuasar-sandbox/sandboxer",
        "release.yml",
        ("accelerator", "connector"),
    ),
    Unit(
        "orchestrator",
        "kuasar-sandbox/orchestrator",
        "component-release.yml",
        ("accelerator", "connector", "sandboxer"),
    ),
    Unit(
        "runtime",
        "kuasar-sandbox/guest-runtime",
        "release-runtime.yml",
        ("accelerator", "connector", "sandboxer"),
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
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(f"{' '.join(command)}: {detail}")
    return result


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


def archive_name(unit: str, tag: str) -> str:
    if unit == "runtime":
        return f"sandbox-runtime-x86_64-{tag.removeprefix('runtime-')}.tar.gz"
    if unit == "vmlinux":
        return f"vmlinux-x86_64-{tag.removeprefix('vmlinux-')}.tar.gz"
    if unit == "platform":
        return f"platform-{tag}.tar.gz"
    return f"{unit}-{tag}-linux-x86_64.tar.gz"


def platform_asset_names(tag: str, sha: str) -> set[str]:
    relative = (
        "releases/daily-preview.yaml"
        if "-preview." in tag
        else "releases/release.yaml"
    )
    state = api_optional(
        f"repos/{PLATFORM_REPOSITORY}/contents/{relative}?ref={sha}"
    )
    if state is None or not isinstance(state.get("content"), str):
        raise Deferred(f"platform {tag} has no release manifest at {sha}")
    try:
        content = base64.b64decode(state["content"].replace("\n", "")).decode()
        aggregate, _, components = selection.parse_manifest(
            content, f"{sha}:{relative}", "-preview." in tag
        )
    except (ValueError, UnicodeDecodeError, selection.ManifestError) as error:
        raise Deferred(f"platform {tag} has an invalid release manifest at {sha}") from error
    if aggregate != tag:
        raise Deferred(f"platform manifest at {sha} selects {aggregate}, not {tag}")
    return {
        "SHA256SUMS",
        archive_name("platform", tag),
        *(archive_name(unit, components[unit]) for unit in selection.UNITS),
    }


def tag_sha(repository: str, tag: str) -> str | None:
    state = api_optional(f"repos/{repository}/git/ref/tags/{quote(tag, safe='')}")
    if state is None:
        return None
    target = state.get("object")
    if not isinstance(target, dict) or target.get("type") != "commit":
        raise Deferred(f"{repository} {tag} is not a lightweight commit tag")
    sha = target.get("sha")
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise Deferred(f"{repository} {tag} has an invalid tag target")
    return sha


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
            if not isinstance(target, dict) or target.get("type") != "commit":
                self.invalid_tags.add(tag)
                continue
            sha = target.get("sha")
            if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha):
                self.tag_shas[tag] = sha
        self._statuses: dict[str, ReleaseStatus] = {}

    def status(self, tag: str) -> ReleaseStatus:
        if tag in self._statuses:
            return self._statuses[tag]
        release = self.releases.get(tag)
        if tag in self.invalid_tags:
            raise Deferred(
                f"{self.unit.repository} {tag} is not a lightweight commit tag"
            )
        sha = self.tag_shas.get(tag)
        prerelease = "-preview." in tag
        archive = archive_name(self.unit.name, tag)
        complete = False
        if release is not None:
            assets = release.get("assets")
            complete = (
                release.get("draft") is False
                and release.get("prerelease") is prerelease
                and isinstance(assets, list)
                and sorted(asset.get("name") for asset in assets)
                == sorted(("SHA256SUMS", archive))
                and all(asset.get("state") == "uploaded" for asset in assets)
                and sha is not None
                and release.get("target_commitish") == sha
            )
        status = ReleaseStatus(release, sha, complete)
        self._statuses[tag] = status
        return status

    def usable_tags(self) -> list[str]:
        return [tag for tag in self.releases if self.status(tag).complete]


def recoverable_source_sha(status: ReleaseStatus) -> str | None:
    if status.tag_sha is not None:
        return status.tag_sha
    if status.release is None:
        return None
    target = status.release.get("target_commitish")
    if isinstance(target, str) and re.fullmatch(r"[0-9a-f]{40}", target):
        return target
    return None


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
            "--force",
            "origin",
            f"refs/heads/{source_ref}:refs/heads/{source_ref}",
            "+refs/tags/*:refs/tags/*",
        ],
        cwd=destination,
    )


def latest_run(repository: str, workflow: str, title: str) -> dict[str, Any] | None:
    runs = paginated(
        f"repos/{repository}/actions/workflows/{workflow}/runs?event=workflow_dispatch&per_page=100",
        "workflow_runs",
    )
    matching = [item for item in runs if item.get("display_title") == title]
    return max(matching, key=lambda item: str(item.get("created_at", "")), default=None)


def run_active(state: dict[str, Any]) -> bool:
    return state.get("status") in {
        "queued",
        "in_progress",
        "pending",
        "requested",
        "waiting",
    }


def dispatch_cleanup(plan: Plan, mode: str) -> None:
    title = f"Delete {plan.selected} ({mode})"
    state = latest_run(plan.unit.repository, "delete-preview.yml", title)
    if state is not None and run_active(state):
        raise Pending(f"cleanup is active: {state.get('html_url')}")
    if state is not None and state.get("conclusion") == "success":
        raise Pending(f"cleanup completed and API state is converging: {state.get('html_url')}")
    if state is not None and int(state.get("run_attempt", 1)) < 3:
        gh("run", "rerun", str(state["id"]), "--repo", plan.unit.repository, "--failed")
        raise Pending(f"reran cleanup: {state.get('html_url')}")
    if state is not None:
        raise Deferred(f"cleanup remains failed after three attempts: {state.get('html_url')}")
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
    raise Pending(f"dispatched {mode} cleanup for {plan.unit.name} {plan.selected}")


def ensure_configured_state(
    unit: Unit,
    configured: str,
    state: RepositoryState,
) -> None:
    status = state.status(configured)
    if not status.partial or status.complete:
        return
    if "-preview." not in configured:
        raise Deferred(f"fixed Stable selection is incomplete: {unit.name} {configured}")
    source_sha = recoverable_source_sha(status)
    if source_sha is None:
        run_state = latest_run(unit.repository, unit.workflow, f"Release {configured}")
        if run_state is not None and run_active(run_state):
            raise Pending(f"release is active: {run_state.get('html_url')}")
        raise Deferred(
            f"{unit.name} {configured} has no recoverable source target; keep it deferred"
        )
    plan = Plan(unit, configured, None, source_sha, configured, configured, "cleanup")
    run_state = latest_run(unit.repository, unit.workflow, f"Release {configured}")
    if run_state is not None and run_active(run_state):
        raise Pending(f"release is active: {run_state.get('html_url')}")
    dispatch_cleanup(plan, "incomplete")


def make_plan(
    unit: Unit,
    configured: str,
    date: str,
    temporary: pathlib.Path,
) -> Plan:
    state = RepositoryState(unit)
    ensure_configured_state(unit, configured, state)
    source_ref = preview_selection.component_source_ref(
        PLATFORM_REF, unit.name, configured
    )
    source_sha = branch_sha(unit.repository, source_ref)
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
        repository, source_ref, unit.name, configured, usable, date
    )
    if resolution.head != source_sha:
        raise Deferred(f"{unit.repository}:{source_ref} moved while selecting")
    selected_state = state.status(resolution.selected)
    if selected_state.partial and not selected_state.complete:
        if resolution.selected == configured and "-preview." in configured:
            recovery_sha = recoverable_source_sha(selected_state)
            if recovery_sha is None:
                raise Deferred(
                    f"{unit.name} {resolution.selected} has no recoverable source target"
                )
            dispatch_cleanup(
                Plan(
                    unit,
                    configured,
                    source_ref,
                    recovery_sha,
                    resolution.winner,
                    resolution.selected,
                    "cleanup",
                ),
                "incomplete",
            )
        raise Deferred(
            f"ignore foreign incomplete publication: {unit.name} {resolution.selected}"
        )
    if selected_state.complete and selected_state.tag_sha != source_sha:
        raise Deferred(
            f"{unit.name} {resolution.selected} exists on another source commit"
        )
    return Plan(
        unit,
        configured,
        source_ref,
        source_sha,
        resolution.winner,
        resolution.selected,
        resolution.action,
    )


def force_dependency_preview(plan: Plan, date: str, state: RepositoryState) -> Plan:
    if plan.source_ref is None:
        return plan
    selected = preview_selection.preview_candidate(plan.unit.name, plan.winner, date)
    status = state.status(selected)
    if status.partial and not status.complete:
        if selected == plan.configured:
            recovery_sha = recoverable_source_sha(status)
            if recovery_sha is None:
                raise Deferred(
                    f"{plan.unit.name} {selected} has no recoverable source target"
                )
            dispatch_cleanup(
                replace(plan, selected=selected, source_sha=recovery_sha),
                "incomplete",
            )
        raise Deferred(
            f"ignore foreign incomplete dependency rebuild: {plan.unit.name} {selected}"
        )
    if status.complete and status.tag_sha != plan.source_sha:
        raise Deferred(f"dependency rebuild tag collision: {plan.unit.name} {selected}")
    if status.complete and selected == plan.winner:
        raise Deferred(
            f"{plan.unit.name} needs another dependency rebuild on the same Preview date"
        )
    action = "reuse" if status.complete else "publish"
    return replace(plan, selected=selected, action=action)


def plan_units(configured: dict[str, str], date: str) -> dict[str, Plan]:
    plans: dict[str, Plan] = {}
    with tempfile.TemporaryDirectory(prefix="preview-source-") as directory:
        temporary = pathlib.Path(directory)
        for unit in UNITS:
            plan = make_plan(unit, configured[unit.name], date, temporary)
            dependency_changed = any(
                plans[name].selected != configured[name] for name in unit.dependencies
            )
            if dependency_changed and plan.action != "publish":
                plan = force_dependency_preview(plan, date, RepositoryState(unit))
            plans[unit.name] = plan
    return plans


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


def platform_release(tag: str) -> ReleaseStatus:
    release = api_optional(f"repos/{PLATFORM_REPOSITORY}/releases/tags/{quote(tag, safe='')}")
    sha = tag_sha(PLATFORM_REPOSITORY, tag)
    if release is None:
        return ReleaseStatus(None, sha, False)
    assets = release.get("assets")
    expected = platform_asset_names(tag, sha) if sha is not None else set()
    complete = (
        release.get("draft") is False
        and release.get("prerelease") is ("-preview." in tag)
        and isinstance(assets, list)
        and {str(asset.get("name")) for asset in assets} == expected
        and all(asset.get("state") == "uploaded" for asset in assets)
        and sha is not None
        and release.get("target_commitish") == sha
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
) -> str:
    lines = [f"version: {base}"]
    if previous is not None:
        lines.append(f"previous_version: {previous}")
    lines.append(f"preview_version: preview.{date}")
    if previous_preview is not None:
        lines.append(f"previous_preview_version: {previous_preview}")
    lines.append("components:")
    for name in selection.UNITS:
        lines.append(f"  {name}: {plans[name].selected}")
    return "\n".join(lines) + "\n"


def persist_manifest(content: str, aggregate: str) -> str:
    relative = "releases/daily-preview.yaml"
    local_path = PLATFORM_ROOT / relative
    current = local_path.read_text(encoding="utf-8")
    if current == content:
        return PLATFORM_SHA
    head = branch_sha(PLATFORM_REPOSITORY, PLATFORM_REF)
    if head != PLATFORM_SHA:
        raise Pending(f"platform {PLATFORM_REF} moved; rescan the new head")
    remote = api(
        f"repos/{PLATFORM_REPOSITORY}/contents/{relative}?ref={quote(PLATFORM_REF, safe='')}"
    )
    decoded = base64.b64decode(str(remote["content"]).replace("\n", "")).decode()
    if decoded != current:
        raise Pending("daily-preview.yaml changed concurrently; rescan")
    request = {
        "message": f"release: select daily preview {aggregate.removeprefix('release-')}",
        "content": base64.b64encode(content.encode()).decode(),
        "branch": PLATFORM_REF,
        "sha": remote["sha"],
    }
    with tempfile.NamedTemporaryFile("w", encoding="utf-8") as handle:
        json.dump(request, handle)
        handle.flush()
        result = gh_platform(
            "api",
            "--method",
            "PUT",
            f"repos/{PLATFORM_REPOSITORY}/contents/{relative}",
            "--input",
            handle.name,
        )
    response = json.loads(result.stdout)
    sha = response["commit"]["sha"]
    local_path.write_text(content, encoding="utf-8")
    print(f"==> committed {PLATFORM_REF}:{relative} at {sha}")
    return str(sha)


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
        print(f"==> reuse {plan.unit.repository} {plan.selected}")
        return True
    if plan.source_ref is None:
        raise Deferred(f"fixed selection is unavailable: {plan.unit.name} {plan.selected}")
    title = f"Release {plan.selected}"
    if status.partial and "-preview." in plan.selected:
        state_run = latest_run(plan.unit.repository, plan.unit.workflow, title)
        if state_run is not None and run_active(state_run):
            print(f"==> pending {state_run.get('html_url')}")
            return False
        recovery_sha = recoverable_source_sha(status)
        if recovery_sha is None:
            raise Deferred(
                f"{plan.unit.name} {plan.selected} has no recoverable source target"
            )
        dispatch_cleanup(replace(plan, source_sha=recovery_sha), "incomplete")

    state_run = latest_run(plan.unit.repository, plan.unit.workflow, title)
    if state_run is None:
        gh(
            "workflow",
            "run",
            plan.unit.workflow,
            "--repo",
            plan.unit.repository,
            "--ref",
            "main",
            *release_inputs(plan, plans, aggregate, aggregate_sha),
        )
        print(f"==> dispatched {plan.unit.name} {plan.selected} from {plan.source_ref}")
        return False
    if run_active(state_run):
        print(f"==> pending {state_run.get('html_url')}")
        return False
    if state_run.get("conclusion") == "success":
        raise Pending(f"successful run is waiting for Release API convergence: {title}")
    attempt = int(state_run.get("run_attempt", 1))
    if attempt < 3:
        gh(
            "run",
            "rerun",
            str(state_run["id"]),
            "--repo",
            plan.unit.repository,
            "--failed",
        )
        print(f"==> reran failed publication: {state_run.get('html_url')}")
        return False
    raise Deferred(f"publication remains failed after three attempts: {state_run.get('html_url')}")


def dispatch_platform_cleanup(tag: str, source_sha: str) -> None:
    release_state = latest_run(
        PLATFORM_REPOSITORY, "aggregate-release.yml", f"Aggregate {tag}"
    )
    if release_state is not None and run_active(release_state):
        raise Pending(f"aggregate release is active: {release_state.get('html_url')}")
    title = f"Delete {tag} (incomplete)"
    state = latest_run(PLATFORM_REPOSITORY, "delete-preview.yml", title)
    if state is not None and run_active(state):
        raise Pending(f"platform cleanup is active: {state.get('html_url')}")
    if state is not None and state.get("conclusion") == "success":
        raise Pending("platform cleanup completed and API state is converging")
    if state is not None and int(state.get("run_attempt", 1)) < 3:
        gh("run", "rerun", str(state["id"]), "--repo", PLATFORM_REPOSITORY, "--failed")
        raise Pending(f"reran platform cleanup: {state.get('html_url')}")
    if state is not None:
        raise Deferred(
            f"platform cleanup remains failed after three attempts: {state.get('html_url')}"
        )
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
    raise Pending(f"dispatched incomplete aggregate cleanup for {tag}")


def ensure_aggregate(version: str, source_sha: str) -> bool:
    status = platform_release(version)
    if status.complete:
        if status.tag_sha != source_sha:
            raise Deferred(f"aggregate {version} exists on another platform commit")
        print(f"==> aggregate {version} is complete")
        return True
    if status.partial and "-preview." in version:
        recovery_sha = recoverable_source_sha(status)
        if recovery_sha is None:
            raise Deferred(f"aggregate {version} has no recoverable source target")
        dispatch_platform_cleanup(version, recovery_sha)
    title = f"Aggregate {version}"
    state = latest_run(PLATFORM_REPOSITORY, "aggregate-release.yml", title)
    if state is None:
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
        print(f"==> dispatched aggregate {version} from {PLATFORM_REF}")
        return False
    if run_active(state):
        print(f"==> pending {state.get('html_url')}")
        return False
    if state.get("conclusion") == "success":
        raise Pending("aggregate run is waiting for Release API convergence")
    attempt = int(state.get("run_attempt", 1))
    if attempt < 3:
        gh("run", "rerun", str(state["id"]), "--repo", PLATFORM_REPOSITORY, "--failed")
        return False
    raise Deferred(f"aggregate publication failed: {state.get('html_url')}")


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
    if re.fullmatch(r"[0-9]{8}", TODAY) is None:
        fail("PREVIEW_DATE must match YYYYMMDD")
    if run(["git", "rev-parse", "HEAD"], cwd=PLATFORM_ROOT).stdout.strip() != PLATFORM_SHA:
        fail("platform checkout does not match PLATFORM_SHA")
    if branch_sha(PLATFORM_REPOSITORY, PLATFORM_REF) != PLATFORM_SHA:
        raise Deferred(f"platform {PLATFORM_REF} moved after the scanner selected it")
    selection.validate_current_manifests(PLATFORM_ROOT)


def main() -> None:
    global PLATFORM_SHA
    validate_environment()
    base, previous, preview, previous_preview, configured = manifest_values()
    current_date = preview.removeprefix("preview.")
    current_aggregate = f"{base}-{preview}"
    formal = release_version()
    formal_status = platform_release(formal)
    if formal == base and formal_status.complete:
        print(f"==> {base} is closed by its Stable aggregate; no Preview may be published")
        return

    current_status = platform_release(current_aggregate)
    date = current_date
    next_previous_preview = previous_preview
    if current_status.complete:
        if TODAY <= current_date:
            print(f"==> maintained preview is current: {current_aggregate}")
            return
        date = TODAY
        next_previous_preview = preview

    plans = plan_units(configured, date)
    changed = any(plans[name].selected != configured[name] for name in configured)
    if current_status.complete:
        changed = changed or platform_changed_since(current_aggregate)
        if not changed:
            print(f"==> no source changes since {current_aggregate}; keep maintained preview")
            return

    aggregate = f"{base}-preview.{date}"
    content = render_manifest(base, previous, date, next_previous_preview, plans)
    PLATFORM_SHA = persist_manifest(content, aggregate)
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        try:
            if converge(plans, aggregate, PLATFORM_SHA):
                return
        except Pending as error:
            print(f"==> pending: {error}")
        if time.monotonic() >= deadline:
            print("==> Daily Preview remains pending; the next scan resumes it")
            return
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except Pending as error:
        print(f"==> pending without failure: {error}")
    except Deferred as error:
        print(f"==> deferred without mutation: {error}")
    except (RuntimeError, selection.ManifestError, preview_selection.SelectionError) as error:
        fail(str(error))
