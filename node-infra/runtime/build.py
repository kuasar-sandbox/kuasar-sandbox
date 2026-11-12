#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build the deployment helper against the aggregate's exact Connector source.

This temporary module deliberately follows the aggregate selection, rather than
introducing an independently versioned copy of Connector's configuration parser.
Go, GOROOT and GOTOOLCHAIN are supplied by the standard build environment.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ARCHES = {"x86_64": "amd64", "aarch64": "arm64", "amd64": "amd64", "arm64": "arm64"}


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, text=True, **kwargs)


def build(args):
    selection = json.loads(args.selection.read_text())
    selected = selection["connector"]
    sha = selected["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("selection.connector.sha must be an exact commit")
    source = (args.sources / "connector").resolve()
    actual = run(["git", "-C", source, "rev-parse", "HEAD"], capture_output=True).stdout.strip()
    if actual != sha:
        raise ValueError(f"Connector source HEAD {actual} differs from selected {sha}")
    dirty = run(["git", "-C", source, "status", "--porcelain", "--untracked-files=no"], capture_output=True).stdout
    if dirty.strip():
        raise ValueError("Connector source has tracked modifications")
    go_version = re.search(r"(?m)^go\s+(\S+)$", (source / "go.mod").read_text())
    if not go_version:
        raise ValueError("Connector go.mod has no Go version")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="node-infra-runtime-") as temporary:
        module = Path(temporary)
        for path in Path(__file__).parent.glob("*.go"):
            shutil.copy2(path, module / path.name)
        environment = dict(os.environ, CGO_ENABLED="0", GOOS="linux", GOARCH=ARCHES[args.arch])
        version = environment.get("KUASAR_GO_RELEASE_VERSION")
        if version:
            sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "release"))
            import go_release_consumer
            env = go_release_consumer.prepare(module, "connector", args.sources.resolve(),
                                              selection, version, environment)
        else:
            # Source-mode CI and local workspace images also support unpublished PR inputs.
            env = dict(environment, GOWORK="off")
            (module / "connector").symlink_to(source, target_is_directory=True)
            (module / "go.mod").write_text(
                "module github.com/kuasar-sandbox/kuasar-sandbox/node-infra/runtime\n\n"
                f"go {go_version.group(1)}\n\n"
                "require github.com/kuasar-sandbox/connector v0.0.0\n\n"
                "replace github.com/kuasar-sandbox/connector => ./connector\n"
            )
            shutil.copy2(source / "go.sum", module / "go.sum")
            run(["go", "mod", "tidy"], cwd=module, env=env)
        if args.test:
            run(["go", "test", "./..."], cwd=module, env=env)
        run(["go", "build", "-trimpath", "-ldflags=-s -w", "-o", args.output.resolve(), "."], cwd=module, env=env)
    args.output.chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--arch", choices=ARCHES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test", action="store_true", help="run helper tests on the native target architecture")
    build(parser.parse_args())


if __name__ == "__main__":
    main()
