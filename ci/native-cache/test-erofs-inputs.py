#!/usr/bin/env python3
"""Real compiler regressions for cache keys; all modified inputs are private."""
import os
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

CACHE = Path(__file__).with_name("native-cache.sh").resolve()


class InputsTests(unittest.TestCase):
    def test_config_site_defaults_and_explicit_list(self):
        spec = importlib.util.spec_from_file_location("erofs_inputs", CACHE.with_name("erofs-inputs.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        defaults = ["/usr/local/share/config.site", "/usr/local/etc/config.site"]
        self.assertEqual(module.config_site_paths({}), defaults)
        self.assertEqual(module.config_site_paths({"CONFIG_SITE": ""}), defaults)
        self.assertEqual(module.config_site_paths({"CONFIG_SITE": "/a/site /b/site"}), ["/a/site", "/b/site"])

    def test_consumed_inputs_and_source_relocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "first"
            native = workspace / "guest-runtime/native-deps"
            (native / "deps").mkdir(parents=True)
            (native / "Makefile").write_text("EROFS_TARBALL ?= source.tar.gz\nEROFS_TARBALL_SHA256 ?=\n")
            (native / "source.tar.gz").write_bytes(b"source identity fixture\n")
            (native / "deps/build-erofs.sh").write_text("# --without-openssl legacy uuid recipe\n")
            (native / "deps/common.sh").write_text("# source fixture\n")
            env = dict(os.environ, KUASAR_WORKSPACE_ROOT=str(workspace), CROSS_PREFIX="", TARGET_ARCH="x86_64")
            for name in ("EROFS_TARBALL", "EROFS_TARBALL_SHA256", "CC", "CFLAGS", "CPPFLAGS", "LDFLAGS", "LIBS", "PKG_CONFIG_SYSROOT_DIR", "PKG_CONFIG_LIBDIR"):
                env.pop(name, None)

            def key(**changes):
                return subprocess.check_output([str(CACHE), "key", "erofs"], env=dict(env, **changes), text=True).strip()

            original = key()
            relocated = root / "relocated"
            shutil.copytree(workspace, relocated)
            self.assertEqual(original, key(KUASAR_WORKSPACE_ROOT=str(relocated)))
            copied = root / "renamed.tar.gz"
            shutil.copy2(native / "source.tar.gz", copied)
            self.assertEqual(original, key(EROFS_TARBALL=str(copied)))
            copied.write_bytes(b"changed bytes at the same local source path\n")
            self.assertNotEqual(original, key(EROFS_TARBALL=str(copied)))
            # The actual -L-selected uuid archive must beat pkg-config's libdir.
            selected = root / "selected"
            selected.mkdir()
            library = subprocess.check_output(["gcc", "-print-file-name=libuuid.a"], text=True).strip()
            self.assertTrue(Path(library).is_file())
            shutil.copy2(library, selected / "libuuid.a")
            env["LDFLAGS"] = "-L" + str(selected)
            before = key()
            (root / "extra.c").write_text("int private_cache_input(void) { return 7; }\n")
            subprocess.run(["gcc", "-c", str(root / "extra.c"), "-o", str(root / "extra.o")], check=True)
            subprocess.run(["ar", "r", str(selected / "libuuid.a"), str(root / "extra.o")], check=True)
            self.assertNotEqual(before, key())
            header = root / "forced.h"
            header.write_text("#define FORCED_INPUT 1\n")
            env["CPPFLAGS"] = "-include " + str(header)
            before = key()
            header.write_text("#define FORCED_INPUT 2\n")
            self.assertNotEqual(before, key())
            before = key(LIBS=str(root / "extra.o"))
            (root / "extra.c").write_text("int private_cache_input(void) { return 9; }\n")
            subprocess.run(["gcc", "-c", str(root / "extra.c"), "-o", str(root / "extra.o")], check=True)
            self.assertNotEqual(before, key(LIBS=str(root / "extra.o")))
            bad = subprocess.run([str(CACHE), "key", "erofs"], env=dict(env, LIBS="-l_cache_nonexistent"), text=True, capture_output=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("EROFS input preflight failed", bad.stderr)
            print("real EROFS key: relocation, source bytes, -L-selected archive, forced header, explicit object and failed link PASS")


if __name__ == "__main__":
    unittest.main()
