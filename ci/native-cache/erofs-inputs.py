#!/usr/bin/env python3
"""Bounded EROFS source, compiler and actual static-link input identities."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


def emit(label, value):
    print(json.dumps([label, value], separators=(",", ":")))


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(arguments):
    return subprocess.check_output(arguments, text=True, stderr=subprocess.PIPE).strip()


def config_site_paths(environment):
    """Match Autoconf: an empty CONFIG_SITE also selects both defaults."""
    return (environment.get("CONFIG_SITE") or "/usr/local/share/config.site /usr/local/etc/config.site").split()


def main():
    root = Path(sys.argv[1]).resolve()
    cross = sys.argv[2]
    native = root / "guest-runtime/native-deps"
    recipe = (native / "deps/build-erofs.sh").read_text()
    makefile = (native / "Makefile").read_text()
    emit("erofs-input-schema", 1)

    def file(path, label=None):
        path = Path(os.path.abspath(path))
        name = "workspace/" + str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
        emit(label or name, digest(path))

    def setting(name):
        if name in os.environ:
            return os.environ[name]
        match = re.search(r"^" + name + r"\s*\?=\s*(.*)$", makefile, re.M)
        return match[1].strip().replace("\\#", "#") if match else ""

    source, expected = setting("EROFS_TARBALL"), setting("EROFS_TARBALL_SHA256")
    emit("source/expected-sha256", expected)
    if source.startswith(("https://", "http://")):
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("unpinned EROFS URL cannot authorize a shared cache; use a local archive or SHA256 pin")
        emit("source/archive", expected)
    elif source:
        path = Path(source)
        if not path.is_absolute():
            path = native / path  # make -C native-deps uses this working directory
        actual = digest(path)
        if expected and actual != expected:
            raise ValueError("local EROFS archive does not match EROFS_TARBALL_SHA256")
        emit("source/archive", actual)

    # Match build-erofs.sh's splitting and preserve flag semantics. Only file
    # labels/source locators are relocatable; compiler macro/path values are not.
    names = "CC CXX CPP AR RANLIB STRIP LD NM AS OBJDUMP OBJCOPY READELF CFLAGS CXXFLAGS CPPFLAGS LDFLAGS LIBS ARFLAGS CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH COMPILER_PATH GCC_EXEC_PREFIX CONFIG_SITE CONFIG_SHELL MAX_BLOCK_SIZE SOURCE_DATE_EPOCH LANG LC_ALL AUTOCONF AUTOHEADER AUTOMAKE ACLOCAL ACLOCAL_PATH AUTOM4TE M4 LIBTOOLIZE libuuid_CFLAGS libuuid_LIBS".split()
    names += [n for n in os.environ if n.startswith("PKG_CONFIG") or "_cv_" in n]
    for name in sorted(set(names)):
        emit("env/" + name, os.environ.get(name))
    cc = [cross + "gcc"] if cross else os.environ.get("CC", "gcc").split()

    def tool(label, command):
        words = command.split()
        emit("tool/" + label, command)
        for i, word in enumerate(words):
            path = shutil.which(word)
            if path and Path(path).is_file():
                file(path, "tool/" + label + "/" + str(i))

    for name in "gcc g++ ar ranlib strip ld nm as objdump objcopy readelf make patch tar bash sh autoreconf autoconf autoheader automake aclocal autom4te m4 libtoolize".split():
        command = os.environ.get(name.upper(), name)
        if name == "gcc":
            command = " ".join(cc)
        elif name == "g++":
            command = cross + name if cross else os.environ.get("CXX", name)
        elif name == "nm":
            command = os.environ.get("NM", cross + name)
        elif name in ("ar", "ranlib", "strip") and cross:
            command = cross + name
        tool(name, command)
    for program in ("cc1", "collect2", "lto1", "ld", "as"):
        tool("compiler/" + program, run(cc + ["-print-prog-name=" + program]))
    for site in config_site_paths(os.environ):
        if Path(site).is_file():
            file(site)

    # Old test/source shapes without an archive declaration retain their
    # existing metadata identity. Real pinned guest recipes supply the source.
    if not source:
        return
    pkg = os.environ.get("PKG_CONFIG", "pkg-config").split()
    tool("pkg-config", " ".join(pkg))
    modules = ["uuid"]
    includes = "#include <uuid/uuid.h>\n"
    body = "uuid_t u; uuid_clear(u);\n"
    if "libgcrypt" in recipe:
        modules += ["libgcrypt", "gpg-error"]
        includes += "#include <gcrypt.h>\n"
        body += 'unsigned char d[32]; gcry_check_version(GCRYPT_VERSION); gcry_md_hash_buffer(GCRY_MD_SHA256, d, "abc", 3);\n'
    elif "--with-openssl" in recipe:
        modules += ["openssl"]
        includes += "#include <openssl/evp.h>\n#include <openssl/ssl.h>\n"
        body += 'unsigned char d[64]; unsigned int n; EVP_Digest("abc", 3, d, &n, EVP_sha256(), 0); SSL_CTX_free(SSL_CTX_new(TLS_method()));\n'
    flags, libraries = [], []
    for module in modules:
        for option in (["--modversion"], ["--cflags"], ["--libs", "--static"]):
            emit("pkg/" + module + "/" + " ".join(option), run(pkg + option + [module]))
        directory = run(pkg + ["--variable=pcfiledir", module])
        file(Path(directory) / (module + ".pc"), "pkg/" + module + ".pc")
        flags += (os.environ["libuuid_CFLAGS"] if module == "uuid" and "libuuid_CFLAGS" in os.environ else run(pkg + ["--cflags", module])).split()
        libraries += (os.environ["libuuid_LIBS"] if module == "uuid" and "libuuid_LIBS" in os.environ else run(pkg + ["--libs", "--static", module])).split()
    # -MD includes system headers and forced includes. The linker chooses -L,
    # LIBRARY_PATH, sysroot, startup objects and explicit LIBS exactly as usual.
    with tempfile.TemporaryDirectory(prefix="erofs-inputs-") as directory:
        work = Path(directory)
        (work / "probe.c").write_text(includes + "int main(void) {\n" + body + "return 0; }\n")
        cflags = os.environ.get("CFLAGS", "").split()
        subprocess.run(cc + os.environ.get("CPPFLAGS", "").split() + cflags + flags +
                       ["-MD", "-MF", str(work / "probe.d"), "-c", str(work / "probe.c"), "-o", str(work / "probe.o")], cwd=native, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(cc + cflags + [str(work / "probe.o")] + os.environ.get("LDFLAGS", "").split() +
                       ["-static", "-Wl,-Map," + str(work / "probe.map")] + libraries + os.environ.get("LIBS", "").split() +
                       ["-o", str(work / "probe")], cwd=native, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        dependencies = (work / "probe.d").read_text().replace("\\\n", "").split(":", 1)[1]
        paths = set(shlex.split(dependencies.replace("$$", "$")))
        paths.update(line[5:].strip() for line in (work / "probe.map").read_text().splitlines() if line.startswith("LOAD "))
        for name in sorted(paths):
            path = Path(os.path.abspath(native / name))
            if not path.is_relative_to(work):
                file(path)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", "") or ""
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        sys.exit("native-cache: EROFS input preflight failed: " + str(error) + "\n" + detail)
