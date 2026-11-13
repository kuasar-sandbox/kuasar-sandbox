#!/usr/bin/env python3
"""Run the current Stable release controller against this maintenance checkout."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    platform = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="kuasar-release-control-") as directory:
        control = Path(directory)
        subprocess.run(["git", "init", "--quiet", "--template=", control], check=True)
        git = ["git", "-C", str(control)]
        subprocess.run([*git, "fetch", "--quiet", "--no-tags", "--depth=1",
                        "https://github.com/kuasar-sandbox/kuasar-sandbox.git",
                        "refs/heads/main"], check=True)
        subprocess.run([*git, "checkout", "--quiet", "--detach", "FETCH_HEAD"], check=True)
        sha = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
        print(f"==> Stable release controller: kuasar-sandbox/kuasar-sandbox@{sha}", flush=True)
        return subprocess.run([sys.executable, "-B", control / "release/formal_coordinator.py"],
                              cwd=platform, env={**os.environ, "PLATFORM_ROOT": str(platform)}).returncode


if __name__ == "__main__":
    sys.exit(main())
