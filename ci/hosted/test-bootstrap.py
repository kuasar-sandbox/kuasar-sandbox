#!/usr/bin/env python3
"""Offline bootstrap tests: no installs, downloads, VM changes or native builds."""
import os
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
            "artifact-prepare": {"python3-pip"},
            "artifact-arm": {"iproute2", "python3-venv", "redis-server", "systemd", "strace"},
            "artifact-x86": {"iproute2", "kmod", "acl", "e2fsprogs", "redis-server", "systemd", "strace"},
        }
        compilers = {"build-essential", "gcc", "g++", "clang", "rustc", "cargo", "golang", "libclang-dev"}
        for profile, expected in required.items():
            result = shell('select_profile "$PROFILE"; printf "%s\\n" "${packages[@]}"', PROFILE=profile)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLessEqual(expected, set(result.stdout.splitlines()))
            self.assertFalse(compilers & set(result.stdout.splitlines()))
            self.assertNotIn("systemd-container", result.stdout)
            flags = shell('select_profile "$PROFILE"; echo "$with_vm"', PROFILE=profile)
            self.assertEqual(flags.returncode, 0, flags.stderr)
            self.assertEqual(flags.stdout.strip(), "true" if profile == "artifact-x86" else "false")
        for obsolete in ("helper-build", "kernel", "runtime", "runtime-publish", "source", "exact-assets",
                         "artifact-build", "artifact-cross", "typo"):
            result = shell('select_profile "$PROFILE"', PROFILE=obsolete)
            self.assertNotEqual(result.returncode, 0, obsolete)
            self.assertIn("unknown profile", result.stderr)

    def test_prepare_has_wheel_installer_without_a_compiler(self):
        result = shell('select_profile artifact-prepare; printf "%s\\n" "${packages[@]}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("python3-pip", result.stdout.splitlines())
        self.assertNotIn("build-essential", result.stdout.splitlines())

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
            body += 'sudo() { echo INSTALL_BOUNDARY; exit 79; }; main --profile artifact-x86'
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
        self.assertIn('test/e2e/e2e', executor)
        self.assertNotIn('requires_privilege', executor)

    def test_required_tools_fail_closed(self):
        self.assertNotEqual(shell("need kuasar_nonexistent_required_tool").returncode, 0)
        self.assertNotEqual(shell("main").returncode, 0)

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
        for name in ("KUASAR_SOURCE_CACHE_ROOT", "KUASAR_NATIVE_CACHE_ROOT", "KUASAR_TARBALL_CACHE"):
            self.assertIn(f'emit {name} "$KUASAR_HOSTED_ROOT/', source)
        self.assertIn('mktemp -d "$RUNNER_TEMP/kuasar-hosted.XXXXXX"', source)
        self.assertIn("sudo -n modprobe tun vhost_vsock", source)
        self.assertIn("vm.unprivileged_userfaultfd=1", source)
        self.assertIn("userfaultfd is required", source)
        self.assertNotIn("chmod 666", "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#")))


if __name__ == "__main__":
    unittest.main()
