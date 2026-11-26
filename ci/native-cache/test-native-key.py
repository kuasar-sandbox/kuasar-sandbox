#!/usr/bin/env python3
"""Exercise native cache inputs and restoration without compiling products."""

import os
import json
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


class VppKeyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='vpp-key-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'src'
        self.connector = self.workspace / 'connector'
        self.native = self.connector / 'native-deps'
        for name in ('LICENSE', 'native-deps/Makefile', 'native-deps/deps/common.sh',
                     'native-deps/deps/build-vpp.sh', 'native-deps/deps/stage-vpp.py',
                     'native-deps/deps/static-libraries.py',
                     'native-deps/deps/vpp-source.json', 'scripts/vpp-runtime.py',
                     'native-deps/deps/vpp-patches/0001-policy.patch',
                     'native-deps/deps/vpp-patches/0001-policy.patch.license',
                     'egress/vpp/plugins/egress_policy/policy.c'):
            path = self.connector / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('exact input fixture\n')
        self.environment = {name: value for name, value in os.environ.items()
                            if name in {'PATH', 'LANG', 'LC_ALL', 'TMPDIR'}}
        self.environment.update(HOME=str(self.root / 'home'), TARGET_ARCH=platform.machine(),
                                KUASAR_WORKSPACE_ROOT=str(self.workspace),
                                KUASAR_NATIVE_CACHE_ROOT=str(self.root / 'cache'))

    def invoke(self, command='key', **changes):
        return subprocess.run([str(CACHE), command, 'vpp'], env={**self.environment, **changes},
                              cwd=self.root, capture_output=True, text=True)

    def key(self, **changes):
        result = self.invoke(**changes)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'^vpp\t[0-9a-f]{64}\n$')
        return result.stdout

    def test_patch_body_plugin_source_lock_and_recipe_change_key(self):
        for name in ('native-deps/deps/vpp-patches/0001-policy.patch',
                     'egress/vpp/plugins/egress_policy/policy.c',
                     'native-deps/deps/vpp-source.json', 'native-deps/deps/stage-vpp.py',
                     'native-deps/deps/static-libraries.py',
                     'scripts/vpp-runtime.py'):
            with self.subTest(name=name):
                before = self.key()
                path = self.connector / name
                path.write_text(path.read_text() + 'changed content, same filename\n')
                self.assertNotEqual(before, self.key())

    def test_architecture_flags_and_local_archive_bytes_change_key(self):
        before = self.key()
        other = 'aarch64' if platform.machine() == 'x86_64' else 'x86_64'
        self.assertNotEqual(before, self.key(TARGET_ARCH=other))
        self.assertNotEqual(before, self.key(CFLAGS='-O1'))
        archive = self.root / 'upstream.tar.gz'
        archive.write_bytes(b'first archive')
        before = self.key(VPP_TARBALL=str(archive))
        archive.write_bytes(b'changed archive')
        self.assertNotEqual(before, self.key(VPP_TARBALL=str(archive)))

    def test_default_clang_and_explicit_cc_use_selected_compiler_bytes(self):
        tools = self.root / 'tools'
        tools.mkdir()
        clang, gcc = tools / 'clang', tools / 'gcc'

        def compiler(path, revision):
            # Identical version strings still need distinct compiler keys.
            path.write_text(f'#!/bin/sh\n# revision {revision}\necho fixture-compiler\n')
            path.chmod(0o755)

        compiler(clang, 1)
        compiler(gcc, 1)
        environment = {'PATH': str(tools) + os.pathsep + self.environment['PATH']}
        default = self.key(**environment)
        compiler(gcc, 2)
        self.assertEqual(default, self.key(**environment))
        compiler(clang, 2)
        self.assertNotEqual(default, self.key(**environment))

        environment['CC'] = str(gcc)
        explicit = self.key(**environment)
        compiler(clang, 3)
        self.assertEqual(explicit, self.key(**environment))
        compiler(gcc, 3)
        self.assertNotEqual(explicit, self.key(**environment))

    def test_static_linker_archives_startup_objects_and_lto_plugin_change_key(self):
        tools = self.root / 'static-tools'
        tools.mkdir()
        compiler = tools / 'cc'
        compiler.write_text('''#!/usr/bin/env python3
from pathlib import Path
import shlex,sys
root=Path(__file__).parent
linker='ld.lld' if '-fuse-ld=lld' in sys.argv else 'ld'
for value in sys.argv[1:]:
 if value.startswith('-print-prog-name=') or value.startswith('-print-file-name='):
  print(root/value.split('=',1)[1]);break
 elif value == '-print-libgcc-file-name':
  print(root/'libgcc.a');break
 elif value == '-###':
  words=[str(root/linker),'-plugin',str(root/'lto.so')]+[flag for flag in sys.argv if flag.startswith('-L')]
  print(' '.join(map(shlex.quote,words)),file=sys.stderr);break
else: print('fixture compiler 1')
''')
        compiler.chmod(0o755)
        for name in ('ld', 'ld.lld'):
            path = tools / name
            path.write_text('#!/bin/sh\necho fixture linker 1\n')
            path.chmod(0o755)
        for name in ('libc.a', 'libgcc.a', 'crt1.o', 'lto.so', 'libc.so.6'):
            (tools / name).write_bytes(b'original selected input')
        environment = {'CC': str(compiler), 'LDFLAGS': '-fuse-ld=lld'}
        before = self.key(**environment)
        for name in ('ld', 'libc.so.6'):
            path = tools / name
            path.write_bytes(path.read_bytes() + b'\n# unrelated input\n')
        self.assertEqual(before, self.key(**environment))
        for name in ('ld.lld', 'libc.a', 'libgcc.a', 'crt1.o', 'lto.so'):
            with self.subTest(name=name):
                before = self.key(**environment)
                path = tools / name
                path.write_bytes(path.read_bytes() + b'\n# changed bytes, same package/version\n')
                self.assertNotEqual(before, self.key(**environment))
        alternate = tools / 'alternate'
        alternate.mkdir()
        (alternate / 'libc.a').write_bytes(b'overridden static libc')
        environment['LDFLAGS'] += ' -L' + str(alternate)
        before = self.key(**environment)
        (tools / 'libc.a').write_bytes(b'no longer selected')
        self.assertEqual(before, self.key(**environment))
        (alternate / 'libc.a').write_bytes(b'changed selected override')
        self.assertNotEqual(before, self.key(**environment))

    def test_restore_preserves_complete_vpp_and_sibling_cli_without_building_again(self):
        # A tiny owner recipe emits ELF headers and manifests as data. The
        # actual native cache pack/restore/reader paths remain unchanged.
        builder = self.native / 'fixture-build.py'
        builder.write_text('''import hashlib,json,os,pathlib,struct
root=pathlib.Path.cwd()
arch=os.environ['TARGET_ARCH']
output=root.parent/'bin'/arch
output.mkdir(parents=True,exist_ok=True)
rows={'connector-vpp':0o755}
files={}
for name,mode in rows.items():
 data=bytearray(120);data[:7]=b'\\x7fELF\\x02\\x01\\x01'
 struct.pack_into('<HHI',data,16,2,62 if arch=='x86_64' else 183,1)
 struct.pack_into('<Q',data,32,64);struct.pack_into('<HHH',data,52,64,56,1)
 struct.pack_into('<IIQQQQQQ',data,64,1,5,0,0x400000,0,len(data),len(data),4096)
 path=output/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);path.chmod(mode)
 files[name]=hashlib.sha256(data).hexdigest()
manifest=dict(schema=1,product='connector-egress-native-runtime',architecture=arch,files=files,
 cpu_baseline='x86-64-v2' if arch=='x86_64' else 'armv8-a+crc',required_system_libraries=[])
(output/'vpp-runtime.json').write_text(json.dumps(manifest))
licenses=root/'build'/arch/'vpp/licenses';licenses.mkdir(parents=True,exist_ok=True)
for name in ('CONNECTOR-LICENSE','VPP-LICENSE','VPP-EMBEDDED-NOTICES'):
 (licenses/name).write_text(name+' license\\n')
(licenses.parent/'static-libraries.json').write_text('{}\\n')
(root/'build/src/vpp').mkdir(parents=True,exist_ok=True)
with (root/'build-count').open('a') as stream:stream.write('build\\n')
''')
        (self.native / 'Makefile').write_text('vpp:\n\t@python3 fixture-build.py\n')
        bindir = self.connector / 'bin' / platform.machine()
        bindir.mkdir(parents=True)
        sibling = bindir / 'connector-ctl'
        sibling.write_bytes(b'keep the separately selected Go product')
        unrelated = bindir / 'lib/other-product/library.so'
        unrelated.parent.mkdir(parents=True)
        unrelated.write_bytes(b'not owned by the VPP cache')
        first = self.invoke('restore-or-build')
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        expected = (bindir / 'connector-vpp').read_bytes()
        (bindir / 'connector-vpp').write_bytes(b'old output')
        second = self.invoke('restore-or-build')
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('vpp hit ', second.stderr)
        self.assertEqual((self.native / 'build-count').read_text(), 'build\n')
        self.assertEqual((bindir / 'connector-vpp').read_bytes(), expected)
        self.assertEqual(sibling.read_bytes(), b'keep the separately selected Go product')
        self.assertEqual(unrelated.read_bytes(), b'not owned by the VPP cache')
        manifest = json.loads((bindir / 'vpp-runtime.json').read_text())
        self.assertTrue(all((bindir / name).is_file() for name in manifest['files']))
        self.assertTrue((self.native / 'build' / platform.machine() / 'vpp/licenses/VPP-LICENSE').is_file())
        self.assertTrue((self.native / 'build' / platform.machine() / 'vpp/static-libraries.json').is_file())


if __name__ == "__main__":
    unittest.main()
