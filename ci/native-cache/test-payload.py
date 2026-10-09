#!/usr/bin/env python3
"""Native cache rejects unsafe and incomplete material archives before restore."""

import importlib.util
import io
import hashlib
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("payload", Path(__file__).with_name("validate-payload.py"))
PAYLOAD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PAYLOAD)


class PayloadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="native-material-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = self.root / "src"
        self.workspace.mkdir()
        self.archive = self.root / "payload.tar"
        self.output = "sandboxer/native-deps/build/src/cloud-hypervisor"
        self.manifest = self.output + "/Cargo.toml"

    def member(self, name, content=b"fixture", kind=tarfile.REGTYPE, link="", mode=0o644):
        item = tarfile.TarInfo(name)
        item.type = kind
        item.linkname = link
        item.mode = mode
        item.size = len(content) if kind == tarfile.REGTYPE else 0
        return item, content

    def check(self, extra=(), roots=None):
        with tarfile.open(self.archive, "w") as archive:
            for item, content in (self.member(self.output, kind=tarfile.DIRTYPE),
                                  self.member(self.manifest), *extra):
                archive.addfile(item, io.BytesIO(content) if item.isfile() else None)
        PAYLOAD.validate(self.archive, self.workspace, roots or [self.output, self.manifest])

    def test_regular_materials_and_internal_links(self):
        self.check([self.member(self.output + "/LICENSE", kind=tarfile.SYMTYPE, link="Cargo.toml"),
                    self.member(self.output + "/COPYING", kind=tarfile.LNKTYPE, link=self.manifest)])

    def test_rejects_paths_outside_component_and_git_state(self):
        for name in ("/etc/passwd", "../escape", self.output + "/../../escape",
                     "sandboxer/go.mod", self.output + "/.git/config"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.check([self.member(name)])

    def test_rejects_link_escapes_and_link_ancestors(self):
        for link in ("/etc", "../../../../../../etc"):
            with self.subTest(link=link), self.assertRaises(ValueError):
                self.check([self.member(self.output + "/LICENSE", kind=tarfile.SYMTYPE, link=link)])
        with self.assertRaisesRegex(ValueError, "traverses a link"):
            self.check([self.member(self.output + "/alias", kind=tarfile.SYMTYPE, link="Cargo.toml"),
                        self.member(self.output + "/alias/child")])

    def test_rejects_missing_and_empty_packaging_materials(self):
        with self.assertRaisesRegex(ValueError, "omits outputs"):
            self.check(roots=[self.output, self.output + "/build-report.jsonl"])
        with self.assertRaisesRegex(ValueError, "empty required"):
            self.check([self.member(self.output + "/build-report.jsonl", b"")],
                       roots=[self.output, self.output + "/build-report.jsonl"])

    def test_rejects_duplicate_entries_and_special_files(self):
        for entry in (self.member(self.manifest), self.member(self.output + "/fifo", kind=tarfile.FIFOTYPE),
                      self.member(self.output + "/setuid", mode=0o4755)):
            with self.subTest(entry=entry[0].name), self.assertRaises(ValueError):
                self.check([entry])

    def test_rejects_dangling_hard_link(self):
        with self.assertRaisesRegex(ValueError, "lacks a regular target"):
            self.check([self.member(self.output + "/LICENSE", kind=tarfile.LNKTYPE,
                                    link=self.output + "/missing")])

    def test_rejects_existing_workspace_parent_symlink(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / "sandboxer").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "workspace output parent"):
            self.check()
        self.assertEqual(list(outside.iterdir()), [])

    def test_erofs_preserves_only_its_exact_archive_and_packaging_materials(self):
        native = self.workspace / "guest-runtime/native-deps"
        native.mkdir(parents=True)
        (native / "Makefile").write_text("EROFS_TARBALL ?= https://example.test/erofs.tar.gz\\#erofs.tar.gz\n")
        source = native / "build/x86_64/src/erofs-utils"
        files = {"bin/x86_64/mkfs.erofs": b"mkfs", "bin/x86_64/fsck.erofs": b"fsck",
                 "bin/x86_64/.erofs-recipe": b"stamp", "build/tarball/erofs.tar.gz": b"exact-archive",
                 "build/tarball/unrelated.tar.gz": b"not-this-component"}
        for name in ("AUTHORS", "COPYING", "mkfs/mkfs.erofs.map", "mkfs/mkfs_erofs-main.o",
                     "fsck/fsck.erofs.map", "fsck/fsck_erofs-main.o", "lib/.libs/liberofs.a"):
            files["build/x86_64/src/erofs-utils/" + name] = name.encode()
        for name, content in files.items():
            path = native / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        # Autoreconf may install absolute links which package/reuse never read.
        # Such unrelated generated helpers must not make a valid payload unsafe.
        (source / "config.sub").symlink_to("/usr/share/automake/config.sub")
        descriptor = self.root / "inputs.tsv"
        descriptor.write_text("fixture-input-identity\n")
        key = hashlib.sha256(descriptor.read_bytes()).hexdigest()
        entry = self.root / "entry"
        cache = Path(__file__).with_name("native-cache.sh")
        env = dict(os.environ, KUASAR_WORKSPACE_ROOT=str(self.workspace),
                   KUASAR_NATIVE_CACHE_ROOT=str(self.root / "cache"), TARGET_ARCH="x86_64")
        env.pop("EROFS_TARBALL", None)
        result = subprocess.run(["bash", "-c", 'source "$1" help >/dev/null; publish_entry erofs "$2" "$3" "$4"; restore_entry erofs "$4" "$2"',
                                 "bash", str(cache), key, str(descriptor), str(entry)],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with tarfile.open(entry / "payload.tar", "r:") as archive:
            names = archive.getnames()
            exact = archive.extractfile("./guest-runtime/native-deps/build/tarball/erofs.tar.gz").read()
        self.assertEqual(exact, b"exact-archive")
        self.assertFalse(any(name.endswith(("config.sub", "unrelated.tar.gz")) for name in names))
        self.assertFalse((source / "config.sub").is_symlink())
        self.assertEqual((native / "build/tarball/erofs.tar.gz").read_bytes(), b"exact-archive")


if __name__ == "__main__":
    unittest.main()
