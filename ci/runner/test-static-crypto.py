#!/usr/bin/env python3
"""Offline catalog/provider regressions. Synthetic identities exist only in tests.

The catalog tests are mirrored in guest-runtime so either repo can test alone.
The provider cases run when the adjacent provider is present.
"""
import contextlib
import copy
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).absolute().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())


def tar_bytes(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data, kind in entries:
            entry = tarfile.TarInfo(name)
            entry.mode = 0o644
            entry.type = kind
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                entry.linkname = "../../outside"
            else:
                entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return stream.getvalue()


def rpm_bytes(entries):
    body = bytearray()
    for name, data, mode, links in entries + [("TRAILER!!!", b"", 0, 1)]:
        filename = name.encode() + b"\0"
        fields = [1, mode, 0, 0, links, 0, len(data), 0, 0, 0, 0, len(filename), 0]
        body.extend(b"070701" + "".join(f"{n:08x}" for n in fields).encode() + filename)
        body.extend(b"\0" * (-len(body) % 4))
        body.extend(data)
        body.extend(b"\0" * (-len(body) % 4))
    return b"\xed\xab\xee\xdb" + b"\0" * 92 + (b"\x8e\xad\xe8\x01" + b"\0" * 12) * 2 + gzip.compress(body, mtime=0)


class ContractTests(unittest.TestCase):
    def test_paired_recipe_contract(self):
        module = load("contract", HERE / "static-crypto-catalog.py")
        if (HERE / "static-crypto.py").exists():
            self.assertEqual(module.PROVIDER_SHA256, module.digest((HERE / "static-crypto.py").read_bytes()))
            peer = HERE.parents[2] / "guest-runtime/scripts/static-crypto-catalog.py"
        else:
            peer = HERE.parents[1] / "kuasar-sandbox/ci/runner/static-crypto-catalog.py"
        if peer.exists():
            self.assertEqual((HERE / "static-crypto-catalog.py").read_bytes(), peer.read_bytes())


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.c = load("catalog_test", HERE / "static-crypto-catalog.py")
        self.c.PINS = copy.deepcopy(self.c.PINS)
        self.recipe = (HERE / "static-crypto.py").read_bytes() if (HERE / "static-crypto.py").exists() else b"fixture recipe\n"
        self.c.PROVIDER_SHA256 = self.c.digest(self.recipe)
        catalog = self.root / "catalog"
        for name, pin in self.c.PINS.items():
            files = {n: ("fixture source " + n + "\n").encode() for n in pin["files"]}
            prefix = name + "-" + pin["upstream"] + "/"
            files[pin["archive"]] = tar_bytes([(prefix + n, ("fixture notice " + n).encode(), tarfile.REGTYPE)
                                             for n in pin["notices"]] + [(prefix + "input", b"before\n", tarfile.REGTYPE)])
            pin["files"] = {n: self.c.digest(b) for n, b in files.items()}
            srpm = rpm_bytes([(n, b, stat.S_IFREG | 0o644, 1) for n, b in files.items()])
            pin["srpm"] = self.c.digest(srpm)
            put(catalog / "sources" / name / self.c.srpm_name(name), srpm)
            for filename, body in files.items():
                put(catalog / "sources" / name / filename, body)
            for filename, body in self.c.source_tar(name, files[pin["archive"]]).items():
                put(catalog / "licenses" / name / filename, body)
            put(catalog / "lib" / (name + ".a"), b"!<arch>\nfixture archive " + name.encode())
            for filename in self.c.BUILD_FILES + ["src/" + h for h in pin["headers"]]:
                put(catalog / "build" / name / filename, "fixture " + filename + "\n")
        put(catalog / "recipe/static-crypto.py", self.recipe)
        put(catalog / "recipe/static-crypto-catalog.py", (HERE / "static-crypto-catalog.py").read_bytes())
        tools = {n: {"path": "/usr/bin/" + n, "sha256": "0" * 64, "version": "fixture"}
                 for n in self.c.TOOLS + ["cc1", "collect2"]}
        tools["target"] = "fixture-target"
        tool_bytes = json.dumps(tools).encode()
        put(catalog / "build/toolchain.json", tool_bytes)
        put(catalog / "build/probe.c", "fixture static probe\n")
        put(catalog / "build/probe.log", "fixture static probe passed\n")
        put(catalog / "BUILD.json", json.dumps({"schema": self.c.SCHEMA, "jobs": 2, "work": "/tmp/fixture",
            "flags": self.c.FLAGS, "configure": {n: self.c.configure_options(n, "/tmp/fixture") for n in self.c.PINS},
            "patches": {n: p["patches"] for n, p in self.c.PINS.items()}, "toolchain_sha256": self.c.digest(tool_bytes)}))
        put(catalog / "SOURCES.tsv", self.c.source_table(catalog))
        self.rehash(catalog)
        self.build_id = self.c.digest((catalog / "MATERIALS.sha256").read_bytes())
        self.catalog = catalog.rename(self.root / self.build_id)
        self.libraries = self.root / "libraries"
        shutil.copytree(self.catalog / "lib", self.libraries)

    def rehash(self, catalog):
        files = self.c.inventory(catalog)
        put(catalog / "MATERIALS.sha256", "".join(h + "  " + n + "\n" for n, h in sorted(files.items())
                                                  if n != "MATERIALS.sha256"))

    def validate(self, path=None):
        return self.c.validate(path or self.catalog, self.libraries, self.build_id)

    def test_complete_catalog_and_repeated_collection(self):
        self.assertEqual(self.validate(), self.build_id)
        for _ in range(2):
            with mock.patch.object(sys, "argv", ["catalog", "collect", "--catalog", str(self.catalog),
                 "--libraries", str(self.libraries), "--destination", str(self.root / "collected")]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.c.main()
        self.validate(self.root / "collected" / self.build_id)
        self.assertEqual(len(list((self.root / "collected").iterdir())), 1)

    def test_collection_requires_actual_linked_archives(self):
        with mock.patch.object(sys, "argv", ["catalog", "collect", "--catalog", str(self.catalog),
             "--destination", str(self.root / "collected")]):
            with self.assertRaisesRegex(ValueError, "requires linked archives"):
                self.c.main()
        self.assertFalse((self.root / "collected").exists())

    def test_changed_missing_material_even_with_recomputed_inventory(self):
        paths = ["sources/libgcrypt/libgcrypt.spec", "sources/libgcrypt/libgcrypt-1.10.2.tar.bz2",
                 "sources/libgcrypt/" + self.c.PINS["libgcrypt"]["patches"][2],
                 "sources/libgcrypt/" + self.c.srpm_name("libgcrypt"), "licenses/libgcrypt/LICENSES",
                 "recipe/static-crypto.py", "recipe/static-crypto-catalog.py", "lib/libgcrypt.a", "SOURCES.tsv"]
        for index, path in enumerate(paths):
            for mutation in ("alter", "remove"):
                with self.subTest(path=path, mutation=mutation):
                    candidate = self.root / f"mutated-{index}-{mutation}"
                    shutil.copytree(self.catalog, candidate)
                    if mutation == "alter":
                        with (candidate / path).open("ab") as stream:
                            stream.write(b"changed\n")
                    else:
                        (candidate / path).unlink()
                    self.rehash(candidate)
                    with self.assertRaises((ValueError, OSError)):
                        self.c.validate(candidate, self.libraries)

    def test_build_and_complete_inventory(self):
        for mutation in ("jobs", "configure", "patches", "toolchain", "missing-build", "unlisted", "extra"):
            candidate = self.root / mutation
            shutil.copytree(self.catalog, candidate)
            if mutation in ("jobs", "configure", "patches"):
                record = json.loads((candidate / "BUILD.json").read_bytes())
                if mutation == "jobs": record["jobs"] = 3
                elif mutation == "configure": record["configure"]["libgcrypt"].append("--enable-shared")
                else: record["patches"]["libgcrypt"].pop()
                put(candidate / "BUILD.json", json.dumps(record))
            elif mutation == "toolchain":
                put(candidate / "build/toolchain.json", "{}")
            elif mutation == "missing-build":
                (candidate / "build/libgcrypt/config.log").unlink()
            else:
                put(candidate / "extra", "unlisted data")
            if mutation != "unlisted": self.rehash(candidate)
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, OSError)):
                self.c.validate(candidate, self.libraries)

    def test_archive_and_marker_binding(self):
        put(self.libraries / "libgcrypt.a", b"!<arch>\nother archive")
        with self.assertRaises(ValueError): self.validate()
        shutil.copyfile(self.catalog / "lib/libgcrypt.a", self.libraries / "libgcrypt.a")
        put(self.catalog / "build/probe.log", "changed and rechecksummed")
        self.rehash(self.catalog)
        with self.assertRaisesRegex(ValueError, "build identity"): self.validate()

    def test_links_special_files_and_inventory_paths(self):
        for mutation in ("file-link", "dir-link", "hardlink", "fifo", "traversal", "absolute", "duplicate", "root-link", "empty-dir"):
            candidate = self.root / mutation
            shutil.copytree(self.catalog, candidate)
            target = candidate / "licenses/libgcrypt/COPYING"
            if mutation in ("file-link", "hardlink"):
                target.unlink()
                if mutation == "file-link": target.symlink_to(self.catalog / "licenses/libgcrypt/COPYING")
                else: os.link(self.catalog / "licenses/libgcrypt/COPYING", target)
            elif mutation == "dir-link":
                shutil.rmtree(candidate / "licenses")
                (candidate / "licenses").symlink_to(self.catalog / "licenses", target_is_directory=True)
            elif mutation == "fifo": os.mkfifo(candidate / "fifo")
            elif mutation == "empty-dir": (candidate / "empty").mkdir()
            elif mutation == "root-link":
                shutil.rmtree(candidate)
                candidate.symlink_to(self.catalog, target_is_directory=True)
            else:
                line = (candidate / "MATERIALS.sha256").read_text().splitlines()[0]
                if mutation == "traversal": line = "0" * 64 + "  ../outside"
                if mutation == "absolute": line = "0" * 64 + "  /etc/passwd"
                with (candidate / "MATERIALS.sha256").open("a") as stream: stream.write(line + "\n")
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, OSError)):
                self.c.validate(candidate, self.libraries)

    def test_tar_and_cpio_attacks_never_extract(self):
        pin = self.c.PINS["libgcrypt"]
        for name, kind in [("libgcrypt-1.10.2/../outside", tarfile.REGTYPE),
                           ("/outside", tarfile.REGTYPE), ("libgcrypt-1.10.2/link", tarfile.SYMTYPE),
                           ("libgcrypt-1.10.2/link", tarfile.LNKTYPE)]:
            data = tar_bytes([(name, b"x", kind)])
            pin["files"][pin["archive"]] = self.c.digest(data)
            with self.subTest(name=name, kind=kind), self.assertRaises(ValueError):
                self.c.source_tar("libgcrypt", data, self.root / "extract")
            self.assertFalse((self.root / "extract").exists())
        for entries in [[("../outside", b"x", stat.S_IFREG, 1)], [("/outside", b"x", stat.S_IFREG, 1)],
                        [("link", b"x", stat.S_IFLNK, 1)], [("hard", b"x", stat.S_IFREG, 2)],
                        [("same", b"x", stat.S_IFREG, 1)] * 2]:
            with self.assertRaises(ValueError): self.c.rpm_sources(rpm_bytes(entries))

    def test_release_claims_sources_and_notices(self):
        stage = self.root / "release"
        material = stage / "share/sources/runtime"
        shutil.copytree(self.catalog, material / "native-crypto" / self.build_id)
        rows = []
        for line in (self.catalog / "SOURCES.tsv").read_text().splitlines()[1:]:
            file, name, version, source, integrity, licenses = line.split("\t")
            destination = "share/licenses/runtime/system/" + file
            shutil.copytree(self.catalog / licenses, stage / destination)
            rows.append("bin/mkfs.erofs\tsystem:" + file + "\t" + version + "\t" + source + "\t" +
                        integrity + ";crypto-catalog:" + self.build_id + "\t" + destination + "\n")
        put(material / "SOURCES.tsv", self.c.HEADER + "".join(rows))
        self.c.validate_release(stage)
        put(stage / "share/licenses/runtime/system/libgcrypt.a/COPYING", "changed notice")
        with self.assertRaises(ValueError): self.c.validate_release(stage)
        shutil.copyfile(self.catalog / "licenses/libgcrypt/COPYING", stage / "share/licenses/runtime/system/libgcrypt.a/COPYING")
        put(material / "SOURCES.tsv", self.c.HEADER + rows[0])
        with self.assertRaises(ValueError): self.c.validate_release(stage)


