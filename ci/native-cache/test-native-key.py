#!/usr/bin/env python3
"""Exercise CH cache keys without compiling products or fixture programs."""

import os
from pathlib import Path
import platform
import subprocess
import tempfile
import unittest


CACHE = Path(__file__).with_name("native-cache.sh").resolve()


class CargoKeyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-key-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "src"
        self.native = self.workspace / "sandboxer/native-deps"
        (self.native / "deps/ch-patches").mkdir(parents=True)
        for name in ("Makefile", "deps/common.sh", "deps/build-cloud-hypervisor.sh"):
            (self.native / name).write_text("# exact recipe fixture\n")
        self.home = self.root / "home"
        self.cargo_home = self.home / ".cargo"
        self.cargo_home.mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.program(self.bin / "rustc")
        self.program(self.bin / "cargo")
        # Keep callers' credentials/config/flags out of this private fixture.
        self.environment = {name: value for name, value in os.environ.items()
                            if name in {"PATH", "LANG", "LC_ALL", "TMPDIR"}}
        self.environment.update(HOME=str(self.home), CARGO_HOME=str(self.cargo_home),
                                PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                                KUASAR_WORKSPACE_ROOT=str(self.workspace),
                                TARGET_ARCH=platform.machine())
        self.target = {"x86_64": "X86_64", "aarch64": "AARCH64"}[platform.machine()]
        self.target_prefix = "CARGO_TARGET_" + self.target + "_UNKNOWN_LINUX_GNU_"

    def program(self, path, revision=1):
        path.parent.mkdir(parents=True, exist_ok=True)
        # The reported version stays unchanged; bytes are the tested identity.
        path.write_text("#!/bin/sh\n# tool revision " + str(revision)
                        + "\ncase \"$*\" in\n"
                          "  -vV|-Vv|--version) printf 'native-key tool 1.0\\nhost: "
                        + platform.machine() + "-unknown-linux-gnu\\n' ;;\n"
                          "  *) echo 'compilation is forbidden in this fixture' >&2; exit 97 ;;\n"
                          "esac\n")
        path.chmod(0o755)
        return str(path)

    def invoke(self, **changes):
        return subprocess.run([str(CACHE), "key", "cloud-hypervisor"],
                              env={**self.environment, **changes}, cwd=self.root,
                              capture_output=True, text=True)

    def key(self, **changes):
        result = self.invoke(**changes)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"^cloud-hypervisor\t[0-9a-f]{64}\n$")
        return result.stdout

    def test_selected_compiler_and_same_path_replacement_change_key(self):
        default = self.key()
        alternate = self.program(self.root / "alternate/rustc")
        selected = self.key(RUSTC=alternate)
        self.assertNotEqual(default, selected)
        self.program(Path(alternate), revision=2)
        self.assertNotEqual(selected, self.key(RUSTC=alternate))

    def test_relative_compiler_is_resolved_from_recipe_directory(self):
        compiler = self.native / "tools/rustc"
        self.program(compiler)
        before = self.key(RUSTC="./tools/rustc")
        self.program(compiler, revision=2)
        self.assertNotEqual(before, self.key(RUSTC="./tools/rustc"))

    def test_wrappers_bind_selected_executable_bytes(self):
        wrapper = self.root / "wrapper"
        for variable in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER",
                         "CARGO_BUILD_RUSTC_WRAPPER", "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER"):
            with self.subTest(variable=variable):
                self.program(wrapper)
                before = self.key(**{variable: str(wrapper)})
                self.program(wrapper, revision=2)
                self.assertNotEqual(before, self.key(**{variable: str(wrapper)}))

    def test_empty_wrapper_overrides_config_environment_selection(self):
        missing = str(self.root / "missing-wrapper")
        for direct, configured in (("RUSTC_WRAPPER", "CARGO_BUILD_RUSTC_WRAPPER"),
                                   ("RUSTC_WORKSPACE_WRAPPER", "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER")):
            with self.subTest(variable=direct):
                self.key(**{direct: "", configured: missing})
                result = self.invoke(**{configured: missing})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("executable is unavailable", result.stderr)

    def test_release_profile_and_build_override_settings_change_key(self):
        default = self.key()
        for name, value in (("LTO", "false"), ("OPT_LEVEL", "1"), ("CODEGEN_UNITS", "1"),
                            ("PANIC", "abort"), ("BUILD_OVERRIDE_OPT_LEVEL", "1")):
            with self.subTest(name=name):
                self.assertNotEqual(default, self.key(**{"CARGO_PROFILE_RELEASE_" + name: value}))

    def test_encoded_empty_and_target_flags_are_distinct_inputs(self):
        default = self.key()
        empty = self.key(CARGO_ENCODED_RUSTFLAGS="")
        self.assertNotEqual(default, empty)
        self.assertNotEqual(empty, self.key(CARGO_ENCODED_RUSTFLAGS="-C\x1fopt-level=1"))
        self.assertNotEqual(default, self.key(**{self.target_prefix + "RUSTFLAGS": "-Copt-level=1"}))

    def test_target_linker_binds_executable_bytes(self):
        linker = self.root / "linker"
        self.program(linker)
        variable = self.target_prefix + "LINKER"
        before = self.key(**{variable: str(linker)})
        self.program(linker, revision=2)
        self.assertNotEqual(before, self.key(**{variable: str(linker)}))

    def test_config_from_real_recipe_ancestors_changes_key(self):
        before = self.key()
        config = self.workspace / "sandboxer/.cargo/config.toml"
        config.parent.mkdir()
        config.write_text('[profile.release]\nopt-level = 1\n')
        selected = self.key()
        self.assertNotEqual(before, selected)
        config.write_text('[profile.release]\nopt-level = 2\n')
        self.assertNotEqual(selected, self.key())

    def test_unsupported_config_tool_selection_is_rejected(self):
        config = self.cargo_home / "config.toml"
        for content in ('[build]\nrustc = "other-rustc"\n',
                        '[build]\nrustc-workspace-wrapper = "wrapper"\n',
                        '[target.x86_64-unknown-linux-gnu]\nlinker = "other-cc"\n',
                        '[env]\nRUSTC = "other-rustc"\n'):
            with self.subTest(config=content):
                config.write_text(content)
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("native-cache: Cargo config", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_missing_tools_and_conflicting_target_are_rejected(self):
        for variable in ("RUSTC", self.target_prefix + "LINKER"):
            with self.subTest(variable=variable):
                result = self.invoke(**{variable: str(self.root / "missing")})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("executable is unavailable", result.stderr)
        result = self.invoke(CARGO_BUILD_TARGET="aarch64-unknown-linux-gnu")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("recipe selects its target/output layout", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_compiler_selection_matches_existing_recipe_and_packager(self):
        compiler = self.root / "alternate-rustc"
        self.program(compiler)
        for value in (str(compiler), ""):
            with self.subTest(value=value):
                result = self.invoke(CARGO_BUILD_RUSTC=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("release material collector; use RUSTC", result.stderr)
                self.assertEqual(result.stdout, "")
        compiler.write_text(compiler.read_text().replace(platform.machine() + "-unknown-linux-gnu",
                                                        "different-unknown-linux-gnu"))
        result = self.invoke(RUSTC=str(compiler))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RUSTC host differs", result.stderr)
        self.assertEqual(result.stdout, "")
        compiler.write_text("#!/bin/sh\nexit 17\n")
        result = self.invoke(RUSTC=str(compiler))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot identify the selected Cargo Rust compiler", result.stderr)


if __name__ == "__main__":
    unittest.main()
