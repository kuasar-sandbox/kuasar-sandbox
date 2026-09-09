#!/usr/bin/env python3
"""Minimal single-node deployer for Kuasar Sandbox aggregate releases."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_VERSION = "release-v0.1.2"
DEFAULT_REPOSITORY = "kuasar-sandbox/kuasar-sandbox"
REQUIRED_COMMANDS = (
    "gh",
    "tar",
    "sha256sum",
    "python3",
    "openssl",
    "ip",
    "curl",
    "sqlite3",
    "iptables",
    "mkfs.ext4",
    "docker",
    "ldd",
)
APT_PACKAGES = (
    "ca-certificates",
    "curl",
    "docker.io",
    "e2fsprogs",
    "gh",
    "iproute2",
    "iptables",
    "libgcc-s1",
    "liblz4-1",
    "libsnappy1v5",
    "libstdc++6",
    "libzstd1",
    "openssl",
    "procps",
    "python3",
    "python3-venv",
    "sqlite3",
    "tar",
    "util-linux",
    "zlib1g",
)


class DeployError(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        capture_output=capture,
    )


def output(command: list[str]) -> str:
    return run(command, capture=True).stdout.strip()


def version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError as exc:
        raise DeployError(f"cannot parse version: {value}") from exc


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DeployError(message)


def command_succeeds_quietly(command: list[str]) -> bool:
    try:
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
    except OSError:
        return False


def missing_apt_packages() -> list[str]:
    if shutil.which("dpkg-query") is None:
        return list(APT_PACKAGES)
    return [
        package
        for package in APT_PACKAGES
        if not command_succeeds_quietly(["dpkg-query", "-W", "-f=${Status}", package])
    ]


def check_environment() -> None:
    print("Checking host requirements...")
    results: list[tuple[str, bool, str]] = []

    def record(name: str, passed: bool, detail: str) -> None:
        results.append((name, passed, detail))
        print(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}")

    record("operating system", platform.system() == "Linux", platform.system())
    record("architecture", platform.machine() == "x86_64", platform.machine())

    pid1 = "unavailable"
    if shutil.which("ps"):
        try:
            pid1 = subprocess.run(
                ["ps", "-p", "1", "-o", "comm="],
                text=True,
                capture_output=True,
                check=False,
            ).stdout.strip() or "unavailable"
        except OSError:
            pass
    record("systemd PID 1", pid1 == "systemd", pid1)

    cgroup_v2 = Path("/sys/fs/cgroup/cgroup.controllers").is_file()
    record("cgroup v2", cgroup_v2, "available" if cgroup_v2 else "not available")

    kvm = Path("/dev/kvm")
    kvm_present = kvm.exists() and kvm.is_char_device()
    record("/dev/kvm", kvm_present, "character device available" if kvm_present else "missing")

    libc_version = "unavailable"
    libc_ok = False
    if shutil.which("getconf"):
        try:
            libc_line = subprocess.run(
                ["getconf", "GNU_LIBC_VERSION"],
                text=True,
                capture_output=True,
                check=False,
            ).stdout.strip()
            if libc_line:
                libc_version = libc_line.split()[-1]
                libc_ok = version_tuple(libc_version) >= (2, 38)
        except (OSError, DeployError):
            pass
    record("glibc >= 2.38", libc_ok, f"found {libc_version}")

    for name in REQUIRED_COMMANDS:
        location = shutil.which(name)
        record(f"command: {name}", location is not None, location or "missing")

    dpkg_available = shutil.which("dpkg-query") is not None
    record("dpkg-query", dpkg_available, shutil.which("dpkg-query") or "missing")
    missing_packages = set(missing_apt_packages())
    for package in APT_PACKAGES:
        installed = dpkg_available and package not in missing_packages
        record(f"package: {package}", installed, "installed" if installed else "missing")

    venv_ok = shutil.which("python3") is not None and command_succeeds_quietly(
        ["python3", "-m", "venv", "--help"]
    )
    record("Python venv support", venv_ok, "available" if venv_ok else "not available")

    sudo_ok = shutil.which("sudo") is not None and command_succeeds_quietly(["sudo", "-n", "true"])
    record("passwordless sudo", sudo_ok, "available" if sudo_ok else "not available")

    kvm_read = sudo_ok and command_succeeds_quietly(["sudo", "-n", "test", "-r", "/dev/kvm"])
    kvm_write = sudo_ok and command_succeeds_quietly(["sudo", "-n", "test", "-w", "/dev/kvm"])
    record("root can read /dev/kvm", kvm_read, "yes" if kvm_read else "no")
    record("root can write /dev/kvm", kvm_write, "yes" if kvm_write else "no")

    docker_ok = shutil.which("docker") is not None and sudo_ok and command_succeeds_quietly(
        ["sudo", "-n", "docker", "info"]
    )
    record("Docker daemon", docker_ok, "available" if docker_ok else "not available")

    gh_ok = shutil.which("gh") is not None and command_succeeds_quietly(["gh", "auth", "status"])
    record("GitHub CLI authentication", gh_ok, "authenticated" if gh_ok else "not authenticated")

    failures = [(name, detail) for name, passed, detail in results if not passed]
    print("\n=== CHECK SUMMARY ===")
    if failures:
        print("Result: FAILED")
        print(f"Passed: {len(results) - len(failures)}")
        print(f"Failed: {len(failures)}")
        print("Failed items:")
        for name, detail in failures:
            print(f"  - {name}: {detail}")
        print(f"Overall: FAILED - {len(failures)} check(s) failed.")
        raise DeployError("")

    print("Result: PASSED")
    print(f"Passed: {len(results)}")
    print("Failed: 0")
    print(f"Overall: PASSED - all {len(results)} checks passed.")


def install_missing_dependencies() -> None:
    require(platform.system() == "Linux", "Linux is required")
    require(shutil.which("apt-get") is not None, "apt-get is required")
    require(shutil.which("sudo") is not None, "sudo is required")
    missing = missing_apt_packages()
    if not missing:
        print("All documented apt packages are already installed.")
        return
    print("Installing missing documented apt packages:")
    for package in missing:
        print(f"  - {package}")
    run(["sudo", "-n", "true"])
    run(["sudo", "-n", "apt-get", "update"])
    run(["sudo", "-n", "apt-get", "install", "-y", *missing])
    print("Dependency installation completed.")


def validate_release(download_dir: Path, install_dir: Path) -> None:
    archives = sorted(download_dir.glob("*.tar.gz"))
    require(len(archives) == 7, f"expected 7 archives, found {len(archives)}")
    require((download_dir / "SHA256SUMS").is_file(), "SHA256SUMS is missing")
    run(["sha256sum", "--quiet", "-c", "SHA256SUMS"], cwd=download_dir)

    for archive in archives:
        run(["tar", "-xzf", str(archive), "-C", str(install_dir)])

    for name in ("bin", "deploy", "docs", "test"):
        require((install_dir / name).is_dir(), f"release directory is missing: {name}")

    for executable in ("cache-ctl", "cloud-hypervisor"):
        binary = install_dir / "bin" / executable
        require(binary.is_file(), f"release binary is missing: {binary}")
        result = run(["ldd", str(binary)], capture=True)
        require("not found" not in result.stdout, f"unresolved shared library in {binary}")


def prepare_python_environment(install_dir: Path) -> None:
    venv = install_dir / ".venv"
    if venv.exists():
        print(f"Python verification environment already exists: {venv}")
        return
    run(["python3", "-m", "venv", str(venv)])
    run([str(venv / "bin" / "python"), "-m", "pip", "install", "e2b", "e2b-code-interpreter"])


def prepare_release(args: argparse.Namespace) -> Path:
    install_missing_dependencies()
    check_environment()
    root = args.root.resolve()
    download_dir = root / f"kuasar-download-{args.version}"
    install_dir = root / f"kuasar-{args.version}"
    require(not download_dir.exists(), f"download directory already exists: {download_dir}")
    require(not install_dir.exists(), f"install directory already exists: {install_dir}")
    download_dir.mkdir(parents=True)
    install_dir.mkdir(parents=True)

    run(
        [
            "gh",
            "release",
            "download",
            args.version,
            "--repo",
            args.repository,
            "--dir",
            str(download_dir),
        ]
    )
    validate_release(download_dir, install_dir)
    prepare_python_environment(install_dir)
    print(f"Release prepared at {install_dir}")
    return install_dir


def release_dir(args: argparse.Namespace) -> Path:
    path = args.release_dir.resolve()
    for name in ("bin", "deploy", "docs", "test"):
        require((path / name).is_dir(), f"invalid release directory; missing {name}: {path}")
    return path


def runtime_environment(path: Path) -> dict[str, str]:
    data_dir = path / ".kuasar-quickstart"
    venv = path / ".venv"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{venv / 'bin'}:{path / 'bin'}:{env.get('PATH', '')}",
            "DEMO_DATA_DIR": str(data_dir),
            "REGISTRY": "127.0.0.1:5000",
            "REGISTRY_INSECURE": "1",
        }
    )
    return env


def deploy(args: argparse.Namespace) -> None:
    path = release_dir(args)
    registry = subprocess.run(
        ["sudo", "-n", "docker", "inspect", "kuasar-quickstart-registry"],
        text=True,
        capture_output=True,
    )
    if registry.returncode != 0:
        run(
            [
                "sudo",
                "-n",
                "docker",
                "run",
                "-d",
                "--name",
                "kuasar-quickstart-registry",
                "--network",
                "host",
                "-e",
                "REGISTRY_HTTP_ADDR=127.0.0.1:5000",
                "registry:2",
            ]
        )

    env = runtime_environment(path)
    sudo_env = ["sudo", "-n", "env"] + [f"{key}={env[key]}" for key in ("PATH", "DEMO_DATA_DIR", "REGISTRY", "REGISTRY_INSECURE")]
    run(sudo_env + ["bash", "test/demo/demo_prep.sh"], cwd=path)
    print("Deployment preparation completed.")


def verify(args: argparse.Namespace) -> None:
    path = release_dir(args)
    env = runtime_environment(path)
    env["DEMO_QUICKSTART"] = "1"
    env["DEMO_UNIT_PREFIX"] = args.unit_prefix
    keys = ["PATH", "DEMO_DATA_DIR", "DEMO_QUICKSTART", "DEMO_UNIT_PREFIX"]
    if args.keep_logs:
        env["DEMO_KEEP"] = "1"
        keys.append("DEMO_KEEP")
    sudo_env = ["sudo", "-n", "env"] + [f"{key}={env[key]}" for key in keys]
    run(sudo_env + ["bash", "test/demo/demo_e2b.sh"], cwd=path)
    print("Kuasar MicroVM verification passed.")


def stop(args: argparse.Namespace) -> None:
    path = release_dir(args)
    env = runtime_environment(path)
    run(
        ["sudo", "-n", "env", f"DEMO_DATA_DIR={env['DEMO_DATA_DIR']}", "bash", "test/demo/demo_prep.sh", "stop"],
        cwd=path,
    )
    subprocess.run(
        ["sudo", "-n", "docker", "rm", "-f", "kuasar-quickstart-registry"],
        check=False,
    )
    print("Kuasar demo services stopped.")


def quick_start(args: argparse.Namespace) -> None:
    install_dir = prepare_release(args)
    runtime_args = argparse.Namespace(
        release_dir=install_dir,
        unit_prefix=args.unit_prefix,
        keep_logs=args.keep_logs,
    )
    deploy(runtime_args)
    verify(runtime_args)
    print("Quick start completed successfully.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal Kuasar Sandbox single-node deployer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="validate the host environment")
    prepare = subparsers.add_parser("prepare", help="download, verify, and extract a release")
    prepare.add_argument("--version", default=DEFAULT_VERSION)
    prepare.add_argument("--repository", default=DEFAULT_REPOSITORY)
    prepare.add_argument("--root", type=Path, default=Path.cwd())

    quick_start_parser = subparsers.add_parser(
        "quick-start", help="check, prepare, deploy, and verify in one command"
    )
    quick_start_parser.add_argument("--version", default=DEFAULT_VERSION)
    quick_start_parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    quick_start_parser.add_argument("--root", type=Path, default=Path.cwd())
    quick_start_parser.add_argument("--unit-prefix", default="kuasar-demo")
    quick_start_parser.add_argument("--keep-logs", action="store_true")

    for command in ("deploy", "stop"):
        item = subparsers.add_parser(command)
        item.add_argument("--release-dir", type=Path, required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--release-dir", type=Path, required=True)
    verify_parser.add_argument("--unit-prefix", default="kuasar-demo")
    verify_parser.add_argument("--keep-logs", action="store_true")

    return parser


def requested_release(args: argparse.Namespace) -> str:
    if hasattr(args, "version"):
        return args.version
    if hasattr(args, "release_dir"):
        return str(args.release_dir.resolve())
    return "host environment"


def print_result(args: argparse.Namespace, succeeded: bool, reason: str | None = None) -> None:
    stream = sys.stdout if succeeded else sys.stderr
    print("\n=== KUASAR DEPLOYMENT RESULT ===", file=stream)
    print(f"Command: {args.command}", file=stream)
    print(f"Release: {requested_release(args)}", file=stream)
    print(f"Result: {'SUCCEEDED' if succeeded else 'FAILED'}", file=stream)
    if reason:
        print(f"Reason: {reason}", file=stream)


def main() -> int:
    args = build_parser().parse_args()
    try:
        require(
            not hasattr(os, "geteuid") or os.geteuid() != 0,
            "run this tool as a regular user; it uses passwordless sudo internally",
        )
        if args.command == "check":
            check_environment()
        elif args.command == "prepare":
            prepare_release(args)
        elif args.command == "quick-start":
            quick_start(args)
        elif args.command == "deploy":
            deploy(args)
        elif args.command == "verify":
            verify(args)
        elif args.command == "stop":
            stop(args)
        print_result(args, True)
        return 0
    except (DeployError, subprocess.CalledProcessError) as exc:
        reason = str(exc) or "one or more required checks failed"
        print_result(args, False, reason)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
