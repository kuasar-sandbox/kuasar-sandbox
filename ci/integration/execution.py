"""Process and private-directory primitives for artifact validation jobs."""
from contextlib import contextmanager
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile

import artifacts


@contextmanager
def scratch_directory():
    # Short task-owned paths preserve Unix socket and direct-I/O contracts.
    state = Path(tempfile.mkdtemp(prefix="ki-", dir=os.environ.get("TMPDIR", "/var/tmp")))
    try:
        yield state
    finally:
        try:
            shutil.rmtree(state)
        except PermissionError:
            subprocess.run(["sudo", "-n", "rm", "-rf", "--", str(state)], check=True)


def environment(state):
    selected = os.environ.copy()
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "CALLER_TOKEN", "KUASAR_CI_APP_PRIVATE_KEY"):
        artifacts.require(not selected.get(key), f"artifact execution must not receive {key}")
    for name in ("tmp", "docker", "metrics", "perf"):
        (state / name).mkdir(mode=0o700)
    prepared = dict(TMPDIR=str(state / "tmp"), DOCKER_CONFIG=str(state / "docker"),
                    KUASAR_CI_DIR=str(state / "metrics"), PERF_OUT_DIR=str(state / "perf"),
                    PYTHONDONTWRITEBYTECODE="1", PATH=selected["PATH"])
    return selected | prepared, prepared


def privileged_command(command, prepared):
    # Keep the existing root execution condition; never forward the entire env.
    if os.geteuid() == 0:
        return command
    # sudo strips some variables (notably TMPDIR) even with preserve-env.
    # Set only these explicit task inputs after the privilege transition.
    return ["sudo", "-n", "--", "env", *[f"{key}={value}" for key, value in sorted(prepared.items())], *command]


def result_record(plan, arch, provenance, workspace):
    return {"arch": arch, "plan_id": artifacts.identity(plan), "conclusion": "failure",
            "test_revisions": provenance["test_revisions"],
            "provenance_sha256": artifacts.digest(workspace / "provenance.json"), "timings": [],
            "preparation_environment": provenance.get("preparation_environment")}


def runtime_preflight(root):
    return ('set -eu; for tool in go cargo rustc cc gcc g++ clang clang++; do '
            'if command -v "$tool" >/dev/null 2>&1; then echo "unexpected compiler: $tool" >&2; exit 1; fi; done; '
            'test ! -d /usr/local/go; test ! -d /root/.cargo; '
            f'test ! -e {root}/go.mod; test ! -e {root}/Cargo.toml; '
            'for owner in accelerator connector guest-runtime sandboxer orchestrator; do '
            f'test ! -d "{root}/$owner"; done; ')


def runtime_identity(image, *, verified=False):
    artifacts.require(re.fullmatch(r'sha256:[0-9a-f]{64}', image), 'clean execution needs an immutable runtime image ID')
    return {'kind': 'clean-container', 'image_id': image, 'verified': verified,
            'compilers': [], 'component_source_trees': []}


def clean_command(image, workspace, state, arguments):
    """Expose only immutable prepared inputs and this run's mutable directory."""
    runtime_identity(image)
    preflight = (runtime_preflight('/inputs') +
                 'exec python3 -B /inputs/test/e2e/e2e run --workdir /inputs '
                 '--run-root /state/cases --out-root /state/out --result /state/result.json "$@"')
    return ['docker', 'run', '--rm', '--pull=never', '--privileged', '--network=host', '--read-only',
            '--mount', f'type=bind,src={workspace},dst=/inputs,readonly',
            '--mount', f'type=bind,src={state},dst=/state',
            '--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
            '--tmpfs', '/run', '--tmpfs', '/tmp', '--env', 'TMPDIR=/state/tmp',
            '--env', 'DOCKER_CONFIG=/state/docker', '--env', 'PYTHONDONTWRITEBYTECODE=1',
            image, 'bash', '-c', preflight, 'clean-e2e', *arguments]


def clean_prepare_command(image, release, work, arguments, deps_dir=None):
    runtime_identity(image)
    artifacts.require(work.name in artifacts.ARCHES, 'clean preparation requires an architecture directory')
    preflight = (runtime_preflight('/release') +
                 'exec python3 -B /release/test/e2e/e2e prepare --release-dir /release '
                 f'--workdir /prepared/{work.name} "$@"')
    inputs = []
    if deps_dir is not None:
        directory = Path(deps_dir)
        artifacts.require(str(deps_dir) != '' and directory.is_dir() and not directory.is_symlink(),
                          f'missing or unsafe deps-dir: {deps_dir}')
        directory = directory.resolve()
        artifacts.require(not directory.is_relative_to(work.parent.resolve()) and
                          not work.parent.resolve().is_relative_to(directory),
                          'deps-dir must be separate from the writable preparation mount')
        artifacts.require(',' not in str(directory), 'deps-dir cannot contain a Docker mount separator')
        inputs += ['--mount', f'type=bind,src={directory},dst=/deps,readonly']
        arguments = [*arguments, '--deps-dir', '/deps']
    if 'E2E_OFFLINE' in os.environ:
        inputs += ['--env', 'E2E_OFFLINE=' + os.environ['E2E_OFFLINE']]
    return ['docker', 'run', '--rm', '--pull=never', '--network=host', '--read-only',
            '--user', f'{os.getuid()}:{os.getgid()}', '--group-add', str(Path('/var/run/docker.sock').stat().st_gid),
            '--mount', f'type=bind,src={release},dst=/release,readonly',
            '--mount', f'type=bind,src={work.parent},dst=/prepared',
            '--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
            '--tmpfs', '/run', '--tmpfs', '/tmp', '--env', 'DOCKER_CONFIG=/tmp/docker',
            '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'HOME=/tmp/e2e-home',
            *inputs, image, 'bash', '-c', preflight, 'clean-prepare', *arguments]
