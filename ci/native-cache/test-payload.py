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



if __name__ == "__main__":
    unittest.main()
