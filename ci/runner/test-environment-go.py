#!/usr/bin/env python3
"""Check tool resolution and mount rendering without changing host/runner state."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

PROVISION = Path(__file__).with_name("provision.sh")


class EnvironmentGo(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="runner-env-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.goroot = self.root / "vendor Go root"
        (self.goroot / "bin").mkdir(parents=True)
        self.tools = self.root / "caller bin"
        self.tools.mkdir()
        for name in ("dirname", "readlink"):
            (self.tools / name).symlink_to(shutil.which(name))

    def invoke(self, body, policy=None):
        env = dict(os.environ, PATH=str(self.tools), TEST_GOROOT=str(self.goroot))
        if policy is None:
            env.pop("GOTOOLCHAIN", None)
        else:
            env["GOTOOLCHAIN"] = policy
        return subprocess.run([shutil.which("bash"), "-c", '. "$1"; ' + body,
                               "test", str(PROVISION)], env=env, text=True,
                              capture_output=True, timeout=10)

    def fake_go(self, path, version="independent-vendor-build"):
        path.write_text('#!/bin/sh\n# ' + version + '\n'
                        'test "$1:$2" = env:GOROOT || exit 71\n'
                        'printf "%s\\n" "$TEST_GOROOT"\n')
        path.chmod(0o755)

    def test_arbitrary_environment_installation_and_policy_are_accepted(self):
        driver = self.goroot / "bin/go"
        self.fake_go(driver)
        (self.tools / "go").symlink_to(driver)
        for policy in (None, "auto", "local", "go1.99.1+path"):
            result = self.invoke('curl() { exit 81; }; tar() { exit 82; }; '
                                 'resolve_environment_go; write_go_mounts; '
                                 'printf "policy=%s\\n" "${GOTOOLCHAIN-unset}"', policy)
            self.assertEqual(result.returncode, 0, result.stderr)
            escaped_root = str(self.goroot).replace(" ", r"\ ")
            self.assertIn("BindReadOnly=" + escaped_root, result.stdout)
            self.assertNotIn('"', result.stdout)
            self.assertEqual(result.stdout.count("BindReadOnly="), 2)
            self.assertIn("policy=" + (policy if policy is not None else "unset"), result.stdout)
            self.assertIn("independent-vendor-build", driver.read_text())
        self.fake_go(driver, "second-toolchain-with-different-bytes")
        self.assertEqual(self.invoke('resolve_environment_go').returncode, 0)

    def test_driver_outside_root_gets_individual_mount_not_parent_directory(self):
        self.fake_go(self.tools / "go")
        result = self.invoke('resolve_environment_go; write_go_mounts')
        self.assertEqual(result.returncode, 0, result.stderr)
        escaped_root = str(self.goroot).replace(" ", r"\ ")
        escaped_driver = str(self.tools / "go").replace(" ", r"\ ")
        self.assertEqual(result.stdout.splitlines(),
                         ["BindReadOnly=" + escaped_root,
                          "BindReadOnly=" + escaped_driver + ":" + escaped_driver])

    def test_command_symlink_keeps_its_name_in_the_runner_path(self):
        driver = self.goroot / "bin/vendor-driver"
        self.fake_go(driver)
        (self.tools / "go").symlink_to(driver)
        result = self.invoke('resolve_environment_go; write_go_mounts; printf "%s\\n" "$GO_ENTRY"')
        self.assertEqual(result.returncode, 0, result.stderr)
        escaped_driver = str(driver).replace(" ", r"\ ")
        escaped_entry = str(self.tools / "go").replace(" ", r"\ ")
        self.assertIn(escaped_driver + ":" + escaped_entry, result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], str(self.tools / "go"))

    def test_missing_go_fails_without_downloading(self):
        result = self.invoke('resolve_environment_go')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Go must be supplied on PATH", result.stderr)

    def test_unusable_go_root_is_an_environment_error(self):
        self.fake_go(self.tools / "go")
        result = self.invoke('TEST_GOROOT="$TEST_GOROOT/absent"; resolve_environment_go')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a directory", result.stderr)

    def test_preloaded_environment_tools_are_capability_checked(self):
        for name in ("zot", "versitygw"):
            path = self.root / name
            path.write_text("#!/bin/sh\necho vendor-tool\n")
            path.chmod(0o755)
        gh = self.root / "gh"
        gh.write_text("#!/bin/sh\n"
                      'test "$1:${2-}" = api:--help || exit 71\n'
                      'echo "  --slurp  compatible environment CLI"\n')
        gh.chmod(0o755)
        # Only the required executable capabilities are relevant to these tools.
        body = 'TOOL_ROOT="' + str(self.root) + '"; assert_environment_tools'
        self.assertEqual(self.invoke(body).returncode, 0)
        (self.root / "zot").unlink()
        result = self.invoke(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provide an executable zot", result.stderr)

    def test_incompatible_environment_cli_is_rejected(self):
        for name in ("zot", "versitygw", "gh"):
            path = self.root / name
            path.write_text("#!/bin/sh\necho environment-tool\n")
            path.chmod(0o755)
        body = 'TOOL_ROOT="' + str(self.root) + '"; assert_environment_tools'
        result = self.invoke(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("environment gh must support api --slurp", result.stderr)

    def test_toolchain_replacement_is_not_a_provisioning_action(self):
        source = PROVISION.read_text()
        for token in ("GO_BINARY_SHA256", "GO_TARBALL_URL", "GO_VERSION=",
                      "ensure_go_toolchain", 'rm -rf "$GO_ROOT"',
                      "BindReadOnly=/usr/local/go", "go version go1."):
            self.assertNotIn(token, source)
        self.assertIn('export PATH="$(cat /opt/actions-runner/.path)"', source)
        self.assertIn('ln -sfn "$TOOL_ROOT/gh" "$root/usr/local/bin/gh"', source)


if __name__ == "__main__":
    unittest.main()
