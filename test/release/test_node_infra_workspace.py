"""Exercise the source-to-image handoff without compiling products or running an engine."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

IMAGE = Path(__file__).resolve().parents[2] / "node-infra/image"
sys.path.insert(0, str(IMAGE))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workspace = load("node_infra_workspace_builder", IMAGE / "build-workspace.py")
assembler = load("node_infra_test_assembler", IMAGE / "build.py")
OWNERS = ("accelerator", "connector", "sandboxer", "orchestrator", "guest-runtime")
UNITS = ("accelerator", "connector", "sandboxer", "orchestrator", "runtime", "vmlinux")
ARCH = "x86_64"

# Honor the two actual package CLI shapes and archive layouts. Put the selected
# source SHA in each product so the image boundary can check what was staged.
PACKAGER = """set -eu
test "$1" = package; shift
unit=${PWD##*/}
if [ "$unit" = guest-runtime ]; then
    unit=$1; shift
    case "$unit" in runtime|vmlinux) ;; *) exit 1;; esac
fi
test "$#" -eq 3
version=$1; arch=$2; output=$3
test "$SOURCE_SHA" = "$(git rev-parse HEAD)"
test "$RELEASE_SOURCE_ROOT" = "$PWD"
test "$RELEASE_BIN_DIR" = "$PWD/bin/$arch"
test "$RELEASE_NATIVE_BIN_DIR" = "$PWD/native-deps/bin/$arch"
case "$unit" in
    runtime) name=sandbox-runtime-$arch-${version#runtime-} ;;
    vmlinux) name=vmlinux-$arch-${version#vmlinux-} ;;
    *) name=$unit-$version-linux-$arch ;;
