#!/usr/bin/env python3
"""Offline bootstrap tests: no installs, downloads, VM changes or native builds."""
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest

BOOTSTRAP = Path(__file__).with_name("bootstrap.sh")
HOSTED_VM = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
             "RUNNER_OS": "Linux", "RUNNER_ARCH": "X64"}


def shell(body, **env):
    return subprocess.run(["bash", "-c", '. "$1"; ' + body, "test-bootstrap", str(BOOTSTRAP)],
                          env=dict(os.environ, **env), text=True, capture_output=True)


class BootstrapTests(unittest.TestCase):
    def test_profiles(self):
        required = {
            "control": {"curl", "git", "jq", "python3", "python3-yaml", "util-linux"},
            "release-control": {"curl", "git", "jq"},
            "kernel": {"build-essential", "bc", "bison", "flex", "libelf-dev", "libssl-dev", "libncurses-dev", "pkg-config", "time"},
            "runtime": {"autoconf", "automake", "libtool", "patch", "uuid-dev", "libgcrypt20-dev", "libgpg-error-dev", "libssl-dev", "liblz4-dev", "libzstd-dev", "zlib1g-dev", "libfuse3-dev"},
            "runtime-publish": {"autoconf", "automake", "libtool", "patch", "uuid-dev", "libgcrypt20-dev", "libgpg-error-dev", "libssl-dev"},
            "source": {"cmake", "clang", "libclang-dev", "libsnappy-dev", "iproute2", "kmod", "acl", "e2fsprogs", "redis-server"},
            "exact-assets": {"build-essential", "autoconf", "automake", "libtool", "uuid-dev", "iproute2", "kmod", "acl", "e2fsprogs", "redis-server"},
        }
        for profile, expected in required.items():
            result = shell('select_profile "$PROFILE"; printf "%s\\n" "${packages[@]}"', PROFILE=profile)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLessEqual(expected, set(result.stdout.splitlines()))
            self.assertNotIn("systemd-container", result.stdout)
            flags = shell('select_profile "$PROFILE"; echo "$with_go $with_kernel $with_readers $with_vm"', PROFILE=profile)
            self.assertEqual(flags.returncode, 0, flags.stderr)
            self.assertEqual(flags.stdout.split()[-1], "true" if profile in ("source", "exact-assets") else "false")
            if profile in ("runtime", "runtime-publish", "source", "exact-assets"):
                self.assertEqual(flags.stdout.split()[2], "true")
            if profile not in ("control",):
                self.assertEqual(flags.stdout.split()[0], "true")
        self.assertNotEqual(shell("select_profile typo").returncode, 0)

    def test_full_suite_utilities_and_exact_assets_build_scope(self):
        for profile in ("source", "exact-assets"):
            result = shell('select_profile "$PROFILE"; printf "%s\\n" "${packages[@]}"', PROFILE=profile)
            self.assertEqual(result.returncode, 0, result.stderr)
            packages = set(result.stdout.splitlines())
            self.assertLessEqual({"systemd", "dbus", "iputils-ping", "netcat-openbsd", "openssl",
                                  "sqlite3", "zip", "socat", "strace", "udev"}, packages)
            if profile == "exact-assets":
                self.assertFalse(packages & {"cmake", "clang", "libclang-dev", "libsnappy-dev",
                                             "bison", "flex", "libncurses-dev", "libelf-dev"})
        result = shell('select_profile exact-assets; echo "$with_go $with_native $with_kernel $with_readers"')
        self.assertEqual(result.stdout.strip(), "true false false true")

    def test_kvm_rule_is_exact_and_rejects_unsafe_ids(self):
        for gid in ("1", "1001", "4294967294"):
            result = shell('render_kvm_rule 1001 "$JOB_GID"', JOB_GID=gid, **HOSTED_VM)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout,
                             f'SUBSYSTEM=="misc", KERNEL=="kvm", GROUP:="{gid}", MODE:="0660"\n')
        for value in ("", "0", "-1", "+1001", "01001", "4294967295", "99999999999",
                      "runner", "1001\n", '1001", MODE:="0666', "1001; false"):
            for args in ('1001 "$BAD_ID"', '"$BAD_ID" 1001'):
                result = shell("render_kvm_rule " + args, BAD_ID=value, **HOSTED_VM)
                self.assertNotEqual(result.returncode, 0, (value, args))
                self.assertIn("non-root numeric job uid/gid", result.stderr)
                self.assertEqual(result.stdout, "")
        for args in ("", "1001", "1001 1001 extra"):
            result = shell("render_kvm_rule " + args, **HOSTED_VM)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires job uid and primary gid", result.stderr)

    def test_kvm_rule_requires_expected_hosted_environment(self):
        for name, invalid in (("GITHUB_ACTIONS", "false"), ("RUNNER_ENVIRONMENT", "self-hosted"),
                              ("RUNNER_OS", "Windows"), ("RUNNER_ARCH", "ARM64")):
            for value in (invalid, ""):
                result = shell("render_kvm_rule 1001 1001", **{**HOSTED_VM, name: value})
                self.assertNotEqual(result.returncode, 0, (name, value))
                self.assertIn("disposable GitHub-hosted Linux x64 job", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_ubuntu_version_does_not_gate_host_initialization(self):
        # Exercise main only up to a mocked installation boundary; no host changes.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "os-release"
            script = root / "bootstrap.sh"
            script.write_text(BOOTSTRAP.read_text().replace(". /etc/os-release", '. "' + str(release) + '"'))
            env = {**os.environ, **HOSTED_VM, "RUNNER_TEMP": directory,
                   "GITHUB_ENV": str(root / "env"), "GITHUB_PATH": str(root / "path")}
            body = '. "$1"; need() { :; }; id() { echo 1001; }; '
            body += 'uname() { case "$1" in -m) echo x86_64;; -s) echo Linux;; esac; }; '
            body += 'sudo() { echo INSTALL_BOUNDARY; exit 79; }; main --profile source'
            for distro, version in (("ubuntu", "24.04"), ("ubuntu", "26.04"),
                                    ("ubuntu", "99.99"), ("debian", "13")):
                release.write_text(f'ID={distro}\nVERSION_ID={version}\n')
                result = subprocess.run(["bash", "-c", body, "test-host", str(script)],
                                        env={**env, "ImageOS": "future-image"}, text=True, capture_output=True)
                if distro == "ubuntu":
                    self.assertEqual(result.returncode, 79, result.stderr)
                    self.assertIn("INSTALL_BOUNDARY", result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("INSTALL_BOUNDARY", result.stdout)
                    self.assertIn("Ubuntu Linux is required", result.stderr)

    def test_missing_docker_capability_fails_before_device_changes(self):
        result = shell('need() { :; }; id() { echo 1001; }; docker() { return 23; }; '
                       'sudo() { echo UNEXPECTED_DEVICE_CHANGE; }; configure_vm', **HOSTED_VM)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertNotIn("UNEXPECTED_DEVICE_CHANGE", result.stdout)

    def test_cross_apt_preserves_comment_and_empty_paragraphs(self):
        archive = ("Types: deb\nURIs: http://archive.example.invalid/ubuntu\n"
                   "Suites: noble noble-updates\nComponents: main universe\n"
                   "Architectures: amd64 arm64\n")
        security = ("Types: deb\nURIs: http://security.example.invalid/ubuntu\n"
                    "Suites: noble-security\nComponents: main universe\n")
        for comments in (False, True):
            with self.subTest(comments=comments), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                sources = root / "sources"
                sources.mkdir()
                native = sources / "ubuntu.sources"
                arm = sources / "kuasar-arm64.sources"
                native.write_text(
                    "# Ubuntu archive configuration\n# Comments are not sources.\n\n"
                    + archive + "\n\n\n# Security archive\n\n" + security
                    + "\n# End of configuration\n" if comments else archive + "\n" + security)
                script = root / "bootstrap.sh"
                script.write_text(BOOTSTRAP.read_text()
                    .replace("/etc/apt/sources.list.d/ubuntu.sources", str(native))
                    .replace("/etc/apt/sources.list.d/kuasar-arm64.sources", str(arm)))
                # Execute the real setup with every privileged write redirected
                # to this fixture. Registering a foreign architecture is mocked.
                body = '''. "$1"
sudo() {
    [ "$1" = -n ] || return 91
    shift
    case "$1" in
        python3|tee) "$@" ;;
        dpkg) [ "$*" = 'dpkg --add-architecture arm64' ] ;;
        *) return 92 ;;
    esac
}
configure_cross_apt
'''
                result = subprocess.run(["bash", "-c", body, "test-cross", str(script)],
                    env={**os.environ, **HOSTED_VM, "VERSION_CODENAME": "noble"},
                    text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                # indextargets parses configured sources without downloading or
                # installing anything; state/cache/source paths are isolated.
                parsed = subprocess.run(["apt-get", "indextargets",
                    "-o", "Dir::Etc::sourcelist=/dev/null",
                    "-o", f"Dir::Etc::sourceparts={sources}",
                    "-o", f"Dir::State={root / 'state'}",
                    "-o", f"Dir::Cache={root / 'cache'}"],
                    text=True, capture_output=True, timeout=10)
                self.assertEqual(parsed.returncode, 0, parsed.stderr)
                self.assertEqual(native.read_text().count("Architectures: amd64"), 2)
                self.assertNotIn("Architectures: amd64 arm64", native.read_text())
                self.assertIn("Architectures: arm64\n", arm.read_text())
                self.assertIn("URIs: http://ports.ubuntu.com/ubuntu-ports\n", arm.read_text())
                if comments:
                    self.assertIn("# Comments are not sources.\n\nTypes: deb", native.read_text())

    def test_kvm_configuration_is_scoped_and_replays_acl_loss(self):
        source = BOOTSTRAP.read_text()
        vm = source.split("configure_vm() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('job_gid=$(id -g)', vm)
        self.assertIn('job_uid=$(id -u)', vm)
        ordered = ('render_kvm_rule "$job_uid" "$job_gid"',
                   'sudo -n setfacl -m "u:$job_uid:rw" "$device"',
                   '/etc/udev/rules.d/99-kuasar-job-kvm.rules',
                   'sudo -n udevadm control --reload-rules',
                   'sudo -n udevadm trigger --action=change --subsystem-match=misc --sysname-match=kvm',
                   'sudo -n udevadm settle --timeout=30',
                   'sudo -n chgrp "$job_gid" /dev/kvm',
                   'sudo -n chmod 0660 /dev/kvm',
                   'sudo -n setfacl -x "u:$job_uid" /dev/kvm',
                   "fd = os.open('/dev/kvm', os.O_RDWR | os.O_CLOEXEC)")
        positions = [vm.index(text) for text in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(positions[0], vm.index("sudo -n"))
        self.assertIn('for device in /dev/kvm /dev/vhost-vsock /dev/net/tun; do', vm)
        self.assertIn("    python3 - <<'PY'\nimport os\n\nfd = os.open('/dev/kvm'", vm)
        for command in ("usermod", "groupmod", "groupadd", "chmod 666", "chmod 0666"):
            self.assertNotIn(command, vm)
        main = source.split("main() {", 1)[1]
        self.assertLess(main.index('render_kvm_rule "$(id -u)" "$(id -g)"'), main.index("sudo -n apt-get"))

    def test_real_network_gate_has_no_redundant_host_probe(self):
        source = BOOTSTRAP.read_text()
        self.assertNotIn("configure_exact_network", source)
        self.assertNotIn("linux-tools-common", source)
        executor = (BOOTSTRAP.resolve().parents[2] / "ci/integration/run-artifact-tests.py").read_text()
        self.assertIn('"CONNECTOR_E2E"', executor)

    def test_required_tools_fail_closed(self):
        self.assertNotEqual(shell("need kuasar_nonexistent_required_tool").returncode, 0)
        self.assertNotEqual(shell("main").returncode, 0)

    def test_environment_go_preserves_selection_without_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / "environment tools"
            tools.mkdir()
            go = tools / "go"
            go.write_text("#!/bin/sh\nprintf 'environment Go %s\\n' \"$GOTOOLCHAIN\"\n")
            go.chmod(0o755)
            for policy in (None, "local", "auto", "go1.99.1+path"):
                env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"],
                           GITHUB_ENV=str(root / "github-env"), KUASAR_HOSTED_ROOT=str(root),
                           GOROOT=str(root / "selected root"))
                if policy is None:
                    env.pop("GOTOOLCHAIN", None)
                else:
                    env["GOTOOLCHAIN"] = policy
                before = go.read_bytes()
                command = ('. "$1"; download() { exit 91; }; tar() { exit 92; }; '
                           'before_root=$GOROOT; before_policy=${GOTOOLCHAIN-unset}; '
                           'configure_go; [ "$GOROOT" = "$before_root" ]; '
                           '[ "${GOTOOLCHAIN-unset}" = "$before_policy" ]')
                result = subprocess.run(["bash", "-c", command, "test", str(BOOTSTRAP)],
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("environment Go", result.stdout)
                self.assertEqual(go.read_bytes(), before)
                entries = (root / "github-env").read_text()
                self.assertNotIn("GOROOT=", entries)
                self.assertNotIn("GOTOOLCHAIN=", entries)

    def test_missing_environment_go_fails_without_installing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dirname").symlink_to(shutil.which("dirname"))
            result = subprocess.run([shutil.which("bash"), "-c", '. "$1"; configure_go',
                                     "test", str(BOOTSTRAP)],
                                    env=dict(os.environ, PATH=directory),
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing required tool: go", result.stderr)

    def test_download_integrity_is_still_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(KUASAR_HOSTED_ROOT=directory)
            result = shell('download https://example.invalid/archive unused invalid', **env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SHA256 pin", result.stderr)
            result = shell('verify_sha256 /dev/null e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
            self.assertEqual(result.returncode, 0, result.stderr)
            result = shell('curl() { printf corrupt > "${@: -1}"; }; '
                           'download https://example.invalid/archive "$KUASAR_HOSTED_ROOT/archive" '
                           'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', **env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("checksum mismatch", result.stderr)

    def test_rust_tools_do_not_require_a_matching_rustup(self):
        # Stop at the existing cgroup check, before any privileged operations.
        result = shell('profile=source; id() { echo 1001; }; '
                       'docker() { :; }; systemctl() { :; }; ip() { :; }; '
                       'modprobe() { :; }; setfacl() { :; }; mkfs.ext4() { :; }; '
                       'udevadm() { :; }; cargo() { echo environment-cargo; }; '
                       'rustc() { echo environment-rustc; }; '
                       'rustup() { echo UNRELATED_RUSTUP; return 89; }; '
                       'stat() { echo stop-before-device-changes; }; configure_vm', **HOSTED_VM)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("environment-cargo", result.stdout)
        self.assertIn("environment-rustc", result.stdout)
        self.assertNotIn("UNRELATED_RUSTUP", result.stdout)
        self.assertIn("cgroup v2 is required", result.stderr)

    def test_resource_budget(self):
        result = shell("resource_budget")
        self.assertEqual(result.returncode, 0, result.stderr)
        jobs, cpus = result.stdout.split()
        chosen = set(map(int, cpus.split(",")))
        self.assertEqual(int(jobs), len(chosen))
        self.assertTrue(chosen <= os.sched_getaffinity(0))
        self.assertGreaterEqual(int(jobs), 1)

    def test_resource_budget_respects_single_cpu_affinity(self):
        # Restrict only this test child; exercise the actual budget calculation.
        cpu = min(os.sched_getaffinity(0))
        result = shell('taskset -pc "$TEST_CPU" "$$" >/dev/null; resource_budget', TEST_CPU=str(cpu))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"1 {cpu}")

    def test_host_erofs_writer_and_readers_are_installed(self):
        source = BOOTSTRAP.read_text()
        for tool in ("mkfs", "fsck", "dump"):
            self.assertIn(f'make -C {tool} -j"$KUASAR_BUILD_JOBS"', source)
            self.assertIn(f"{tool}/{tool}.erofs", source)

    def test_ephemeral_paths(self):
        source = BOOTSTRAP.read_text()
        self.assertNotIn("/var/cache/kuasar", source)
        self.assertNotIn("/var/lib/kuasar", source)
        for name in ("KUASAR_SOURCE_CACHE_ROOT", "KUASAR_NATIVE_CACHE_ROOT", "KUASAR_TARBALL_CACHE", "GOCACHE", "GOMODCACHE", "CARGO_HOME"):
            self.assertIn(f'emit {name} "$KUASAR_HOSTED_ROOT/', source)
        self.assertIn('mktemp -d "$RUNNER_TEMP/kuasar-hosted.XXXXXX"', source)
        self.assertIn("sudo -n modprobe tun vhost_vsock", source)
        self.assertIn("vm.unprivileged_userfaultfd=1", source)
        self.assertIn("userfaultfd is required", source)
        self.assertNotIn("chmod 666", "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#")))


if __name__ == "__main__":
    unittest.main()
