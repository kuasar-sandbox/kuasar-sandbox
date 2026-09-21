#!/usr/bin/env python3
"""The two pinned openEuler crypto source catalogs (also shipped by guest-runtime).

This is deliberately not an RPM spec interpreter. Pins and preparation are reviewed
together. No catalog-supplied program, path, URL or checksum is a trust anchor.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import tarfile

SCHEMA = "kuasar-static-crypto-v1"
PROVIDER_SHA256 = "37f334764ead9912c1cae96435d89adc176acd99669b0886abf0fa31ec1d3673"
BASE_URL = "https://mirrors.huaweicloud.com/openeuler/openEuler-24.03-LTS-SP4/source/Packages/"
PINS = {
    "libgpg-error": {
        "version": "1.47-1.oe2403sp4", "upstream": "1.47",
        "srpm": "cb75e6c3ae8d4d5d13eb621106c4ad9e3a1f0670959d3a277870790e6083f499",
        "archive": "libgpg-error-1.47.tar.gz",
        "files": {
            "libgpg-error-1.47.tar.gz": "685d4bd9d05576c4fc7f0870903dfdfbe41f2dd6a12e76fd8bd1717278f6b365",
            "libgpg-error-1.47.tar.gz.sig": "cbb7038da4cad7262696baf1ca6479f37b9a4ee08b70fe129be1bc8501eb8939",
            "libgpg-error.spec": "fc14c92db697d5a6cf882f83970b05219448515c75f6c18f6449b43eb7688a57",
            "libgpg-error-1.29-multilib.patch": "02eb8379dad773efc597a9cce47cd5ce9b019f503043fed3569b1cbf74db1016",
        },
        "patches": ["libgpg-error-1.29-multilib.patch"],
        "notices": ["AUTHORS", "COPYING", "COPYING.LIB", "THANKS"],
        "headers": ["gpg-error.h", "gpgrt.h"],
    },
    "libgcrypt": {
        "version": "1.10.2-4.oe2403sp4", "upstream": "1.10.2",
        "srpm": "074decf4fb34ddadbc1e7498140bfb6dbf7d24f0ed9b85fe9ec7f5c9540ac984",
        "archive": "libgcrypt-1.10.2.tar.bz2",
        "files": {
            "libgcrypt-1.10.2.tar.bz2": "3b9c02a004b68c256add99701de00b383accccf37177e0d6c58289664cce0c03",
            "libgcrypt.spec": "cf6240e5c7b28a7f2a21121f55cdbc20e159d57e1700102c776aca4daadc2364",
            "random.conf": "639bd7d4df19f8e810433e7158f2e2c0b8d8034b9276562f255dd13b108403e5",
            "Use-the-compiler-switch-O0-for-compiling-jitterentro.patch": "01a4ebfe4fbdc931a539bdfb9a8fd37fc54a298f1ef8a410c6f2be988e665ad7",
            "add-GCRY_MD_SM3_PGP-set-to-109.patch": "5d988d4f166d4e05de95a6887e82da39f678627c07224f7e86fcb16e231aa512",
            "backport-CVE-2026-41989-cipher-ecc-Fix-decoding-a-point-on-Montgomery-curve.patch": "80d20d21272f6c7e1113fe73b39099b3485e4d46a0030b2d83663984ca8cf821",
        },
        "patches": [
            "Use-the-compiler-switch-O0-for-compiling-jitterentro.patch",
            "add-GCRY_MD_SM3_PGP-set-to-109.patch",
            "backport-CVE-2026-41989-cipher-ecc-Fix-decoding-a-point-on-Montgomery-curve.patch",
        ],
        "notices": ["AUTHORS", "COPYING", "COPYING.LIB", "LICENSES", "THANKS"],
        "headers": ["gcrypt.h"],
    },
}
BUILD_FILES = ["prepare.log", "configure.log", "make.log", "config.log", "config.status",
               "config.h", "Makefile", "src/Makefile"]
HEADER = "payload\tname\tversion\tsource\tintegrity\tlicense_directory\n"
FLAGS = {"CFLAGS": "-O2 -g0 -fPIC", "CPPFLAGS": "", "LDFLAGS": "",
         "LC_ALL": "C", "TZ": "UTC", "SOURCE_DATE_EPOCH": "0"}
TOOLS = ["gcc", "ar", "ranlib", "ld", "as", "make", "patch", "autoconf", "autoheader", "m4", "bash"]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_relative(name):
    require(re.fullmatch(r"[A-Za-z0-9._+~@/-]+", name) and
            all(p not in ("", ".", "..") for p in name.split("/")) and
            not name.startswith("/"), "unsafe material path: " + name)
    return name


def no_links(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        require(not part.is_symlink(), "symbolic link in path: " + str(part))
    return path


def regular(path):
    path = no_links(path)
    require(stat.S_ISREG(path.stat().st_mode) and path.stat().st_nlink == 1,
            "not a private regular file: " + str(path))
    return path.read_bytes()


def inventory(directory):
    directory = no_links(directory)
    require(directory.is_dir(), "missing catalog directory")
    files, directories = {}, set()
    for root, dirs, names in os.walk(directory, followlinks=False):
        for name in dirs + names:
            path = Path(root) / name
            relative = safe_relative(path.relative_to(directory).as_posix())
            mode = path.lstat().st_mode
            require(stat.S_ISDIR(mode) or stat.S_ISREG(mode), "unsafe catalog entry: " + relative)
            if stat.S_ISREG(mode):
                files[relative] = digest(regular(path))
            else:
                directories.add(relative)
    expected_dirs = {p.as_posix() for name in files for p in PurePosixPath(name).parents if p.as_posix() != "."}
    require(directories == expected_dirs, "unlisted empty material directory")
    return files


def srpm_name(name):
    return name + "-" + PINS[name]["version"] + ".src.rpm"


def rpm_sources(data):
    """Read only the pinned RPM v3 lead / v1 headers / gzip newc source payload."""
    require(data[:4] == b"\xed\xab\xee\xdb", "invalid RPM lead")
    pos = 96
    for index in range(2):
        require(data[pos:pos + 4] == b"\x8e\xad\xe8\x01", "invalid RPM header")
        count, size = struct.unpack(">II", data[pos + 8:pos + 16])
        pos += 16 + count * 16 + size
        require(pos <= len(data), "truncated RPM header")
        if index == 0:
            pos = (pos + 7) // 8 * 8
    payload = gzip.decompress(data[pos:])
    pos, result = 0, {}
    while True:
        header = payload[pos:pos + 110]
        require(len(header) == 110 and header[:6] == b"070701", "invalid source cpio")
        fields = [int(header[i:i + 8], 16) for i in range(6, 110, 8)]
        size, length = fields[6], fields[11]
        raw_name = payload[pos + 110:pos + 110 + length]
        require(length > 1 and raw_name.endswith(b"\0"), "invalid cpio name")
        name = raw_name[:-1].decode("ascii")
        pos = (pos + 110 + length + 3) // 4 * 4
        body = payload[pos:pos + size]
        require(len(body) == size, "truncated source cpio")
        pos = (pos + size + 3) // 4 * 4
        if name == "TRAILER!!!":
            require(size == 0 and not payload[pos:].strip(b"\0"), "invalid cpio trailer")
            break
        safe_relative(name)
        require("/" not in name and name not in result and stat.S_ISREG(fields[1]) and
                fields[4] == 1, "unsafe or duplicate source cpio member")
        result[name] = body
    return result


def verified_sources(name, data):
    pin = PINS[name]
    require(digest(data) == pin["srpm"], "source RPM checksum mismatch: " + name)
    files = rpm_sources(data)
    require({n: digest(b) for n, b in files.items()} == pin["files"],
            "contained source/spec/patch checksum mismatch: " + name)
    return files


def source_tar(name, data, destination=None):
    """Validate every member before writing. These exact archives contain no links."""
    prefix = name + "-" + PINS[name]["upstream"]
    require(digest(data) == PINS[name]["files"][PINS[name]["archive"]], "tarball checksum mismatch")
    notices, seen = {}, set()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = archive.getmembers()
        for item in members:
            member = item.name.rstrip("/")
            safe_relative(member)
            require(member == prefix or member.startswith(prefix + "/"), "wrong source directory")
            require(member not in seen and (item.isfile() or item.isdir()), "unsafe source tar member")
            seen.add(member)
        for item in members:
            parts = PurePosixPath(item.name).parts[1:]
            if not parts:
                continue
            relative = "/".join(parts)
            body = archive.extractfile(item).read() if item.isfile() else None
            if relative in PINS[name]["notices"]:
                require(body, "empty source notice")
                notices[relative] = body
            if destination is not None:
                target = no_links(Path(destination) / relative)
                if item.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as output:
                        output.write(body)
                    target.chmod(0o755 if item.mode & 0o111 else 0o644)
                os.utime(target, (0, 0))
    require(set(notices) == set(PINS[name]["notices"]), "missing source notices")
    return notices


def configure_options(name, work):
    dependency = work + "/dependency"
    common = ["--enable-static", "--disable-shared", "--disable-doc"]
    if name == "libgpg-error":
        return ["--prefix=" + dependency, "--libdir=" + dependency + "/lib", *common,
                "--disable-nls", "--disable-rpath", "--disable-languages", "--enable-install-gpg-error-config"]
    return ["--prefix=/usr", "--libdir=/usr/lib64", *common, "--enable-noexecstack",
            "--disable-hmac-binary-check", "--enable-pubkey-ciphers=dsa elgamal rsa ecc",
            "--disable-O-flag-munging", "--with-libgpg-error-prefix=" + dependency]


def source_table(catalog):
    result = HEADER
    for name, pin in PINS.items():
        archive_hash = digest(regular(catalog / "lib" / (name + ".a")))
        result += (f"{name}.a\t{name}\t{pin['version']}\t{BASE_URL}{srpm_name(name)}\t"
                   f"sha256:{archive_hash};srpm-sha256:{pin['srpm']};"
                   f"tarball-sha256:{pin['files'][pin['archive']]}\tlicenses/{name}\n")
    return result


def validate(catalog, libraries=None, build_id=None):
    catalog = no_links(catalog)
    files = inventory(catalog)
    manifest = regular(catalog / "MATERIALS.sha256")
    if build_id is not None:
        require(re.fullmatch(r"[0-9a-f]{64}", build_id) and digest(manifest) == build_id,
                "catalog build identity mismatch")
    expected = {}
    for line in manifest.decode("ascii").splitlines():
        require(re.fullmatch(r"[0-9a-f]{64}  [A-Za-z0-9._+/-]+", line), "invalid catalog inventory")
        value, path = line.split("  ")
        safe_relative(path)
        require(path not in expected and path != "MATERIALS.sha256", "duplicate catalog inventory")
        expected[path] = value
    require(expected == {n: h for n, h in files.items() if n != "MATERIALS.sha256"},
            "catalog inventory/checksum mismatch")
    required = {"MATERIALS.sha256", "SOURCES.tsv", "BUILD.json", "recipe/static-crypto.py",
                "recipe/static-crypto-catalog.py", "build/toolchain.json", "build/probe.c", "build/probe.log"}
    require(digest(regular(catalog / "recipe/static-crypto.py")) == PROVIDER_SHA256,
            "unrecognized crypto build recipe")
    require(regular(catalog / "recipe/static-crypto-catalog.py") == Path(__file__).read_bytes(),
            "unrecognized crypto catalog recipe")
    build = json.loads(regular(catalog / "BUILD.json"))
    require(set(build) == {"schema", "jobs", "work", "flags", "configure", "patches", "toolchain_sha256"},
            "invalid crypto build record")
    require(build["schema"] == SCHEMA and type(build["jobs"]) is int and 1 <= build["jobs"] <= 2 and
            build["flags"] == FLAGS and isinstance(build["work"], str) and
            build["work"].startswith("/") and "\n" not in build["work"], "invalid crypto build options")
    require(build["configure"] == {n: configure_options(n, build["work"]) for n in PINS} and
            build["patches"] == {n: p["patches"] for n, p in PINS.items()}, "crypto source preparation mismatch")
    tools_data = regular(catalog / "build/toolchain.json")
    require(digest(tools_data) == build["toolchain_sha256"], "toolchain record mismatch")
    tools = json.loads(tools_data)
    require(set(tools) == {*TOOLS, "cc1", "collect2", "target"} and
            isinstance(tools["target"], str) and tools["target"], "incomplete toolchain identity")
    for name, tool in tools.items():
        if name != "target":
            require(set(tool) == {"path", "sha256", "version"} and tool["path"].startswith("/") and
                    re.fullmatch(r"[0-9a-f]{64}", tool["sha256"]) and tool["version"], "invalid tool identity: " + name)
    for name, pin in PINS.items():
        source = catalog / "sources" / name
        source_files = verified_sources(name, regular(source / srpm_name(name)))
        required.add("sources/" + name + "/" + srpm_name(name))
        for filename, body in source_files.items():
            require(regular(source / filename) == body, "source material differs from pinned SRPM")
            required.add("sources/" + name + "/" + filename)
        notices = source_tar(name, source_files[pin["archive"]])
        for filename, body in notices.items():
            require(regular(catalog / "licenses" / name / filename) == body, "source notice mismatch")
            required.add("licenses/" + name + "/" + filename)
        required.add("lib/" + name + ".a")
        archive = regular(catalog / "lib" / (name + ".a"))
        require(archive.startswith(b"!<arch>\n") and len(archive) > 8, "invalid static archive")
        if libraries is not None:
            require(regular(Path(libraries) / (name + ".a")) == archive, "installed archive differs from catalog")
        for filename in BUILD_FILES + ["src/" + h for h in pin["headers"]]:
            path = "build/" + name + "/" + filename
            require(regular(catalog / path), "empty build material: " + path)
            required.add(path)
    require(set(files) == required, "incomplete or unexpected crypto material set")
    require(regular(catalog / "SOURCES.tsv").decode() == source_table(catalog), "source/archive identity mismatch")
    return digest(manifest)


def installed(root):
    root = no_links(root)
    library_dir = root / "usr/lib64"
    build_id = regular(library_dir / ".kuasar-crypto-build-id").decode().strip()
    require(re.fullmatch(r"[0-9a-f]{64}", build_id), "invalid installed crypto build identity")
    catalog = root / "usr/share/kuasar-ci/native-crypto" / build_id
    validate(catalog, library_dir, build_id)
    return catalog


def validate_release(root):
    root = no_links(root)
    source = root / "share/sources/runtime"
    claims = {}
    for line in regular(source / "SOURCES.tsv").decode().splitlines():
        row = line.split("\t")
        if len(row) != 6 or row[0] == "payload":
            continue
        parts = row[4].split(";")
        identifiers = [p[len("crypto-catalog:"): ] for p in parts if p.startswith("crypto-catalog:")]
        if not identifiers:
            continue
        require(len(identifiers) == 1 and re.fullmatch(r"[0-9a-f]{64}", identifiers[0]), "invalid release crypto catalog claim")
        build_id = identifiers[0]
        catalog = source / "native-crypto" / build_id
        validate(catalog, build_id=build_id)
        expected = {}
        for record in regular(catalog / "SOURCES.tsv").decode().splitlines()[1:]:
            fields = record.split("\t")
            expected["system:" + fields[0]] = fields
        require(row[1] in expected, "unknown crypto archive claim")
        fields = expected[row[1]]
        require(row[2:5] == [fields[2], fields[3], fields[4] + ";crypto-catalog:" + build_id] and
                row[5] == "share/licenses/runtime/system/" + fields[0], "release crypto source identity mismatch")
        license_dir = root / row[5]
        require(inventory(license_dir) == inventory(catalog / fields[5]), "release crypto notice inventory mismatch")
        claims.setdefault(build_id, set()).add(fields[0])
    catalogs = source / "native-crypto"
    if catalogs.exists() or catalogs.is_symlink():
        no_links(catalogs)
        require(catalogs.is_dir() and {p.name for p in catalogs.iterdir()} == set(claims), "unclaimed crypto source material")
    for names in claims.values():
        require(names == {n + ".a" for n in PINS}, "incomplete crypto source claims")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "installed", "collect", "release"])
    parser.add_argument("--root", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--libraries", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.command == "release":
        validate_release(args.root)
    elif args.command == "installed":
        print(installed(args.root))
    else:
        if args.command == "collect":
            require(args.libraries is not None and args.destination is not None,
                    "collection requires linked archives and a destination")
        build_id = validate(args.catalog, args.libraries, args.catalog.name)
        if args.command == "collect":
            destination = no_links(args.destination / build_id)
            if destination.exists():
                validate(destination, args.libraries, build_id)
            else:
                shutil.copytree(args.catalog, destination)
                validate(destination, args.libraries, build_id)
        print(build_id)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, struct.error, tarfile.TarError) as error:
        raise SystemExit("static-crypto: " + str(error))