if (HERE / "static-crypto.py").exists():
    class ProviderTests(CatalogTests):
        def setUp(self):
            super().setUp()
            self.p = load("provider_test", HERE / "static-crypto.py")
            self.p.C = self.c

        def test_publish_copy_and_warm_validation(self):
            template, slot = self.root / "template", self.root / "slot"
            put(template / "usr/include/gcrypt.h", "existing distro header")
            put(template / "usr/lib64/libgcrypt.so.20", "existing distro shared library")
            self.p.publish(self.catalog, template)
            self.assertEqual((template / "usr/include/gcrypt.h").read_text(), "existing distro header")
            self.assertEqual((template / "usr/lib64/libgcrypt.so.20").read_text(), "existing distro shared library")
            for _ in range(2):
                self.p.publish(self.c.installed(template), slot)
                self.c.installed(slot)
            args = type("Args", (), {"root": template, "host_build": True})()
            tools = (self.catalog / "build/toolchain.json").read_bytes()
            with mock.patch.object(self.p, "invoke", return_value=tools), mock.patch.object(self.p, "obtain") as obtain:
                with contextlib.redirect_stdout(io.StringIO()): self.p.install(args)
                obtain.assert_not_called()
            installed = self.c.installed(template)
            put(installed / "licenses/libgcrypt/COPYING", "damaged notice")
            with self.assertRaises(ValueError): self.c.installed(template)
            with self.assertRaises(ValueError): self.p.publish(installed, slot)

        def test_checksum_failure_does_not_publish(self):
            sources = self.root / "sources"
            for name in self.c.PINS:
                put(sources / self.c.srpm_name(name),
                    (self.catalog / "sources" / name / self.c.srpm_name(name)).read_bytes())
            bad = sources / self.c.srpm_name("libgcrypt")
            put(bad, b"wrong source")
            target = self.root / "failed-install"
            args = type("Args", (), {"root": target, "host_build": True, "sources": sources,
                                     "download": False, "jobs": 2})()
            tools = (self.catalog / "build/toolchain.json").read_bytes()
            with mock.patch.object(self.p, "invoke", return_value=tools):
                with self.assertRaisesRegex(ValueError, "checksum"):
                    self.p.install(args)
            self.assertEqual(bad.read_bytes(), b"wrong source")
            self.assertFalse((target / "usr/lib64").exists())

        def test_real_patch_failure_does_not_publish(self):
            name = "libgcrypt"
            materials = self.catalog / "sources" / name
            patch = self.c.PINS[name]["patches"][0]
            bad = b"--- a/input\n+++ b/input\n@@ -1 +1 @@\n-not the source\n+after\n"
            put(materials / patch, bad)
            self.c.PINS[name]["files"][patch] = self.c.digest(bad)
            source = self.root / "prepare"
            source.mkdir()
            with self.assertRaises(subprocess.CalledProcessError):
                self.p.prepare(name, source, materials, self.root / "patch.log", self.p.environment(self.root))
            self.assertEqual((source / "input").read_bytes(), b"before\n")
            self.assertFalse((self.root / "usr/lib64").exists())

        def test_install_rolls_back_when_archive_rename_fails(self):
            root = self.root / "rollback"
            self.p.publish(self.catalog, root)
            original = self.c.inventory(root)
            replace = os.replace
            def fail_second(source, destination):
                if Path(source).name == "libgcrypt.a": raise OSError("fixture rename failure")
                return replace(source, destination)
            with mock.patch.object(self.p.os, "replace", side_effect=fail_second), self.assertRaises(OSError):
                self.p.publish(self.catalog, root)
            self.assertEqual(original, self.c.inventory(root))
            self.c.installed(root)


if __name__ == "__main__":
    unittest.main()
