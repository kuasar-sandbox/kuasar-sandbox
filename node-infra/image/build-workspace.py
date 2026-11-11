#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build a local, native node-infra image from the standard source workspace."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "release"))
import node_infra_assets
import selection
import tag_sources
import workspace_packages


def revision(repo):
    if not repo.is_dir() or Path(tag_sources.git(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise ValueError(f"expected a Git checkout at {repo}")
    flags = tag_sources.git(repo, "-c", "core.quotePath=true", "ls-files", "-v")
    if re.search(r"^[a-zS] ", flags, re.MULTILINE):
        raise ValueError(f"{repo}: cannot verify a checkout with assume-unchanged or skip-worktree entries")
    if tag_sources.git(repo, "status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=none"):
        raise ValueError(f"{repo}: commit or set aside local changes before building the image")
    return tag_sources.git(repo, "rev-parse", "--verify", "HEAD^{commit}")


def identify(root):
    platform_sha = revision(root)
    units = {}
    heads = {}
    for unit in selection.UNITS:
        repository = tag_sources.repository(unit)
        repo = root.parent / repository.split("/")[1]
        if repo not in heads:
            heads[repo] = revision(repo)
        sha = heads[repo]
        units[unit] = {"repository": repository, "sha": sha, "version": "git:" + sha}
    return platform_sha, units


def check_environment(engine, arch):
    if platform.system() != "Linux" or platform.machine() != arch:
        raise ValueError(f"node-infra-image needs a native Linux/{arch} builder (host: {platform.system()}/{platform.machine()})")
    for program in (engine, "skopeo", "go", "make", "git"):
        if not shutil.which(program):
            raise ValueError(f"missing {program} on PATH")
    containerfile = (HERE / "Containerfile").read_text()
    base = re.search(r"^ARG BASE_IMAGE=(\S+)$", containerfile, re.MULTILINE).group(1)
    if subprocess.run([engine, "image", "inspect", base], stdout=subprocess.DEVNULL).returncode:
        raise ValueError(f"check that {engine} is running and that its image store contains {base} "
                         f"(prepare it with: {engine} pull {base})")


def build(root, engine, arch):
    check_environment(engine, arch)
    sha, units = identify(root)
    identity = json.dumps([sha, units], sort_keys=True).encode()
    version = "local-" + hashlib.sha256(identity).hexdigest()[:16]
    sources = root.parent
    print(f"Building {version} for {arch} from {sources}", flush=True)
    node_infra_assets.require_config_sources(sources)
    env = workspace_packages.build_env(sources, units)
    subprocess.run(["make", "-C", str(root), "build", f"TARGET_ARCH={arch}"], env=env, check=True)
    dest = root / "build/node-infra" / arch
    dest.mkdir(parents=True, exist_ok=True)
    # Keep intermediates on the output filesystem; only expose complete builds.
    with tempfile.TemporaryDirectory(prefix=".building-", dir=dest) as temporary:
        work = Path(temporary)
        products = workspace_packages.stage(sources, work, arch, units, env=env)
        selected = work / "selection.json"
        selected.write_text(json.dumps(units, sort_keys=True) + "\n")
        output = work / "image"
        subprocess.run([sys.executable, "-B", str(HERE / "build.py"), "--products", str(products),
                        "--sources", str(sources), "--selection", str(selected), "--output", str(output),
                        "--version", version, "--source-revision", sha, "--arch", arch, "--engine", engine],
                       env=env, check=True)
        receipt = json.loads((output / f"node-infra-{arch}.json").read_text())
        names = (receipt["archive"], receipt["docker_archive"]["name"])
        if not all(Path(name).name == name and (output / name).is_file() for name in names):
            raise ValueError("image builder did not produce both archives listed in its receipt")
        if identify(root) != (sha, units):
            raise ValueError("source checkouts changed during the build; start a new build")
        # mkdtemp reserves a unique destination even for repeated builds of one source set.
        result = Path(tempfile.mkdtemp(prefix=version + "-", dir=dest))
        try:
            output.replace(result)
        except OSError:
            result.rmdir()
            raise
    print(f"Image archives and receipt: {result}")
    archive = result / receipt["docker_archive"]["name"]
    print(f"Load: {engine} load -i {shlex.quote(str(archive))}\nTag:  localhost/node-infra:{version}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", required=True, choices=("x86_64", "aarch64"))
    parser.add_argument("--engine", required=True, choices=("podman", "docker"))
    args = parser.parse_args()
    try:
        build(ROOT, args.engine, args.arch)
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"node-infra-image: {exc}\n")
