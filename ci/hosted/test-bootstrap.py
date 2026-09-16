#!/usr/bin/env python3
"""Offline bootstrap tests: no apt, downloads, VM changes or native builds."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

BOOTSTRAP = Path(__file__).with_name("bootstrap.sh")


def shell(body, **env):
    return subprocess.run(["bash", "-c", '. "$1"; ' + body, "test-bootstrap", str(BOOTSTRAP)],
                          env=dict(os.environ, **env), text=True, capture_output=True)


class BootstrapTests(unittest.TestCase):
    def test_profiles(self):
        required = {
            "control": {"curl", "git", "jq", "python3", "python3-yaml", "util-linux"},
            "release-control": {"curl", "git", "jq"},
            "kernel": {"build-essential", "bc", "bison", "flex", "libelf-dev", "libssl-dev", "libncurses-dev", "pkg-config", "time"},
            "runtime": {"autoconf", "automake", "libtool", "uuid-dev", "liblz4-dev", "libzstd-dev", "zlib1g-dev", "libfuse3-dev"},
            "runtime-publish": {"autoconf", "automake", "libtool", "uuid-dev"},
            "source": {"cmake", "clang", "libclang-dev", "libsnappy-dev", "iproute2", "kmod", "acl", "e2fsprogs", "redis-server"},
        }
        for profile, expected in required.items():
            result = shell('select_profile "$PROFILE"; printf "%s\\n" "${packages[@]}"', PROFILE=profile)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLessEqual(expected, set(result.stdout.splitlines()))
            self.assertNotIn("systemd-container", result.stdout)
            flags = shell('select_profile "$PROFILE"; echo "$with_go $with_kernel $with_readers $with_vm"', PROFILE=profile)
            self.assertEqual(flags.returncode, 0, flags.stderr)
            self.assertEqual(flags.stdout.split()[-1], "true" if profile == "source" else "false")
            if profile in ("runtime", "runtime-publish", "source"):
                self.assertEqual(flags.stdout.split()[2], "true")
            if profile not in ("control",):
                self.assertEqual(flags.stdout.split()[0], "true")
        self.assertNotEqual(shell("select_profile typo").returncode, 0)

    def test_required_tools_fail_closed(self):
        self.assertNotEqual(shell("need kuasar_nonexistent_required_tool").returncode, 0)
        self.assertNotEqual(shell("main").returncode, 0)

    def test_go_pin_and_corruption_before_extraction(self):
        pins = shell('printf "%s %s\\n" "$GO_VERSION" "$GO_SHA256"')
        self.assertEqual(pins.stdout.strip(), "1.26.5 5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053")
        with tempfile.TemporaryDirectory() as directory:
            env = dict(KUASAR_HOSTED_ROOT=directory)
            result = shell('curl() { printf corrupt > "${@: -1}"; }; '
                           'tar() { touch "$KUASAR_HOSTED_ROOT/extracted"; }; install_go', **env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("checksum mismatch", result.stderr)
            self.assertFalse((Path(directory) / "extracted").exists())
            result = shell('download https://example.invalid/archive unused invalid', **env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SHA256 pin", result.stderr)
            result = shell('verify_sha256 /dev/null e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_resource_budget(self):
        result = shell("resource_budget")
        self.assertEqual(result.returncode, 0, result.stderr)
        jobs, cpus = result.stdout.split()
        chosen = set(map(int, cpus.split(",")))
        self.assertEqual(int(jobs), len(chosen))
        self.assertTrue(chosen <= os.sched_getaffinity(0))
        self.assertGreaterEqual(int(jobs), 1)

    def test_host_erofs_writer_and_readers_are_installed(self):
        source = BOOTSTRAP.read_text()
        for tool in ("mkfs", "fsck", "dump"):
            self.assertIn(f'make -C {tool} -j"$KUASAR_BUILD_JOBS"', source)
            self.assertIn(f"{tool}/{tool}.erofs", source)

    def test_ephemeral_paths(self):
        source = BOOTSTRAP.read_text()
        self.assertNotIn("/var/cache/kuasar", source)
        self.assertNotIn("/var/lib/kuasar", source)
        for name in ("KUASAR_SOURCE_CACHE_ROOT", "KUASAR_NATIVE_CACHE_ROOT", "KUASAR_TARBALL_CACHE", "KUASAR_GH_CLI_CACHE", "GOCACHE", "GOMODCACHE", "CARGO_HOME"):
            self.assertIn(f'emit {name} "$KUASAR_HOSTED_ROOT/', source)
        self.assertIn('mktemp -d "$RUNNER_TEMP/kuasar-hosted.XXXXXX"', source)
        self.assertIn("sudo -n modprobe tun vhost_vsock", source)
        self.assertIn("vm.unprivileged_userfaultfd=1", source)
        self.assertIn("userfaultfd is required", source)
        self.assertNotIn("chmod 666", "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#")))


if __name__ == "__main__":
    unittest.main()
