#!/usr/bin/env python3
"""Offline host-tool fixtures: no APIs, Go builds, apt or VM operations."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

HELPER = Path(__file__).with_name("exact-assets-tools.sh")
COMMIT = "494dbceae683d6b20cdbec00fe6b1f554ea2f508"
ZOT = b"#!/bin/sh\necho fixture-zot\n"


class ExactToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="exact-tools-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runner = self.root / "runner temp"
        self.fixtures = self.root / "fixtures"
        self.mockbin = self.root / "mockbin"
        self.product = self.root / "release-install/bin"
        for path in (self.runner, self.fixtures, self.mockbin, self.product):
            path.mkdir(parents=True)
        for name in ("bash", "dirname", "basename", "python3", "cp", "chmod", "mkdir", "sha256sum", "awk",
                     "mv", "tar", "gzip", "sort", "install", "mktemp", "rm", "rmdir", "cut", "taskset", "touch", "readlink"):
            (self.mockbin / name).symlink_to(shutil.which(name))
        for name in ("vmlinux", "cloud-hypervisor", "sandbox-ctl", "mkfs.erofs", "zot", "versitygw"):
            (self.product / name).write_bytes(b"packaged product: " + name.encode())
        self.before = self.product_snapshot()
        (self.fixtures / "common.sh").write_text("# fixture only\n")
        (self.fixtures / "build-versitygw.sh").write_text('''#!/usr/bin/env bash
set -euo pipefail
[ "${FAIL_RECIPE:-0}" != 1 ] || exit 41
python3 - <<'PY'
import json, os
from pathlib import Path
keys = ('BINDIR', 'BUILD_DIR', 'TARBALL_CACHE', 'TMPDIR', 'VERSITYGW_SRC',
        'VERSITYGW_TARBALL', 'VERSITYGW_TARBALL_SHA256', 'VERSITYGW_GOFLAGS',
        'GOTOOLCHAIN', 'GOMAXPROCS', 'GO_ARCH')
Path(os.environ['PROBE']).write_text(json.dumps({key: os.environ.get(key) for key in keys}))
target = Path(os.environ['BINDIR']) / 'versitygw'
target.write_text('#!/bin/sh\\necho fixture-versitygw\\n')
target.chmod(0o755)
PY
''')
        self.archive()
        # Exercise the real ensure-zot download/checksum/install path offline.
        curl = self.mockbin / "curl"
        curl.write_text('''#!/usr/bin/env python3
import hashlib, os, sys
from pathlib import Path
args = sys.argv[1:]
url = next(arg for arg in args if arg.startswith('https://'))
Path(os.environ['CURL_LOG']).open('a').write(url + '\\n')
zot = b'#!/bin/sh\\necho fixture-zot\\n'
if url.endswith('/checksums.sha256.txt'):
    data = (hashlib.sha256(zot).hexdigest() + ' *zot-linux-amd64-minimal\\n').encode()
elif url.endswith('/zot-linux-amd64-minimal'):
    data = zot if os.environ.get('CORRUPT_ZOT') != '1' else b'corrupt'
else:
    data = b'corrupt recipe'
Path(args[args.index('-o') + 1]).write_bytes(data)
''')
        curl.chmod(0o755)
        self.envfile = self.root / "github-env"
        self.envfile.touch()
        self.env = dict(os.environ, PATH=str(self.mockbin),
                        RUNNER_TEMP=str(self.runner), GITHUB_ENV=str(self.envfile),
                        KUASAR_CI_DIR=str(self.root / "metrics"), KUASAR_BUILD_JOBS="1",
                        KUASAR_BUILD_CPUS=str(min(os.sched_getaffinity(0))),
                        PROBE=str(self.root / "probe"), FIXTURES=str(self.fixtures),
                        DOWNLOADS=str(self.root / "downloads"), CURL_LOG=str(self.root / "curl"))

        for key in ("E2E_ZOT_BIN", "E2E_VGW_BIN", "ZOT_BIN", "VGW_BIN"):
            self.env.pop(key, None)

    def archive(self, name="versitygw-1.5.0/cmd/versitygw/main.go", symlink=False):
        with tarfile.open(self.fixtures / "versitygw-1.5.0.tar.gz", "w:gz") as archive:
            item = tarfile.TarInfo(name)
            if symlink:
                item.type = tarfile.SYMTYPE
                item.linkname = str(self.product)
                archive.addfile(item)
            else:
                item.size = 7
                archive.addfile(item, io.BytesIO(b"fixture"))

    def product_snapshot(self):
        return {path.name: (path.read_bytes(), path.stat().st_mode) for path in self.product.iterdir()}

    def run_helper(self, *args, body=None, **env):
        if body is None:
            # Only replace transport with deterministic fixtures. Paths, recipe
            # invocation, archive validation, zot installation and outputs are real.
            body = '''download() {
printf '%s\\t%s\\t%s\\n' "$1" "$2" "$3" >> "$DOWNLOADS"
cp "$FIXTURES/${2##*/}" "$2"
}; main "$@"'''
        return subprocess.run(["bash", "-c", 'source "$1"; shift; ' + body,
                               "test-exact-tools", str(HELPER), *args],
                              cwd=self.root, env={**self.env, **env}, text=True,
                              capture_output=True, timeout=10)

    def test_isolated_tools_and_packaged_binaries_unchanged(self):
        result = self.run_helper(BINDIR=str(self.product), BUILD_DIR=str(self.product),
                                 VERSITYGW_SRC=str(self.product), VERSITYGW_TARBALL="untrusted",
                                 ZOT_URL="https://untrusted.invalid/tool",
                                 CANDIDATE_SHA="f" * 40, PLATFORM_SOURCE_ROOT=str(self.product))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.product_snapshot(), self.before)
        outputs = dict(line.split("=", 1) for line in self.envfile.read_text().splitlines())
        self.assertEqual(set(outputs), {"E2E_ZOT_BIN", "E2E_VGW_BIN"})
        for path in outputs.values():
            self.assertTrue(Path(path).is_relative_to(self.runner))
            self.assertTrue(os.access(path, os.X_OK))
        probe = json.loads((self.root / "probe").read_text())
        for name in ("BINDIR", "BUILD_DIR", "TARBALL_CACHE", "TMPDIR", "VERSITYGW_SRC", "VERSITYGW_TARBALL"):
            self.assertTrue(Path(probe[name]).is_relative_to(self.runner), name)
        self.assertEqual(probe["VERSITYGW_GOFLAGS"], "-mod=mod -p=1")
        self.assertEqual(probe["GOTOOLCHAIN"], self.env.get("GOTOOLCHAIN"))
        self.assertEqual(probe["GOMAXPROCS"], "1")
        self.assertEqual(probe["GO_ARCH"], "amd64")
        downloads = [line.split("\t") for line in (self.root / "downloads").read_text().splitlines()]
        self.assertEqual(len(downloads), 3)
        for (url, destination, digest), name in zip(downloads, ("build-versitygw.sh", "common.sh", "versitygw-1.5.0.tar.gz")):
            self.assertEqual(Path(destination).name, name)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            if name.endswith(".sh"):
                self.assertEqual(url, f"https://raw.githubusercontent.com/kuasar-sandbox/guest-runtime/{COMMIT}/native-deps/deps/{name}")
        self.assertIn("/v2.1.17/", (self.root / "curl").read_text())
        identity = (self.root / "metrics/host-tools.tsv").read_text()
        self.assertIn(COMMIT, identity)
        self.assertIn(hashlib.sha256(ZOT).hexdigest(), identity)

    def test_environment_tools_bypass_no_existing_capabilities(self):
        supplied = self.root / "custom tools"
        supplied.mkdir()
        for name in ("zot", "versitygw"):
            tool = supplied / name
            tool.write_text("#!/bin/sh\necho independently-built-tool\n")
            tool.chmod(0o755)
        for explicit in (False, True):
            settings = ({"E2E_ZOT_BIN": str(supplied / "zot"),
                         "E2E_VGW_BIN": str(supplied / "versitygw")} if explicit else
                        {"PATH": str(supplied) + os.pathsep + str(self.mockbin)})
            result = self.run_helper(**settings)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((self.root / "downloads").exists())
            self.assertFalse((self.root / "curl").exists())
            self.assertFalse((self.root / "probe").exists())
            outputs = dict(line.split("=", 1) for line in self.envfile.read_text().splitlines())
            self.assertEqual(outputs, {"E2E_ZOT_BIN": str(supplied / "zot"),
                                       "E2E_VGW_BIN": str(supplied / "versitygw")})
            self.assertEqual(self.product_snapshot(), self.before)

    def test_invalid_explicit_tool_does_not_download_a_replacement(self):
        result = self.run_helper(E2E_ZOT_BIN=str(self.root / "absent"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("environment zot is not executable", result.stderr)
        self.assertFalse((self.root / "downloads").exists())
        self.assertFalse((self.root / "curl").exists())

    def test_fallback_build_preserves_explicit_toolchain_policy(self):
        result = self.run_helper(GOTOOLCHAIN="go1.99.1+path")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root / "probe").read_text())["GOTOOLCHAIN"],
                         "go1.99.1+path")

    def test_bad_arguments_paths_and_budget_reject_before_fetch(self):
        for args, overrides in ((("--bindir", str(self.product)), {}),
                                ((), {"RUNNER_TEMP": "relative"}),
                                ((), {"RUNNER_TEMP": str(self.root / "absent")}),
                                ((), {"KUASAR_BUILD_JOBS": "0"}),
                                ((), {"KUASAR_BUILD_CPUS": "0;false"})):
            result = self.run_helper(*args, **overrides)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertFalse((self.root / "downloads").exists())
        for path in ("../common.sh", "/native-deps/deps/common.sh", "native-deps/deps/build-kernel.sh"):
            result = self.run_helper(path, body='fetch_recipe "$1"')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unapproved guest tool path", result.stderr)
        result = self.run_helper(body='GUEST_TOOLS_COMMIT=main; fetch_recipe native-deps/deps/common.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid guest tool commit", result.stderr)
        self.assertEqual(self.product_snapshot(), self.before)

    def test_corrupt_recipe_fails_before_execution(self):
        result = self.run_helper(body='main "$@"')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertFalse((self.root / "probe").exists())
        self.assertEqual(self.envfile.read_text(), "")

    def test_archive_traversal_and_symlinks_fail_before_recipe(self):
        for name, symlink in (("versitygw-1.5.0/../../escape", False),
                              ("versitygw-1.5.0/link", True)):
            self.archive(name, symlink)
            result = self.run_helper()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsupported entry type" if symlink else "unsafe path", result.stderr)
            self.assertFalse((self.root / "probe").exists())
            self.assertEqual(self.envfile.read_text(), "")
            self.assertEqual(self.product_snapshot(), self.before)

    def test_recipe_failure_and_corrupt_zot_cannot_publish_paths(self):
        for overrides in ({"FAIL_RECIPE": "1"}, {"CORRUPT_ZOT": "1"}):
            result = self.run_helper(**overrides)
            self.assertNotEqual(result.returncode, 0)
            if "FAIL_RECIPE" in overrides:
                self.assertEqual(result.returncode, 41, result.stderr)
            else:
                self.assertIn("sha256 mismatch", result.stderr)
            self.assertEqual(self.envfile.read_text(), "")
            self.assertEqual(self.product_snapshot(), self.before)


if __name__ == "__main__":
    unittest.main()
