"""Test image base selection without claiming to run Docker or Podman."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("node_infra_image_builder", ROOT / "node-infra/image/build.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
BASE = "docker.io/library/ubuntu:24.04"
TAG = "localhost/node-infra-build:test-unique"
ID = "sha256:" + "a" * 64
LAYER = "sha256:" + "b" * 64


class ImageBuildTests(unittest.TestCase):
    def execute(self, engine="docker", arch="amd64", *, missing=False, allow_pull=False,
                base_change=None, on_build=None, inspect_error=None, cleanup_error=False, tag_error=False):
        base = {"Id": ID, "Os": "linux", "Architecture": arch, "RootFS": {"Layers": [LAYER]}}
        if base_change:
            base_change(base)
        images = {} if missing else {BASE: base}
        commands = []
        def run(argv, **kwargs):
            argv = [str(value) for value in argv]
            commands.append(argv)
            if argv[1:3] == ["image", "inspect"]:
                if inspect_error and argv[3] == BASE:
                    raise subprocess.CalledProcessError(1, argv, stderr=inspect_error)
                if argv[3] not in images:
                    raise subprocess.CalledProcessError(1, argv, stderr="Error: No such image: " + argv[3])
                return subprocess.CompletedProcess(argv, 0, json.dumps([images[argv[3]]]))
            if argv[1] == "pull":
                self.assertEqual(argv, [engine, "pull", "--platform", "linux/" + arch, BASE])
                images[BASE] = base
            elif argv[1:3] == ["image", "tag"]:
                self.assertEqual(argv[3:], [ID, TAG + "-base"])
                images[TAG + "-base"] = copy.deepcopy(base)
                if tag_error:
                    raise subprocess.CalledProcessError(43, argv)
            elif argv[1] == "build":
                self.assertIn("--pull=false" if engine == "docker" else "--pull=never", argv)
                self.assertEqual(sum(value.startswith("BASE_IMAGE=") for value in argv), 1)
                self.assertEqual(argv[-3:], ["--build-arg", "BASE_IMAGE=" + TAG + "-base", "/context"])
                images[TAG] = copy.deepcopy(base)
                images[TAG]["Id"] = "sha256:" + "c" * 64
                images[TAG]["RootFS"]["Layers"].append("sha256:" + "d" * 64)
                # The original mutable tag moves after selection; the build must
                # use and validate the isolated alias, never re-resolve BASE.
                images[BASE] = {"Id": "sha256:" + "e" * 64}
                if on_build:
                    on_build(images)
            elif argv[1:3] == ["image", "rm"]:
                self.assertIn(argv[3], (TAG, TAG + "-base"))
                if cleanup_error:
                    raise subprocess.CalledProcessError(1, argv)
                images.pop(argv[3], None)
            else:
                self.fail("unexpected command: " + repr(argv))
            return subprocess.CompletedProcess(argv, 0, "")
        self.commands = commands
        self.images = images
        with mock.patch.object(builder, "run", side_effect=run), contextlib.redirect_stderr(io.StringIO()):
            return builder.build_image(engine, BASE, arch, TAG,
                                       [engine, "build", "--pull=false" if engine == "docker" else "--pull=never"],
                                       Path("/context"), allow_pull=allow_pull)

    def test_preloaded_base_pinned_for_both_engines_and_architectures(self):
        for engine in ("docker", "podman"):
            for arch in ("amd64", "arm64"):
                with self.subTest(engine=engine, arch=arch):
                    self.assertEqual(self.execute(engine, arch), ID)
                    self.assertFalse(any(command[1] == "pull" for command in self.commands))
                    self.assertEqual(sum(command[1:4] == ["image", "inspect", BASE]
                                         for command in self.commands), 1)
                    self.assertIn(BASE, self.images)
                    self.assertIn(TAG, self.images)
                    self.assertNotIn(TAG + "-base", self.images)

    def test_default_missing_from_daemon_is_materialized_before_build(self):
        for engine in ("docker", "podman"):
            for arch in ("amd64", "arm64"):
                with self.subTest(engine=engine, arch=arch):
                    self.assertEqual(self.execute(engine, arch, missing=True, allow_pull=True), ID)
                    pull = next(i for i, command in enumerate(self.commands) if command[1] == "pull")
                    build = next(i for i, command in enumerate(self.commands) if command[1] == "build")
                    self.assertLess(pull, build)
                    self.assertEqual(self.commands[pull + 1], [engine, "image", "inspect", BASE])
                    self.assertEqual(self.commands[pull + 2], [engine, "image", "tag", ID, TAG + "-base"])

    def test_missing_explicit_base_never_pulls(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute(missing=True)
        self.assertEqual(len(self.commands), 1)

    def test_daemon_failure_never_becomes_registry_pull(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute(allow_pull=True, inspect_error="Cannot connect to the Docker daemon")
        self.assertEqual(len(self.commands), 1)

    def test_invalid_base_stops_before_tag_or_build(self):
        for field, value in (("Architecture", "386"), ("Os", "windows"),
                             ("Id", "not-an-id"), ("RootFS", {"Layers": []})):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.execute(base_change=lambda base: base.update({field: value}))
            self.assertEqual(len(self.commands), 1)

    def test_changed_alias_and_candidate_mismatches_fail_closed(self):
        for reference, field, value in (
                (TAG + "-base", "Id", "sha256:" + "f" * 64),
                (TAG + "-base", "Architecture", "386"),
                (TAG, "Architecture", "386"),
                (TAG, "Os", "windows"),
                (TAG, "RootFS", {"Layers": ["different"]})):
            with self.subTest(reference=reference, field=field), self.assertRaises(ValueError):
                self.execute(on_build=lambda images: images[reference].update({field: value}))
            self.assertNotIn(TAG, self.images)
            self.assertNotIn(TAG + "-base", self.images)
            self.assertIn(BASE, self.images)

    def test_build_failure_cleans_only_temporary_images(self):
        def fail(images):
            raise subprocess.CalledProcessError(42, ["docker", "build"])
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.execute(on_build=fail)
        self.assertEqual(error.exception.returncode, 42)
        self.assertEqual(set(self.images), {BASE})

    def test_cleanup_error_does_not_mask_build_failure(self):
        def fail(images):
            raise subprocess.CalledProcessError(42, ["docker", "build"])
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.execute(on_build=fail, cleanup_error=True)
        self.assertEqual(error.exception.returncode, 42)

    def test_tag_failure_after_creation_cleans_temporary_alias(self):
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.execute(tag_error=True)
        self.assertEqual(error.exception.returncode, 43)
        self.assertEqual(set(self.images), {BASE})
        self.assertFalse(any(command[1] == "build" for command in self.commands))

    def test_postbuild_inspect_failure_cleans_both_temporary_tags(self):
        def remove_alias(images):
            images.pop(TAG + "-base")
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute(on_build=remove_alias)
        self.assertEqual(set(self.images), {BASE})

    def test_failed_pull_does_not_attempt_build(self):
        commands = []
        def run(argv, **kwargs):
            commands.append(argv)
            raise subprocess.CalledProcessError(1, argv, stderr="No such image")
        with mock.patch.object(builder, "run", side_effect=run), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                builder.build_image("docker", BASE, "amd64", TAG, ["docker", "build"],
                                    Path("/context"), allow_pull=True)
        self.assertEqual([command[1] for command in commands], ["image", "pull"])

    def test_inspect_error_preserves_engine_diagnostic(self):
        error = subprocess.CalledProcessError(1, ["docker", "image", "inspect"], stderr="engine diagnostic")
        stream = io.StringIO()
        with mock.patch.object(builder, "run", side_effect=error), contextlib.redirect_stderr(stream):
            with self.assertRaises(subprocess.CalledProcessError):
                builder.inspect_image("docker", BASE)
        self.assertIn("engine diagnostic", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
