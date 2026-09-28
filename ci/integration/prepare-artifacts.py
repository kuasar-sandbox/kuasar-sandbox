#!/usr/bin/env python3
"""Compose a target once and prepare immutable fixtures, without product work."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import artifacts
import execution

def prepare(plan, arch, assets, delta, workspace, clean_image=None, deps_dir=None, offline=False):
    """Compose trusted inputs, then call the same preparation entry as users."""
    workspace.parent.mkdir(parents=True, exist_ok=True)
    deps_dir = deps_dir if deps_dir is not None else os.environ.get('E2E_DEPS_DIR')
    # Keep composed inputs outside the writable output mount used by clean
    # preparation; otherwise that mount would expose a writable alias.
    with tempfile.TemporaryDirectory(prefix="compose-") as directory:
        composed = Path(directory) / arch
        original = artifacts.compose(plan, arch, assets, delta, composed)
        cases = original["selection"]["cases"]
        if cases:
            command = [sys.executable, "-B", str(composed / "test/e2e/e2e"), "prepare",
                       "--release-dir", str(composed), "--workdir", str(workspace), "--arch", arch]
            for case in cases:
                command += ["--include", case]
            if deps_dir is not None:
                command += ['--deps-dir', str(deps_dir)]
            if offline:
                command += ['--offline']
            if clean_image:
                arguments = ['--arch', arch]
                for case in cases:
                    arguments += ['--include', case]
                if offline:
                    arguments += ['--offline']
                command = execution.clean_prepare_command(clean_image, composed, workspace, arguments, deps_dir)
            subprocess.run(command, check=True)
            prepared = json.loads((workspace / 'provenance.json').read_text())
            prepared['preparation_environment'] = execution.runtime_identity(clean_image, verified=True) if clean_image else {'kind': 'native-host'}
            (workspace / 'provenance.json').write_bytes(artifacts.canonical(prepared) + b'\n')
        else:
            # A static lane validates product architecture and provenance only;
            # it cannot prepare or claim execution of an unsupported case.
            composed.rename(workspace)
            original["prepared_cases"] = []
            (workspace / "provenance.json").write_bytes(artifacts.canonical(original) + b"\n")
        prepared = artifacts.verify_workspace(workspace, plan, arch)
        artifacts.require(all(prepared["files"].get(name) == value and
                              prepared["modes"].get(name) == original["modes"][name]
                              for name, value in original["files"].items()),
                          "public preparation modified composed product/test inputs")
        artifacts.require(prepared["prepared_cases"] == cases, "prepared case selection differs from plan")
        return prepared


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--arch", choices=artifacts.ARCHES, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--delta", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--clean-image", help="immutable runtime-only image for preparation acceptance")
    parser.add_argument("--deps-dir", help="pass a local image input directory to public prepare")
    parser.add_argument("--offline", action="store_true", help="pass --offline to public prepare")
    args = parser.parse_args()
    prepare(json.loads(args.plan.read_text()), args.arch, args.assets.resolve(), args.delta.resolve(),
            args.workspace.resolve(), args.clean_image, args.deps_dir, args.offline)


if __name__ == "__main__":
    main()