esac
mkdir -p "$output/assets" "$output/bin"
echo "$SOURCE_SHA" > "$output/bin/$unit"
tar --owner=0 --group=0 --mode=u=rw,go=r -czf "$output/assets/$name.tar.gz" -C "$output" "bin/$unit"
"""


class WorkspaceImageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.sources = Path(temp.name)
        self.root = self.sources / "kuasar-sandbox"
        self.heads = {}
        for owner in ("kuasar-sandbox", *OWNERS):
            repo = self.sources / owner
            subprocess.run(["git", "init", "-q", "--template=", str(repo)], check=True)
            (repo / ".gitignore").write_text("/build/\n")
            if repo == self.root:
                # Real make is cheap here; this also checks its cwd and TARGET_ARCH.
                (repo / "Makefile").write_text(f".PHONY: build\nbuild:\n\t@test \"$(TARGET_ARCH)\" = {ARCH}\n")
            else:
                (repo / "scripts").mkdir()
                (repo / "scripts/release.sh").write_text(PACKAGER)
            config = {"orchestrator": ("config/files.go", "mergeYAMLFiles"),
                      "connector": ("pkg/vswitch/config_file.go", "parseConfigYAML")}.get(owner)
            if config:
                path = repo / config[0]
                path.parent.mkdir(parents=True)
                path.write_text(f"package fixture\nfunc {config[1]}() {{}}\n")
            workspace.tag_sources.git(repo, "add", ".")
            self.heads[owner] = self.commit(repo, owner)
        self.images = []
        self.result_root = self.root / "build/node-infra" / ARCH

    def commit(self, repo, message):
        workspace.tag_sources.git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                  "-c", "commit.gpgSign=false", "commit", "-q", "--allow-empty", "-m", message)
        return workspace.tag_sources.git(repo, "rev-parse", "HEAD")

    def build(self, action=None):
        def fake_image(args):
            self.assertEqual((args.engine, args.arch, args.sources), ("docker", ARCH, self.sources))
            selection = json.loads(args.selection.read_text())
            self.assertEqual(set(selection), set(UNITS))
            self.assertEqual(args.source_revision, self.heads["kuasar-sandbox"])
            for unit in UNITS:
                owner = "guest-runtime" if unit in ("vmlinux", "runtime") else unit
                self.assertEqual(selection[unit]["repository"], "kuasar-sandbox/" + owner)
                self.assertEqual(selection[unit]["sha"], self.heads[owner])
                self.assertEqual(selection[unit]["version"], "git:" + self.heads[owner])
                self.assertEqual((args.products / "bin" / unit).read_text().strip(), self.heads[owner])
            self.images.append(args.version)
            args.output.mkdir()
            oci, docker = "fixture.oci.tar.gz", "fixture.docker.tar.gz"
            (args.output / oci).write_bytes(b"oci fixture")
            (args.output / docker).write_bytes(b"docker fixture")
            (args.output / f"node-infra-{ARCH}.json").write_text(json.dumps(
                {"archive": oci, "docker_archive": {"name": docker}}))
            if action:
                action(args.output)

        def command(argv, **kwargs):
            if argv[0] == "make":
                return subprocess.run(argv, **kwargs)
            self.assertEqual(Path(argv[2]), IMAGE / "build.py")
            # Use the production image CLI parser to check the caller's arguments.
            with mock.patch.object(sys, "argv", argv[2:]), mock.patch.object(assembler, "build", fake_image):
                assembler.main()

        def source_workspace(sources, owners, env, *, adapt_requirements):
            self.assertTrue(adapt_requirements)
            return env

        with mock.patch.object(workspace, "check_environment"), \
             mock.patch.object(workspace.workspace_packages.go_workspace, "configure",
                               side_effect=source_workspace), \
             mock.patch.object(workspace, "subprocess", wraps=subprocess) as external, \
             contextlib.redirect_stdout(io.StringIO()):
            external.run.side_effect = command
            return workspace.build(self.root, "docker", ARCH)

    def test_repeated_source_build_and_changed_sources(self):
        first = self.build()
        second = self.build()
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, self.result_root)
        self.assertEqual(self.images[0], self.images[1])
        self.assertRegex(self.images[0], r"^local-[a-f0-9]+$")
        self.assertEqual((first / "fixture.docker.tar.gz").read_bytes(), b"docker fixture")
        self.assertTrue((second / "fixture.oci.tar.gz").is_file())
        for owner in ("kuasar-sandbox", "guest-runtime"):
            self.heads[owner] = self.commit(self.sources / owner, "advance " + owner)
            self.build()
            self.assertNotEqual(self.images[-2], self.images[-1])

    def test_dirty_component_or_platform_stops_before_make(self):
        for repo, name in ((self.root, "untracked"), (self.sources / "connector", "scripts/release.sh")):
            with self.subTest(repo=repo.name):
                path = repo / name
                path.write_text("local changes")
                with mock.patch.object(workspace, "check_environment"), \
                     mock.patch.object(workspace, "subprocess", wraps=subprocess) as external, \
                     self.assertRaisesRegex(ValueError, repo.name):
                    workspace.build(self.root, "docker", ARCH)
                external.run.assert_not_called()
                if repo == self.root:
                    path.unlink()
        self.assertFalse(self.result_root.exists())

    def test_non_native_target_stops_before_source_and_tool_checks(self):
        with mock.patch.object(workspace.platform, "system", return_value="Linux"), \
             mock.patch.object(workspace.platform, "machine", return_value="aarch64"), \
             mock.patch.object(workspace, "identify") as identify, \
             mock.patch.object(workspace.shutil, "which") as which, \
             self.assertRaisesRegex(ValueError, "native Linux/x86_64"):
            workspace.build(self.root, "docker", ARCH)
        identify.assert_not_called()
        which.assert_not_called()

    def test_failed_image_assembly_and_midbuild_source_change_publish_nothing(self):
        retained = self.build()

        def fail(output):
            raise subprocess.CalledProcessError(1, ["fixture-image"])

        with self.assertRaises(subprocess.CalledProcessError):
            self.build(fail)
        with self.assertRaisesRegex(ValueError, "archives"):
            self.build(lambda output: (output / "fixture.docker.tar.gz").unlink())
        with self.assertRaisesRegex(ValueError, "changed during the build"):
            self.build(lambda output: self.commit(self.sources / "orchestrator", "concurrent source update"))
        self.assertEqual(list(self.result_root.iterdir()), [retained])
        self.assertTrue((retained / "fixture.docker.tar.gz").is_file())


if __name__ == "__main__":
    unittest.main()
