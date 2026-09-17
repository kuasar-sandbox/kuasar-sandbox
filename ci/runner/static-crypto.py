#!/usr/bin/env python3
"""Build only the two pinned crypto static archives; never install shared libraries.

The normal caller builds in an existing install root. --host-build is solely for
verification in a disposable prefix with the local compiler. No RPM registration,
package installation, service changes or runner operations are performed here.
"""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = Path(__file__).absolute().parent
SPEC = importlib.util.spec_from_file_location("crypto_catalog", HERE / "static-crypto-catalog.py")
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    path.chmod(0o644)


def encoded(data):
    return (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()


def environment(work):
    return {"PATH": "/usr/bin:/bin", "TMPDIR": str(work), **C.FLAGS}


def run(command, cwd, log, env):
    with log.open("ab") as output:
        output.write(("$ " + shlex.join(map(str, command)) + "\n").encode())
        output.flush()
        subprocess.run(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, check=True)


def toolchain(work):
    env = environment(work)
    result = {}
    for name in C.TOOLS + ["cc1", "collect2"]:
        if name in ("cc1", "collect2"):
            path = subprocess.check_output(["gcc", "-print-prog-name=" + name], env=env, text=True).strip()
        else:
            path = shutil.which(name, path=env["PATH"])
        C.require(path, "missing build tool: " + name)
        path = Path(path).resolve(strict=True)
        options = ["-version", "-fsyntax-only"] if name == "cc1" else ["--version"]
        version = subprocess.check_output([str(path), *options], input="", env=env, stderr=subprocess.STDOUT, text=True)
        if name == "cc1":
            # cc1 also emits per-run heap/timing statistics after its version.
            version = version.splitlines()[0]
        result[name] = {"path": str(path), "sha256": C.digest(path.read_bytes()), "version": version.strip()}
    result["target"] = subprocess.check_output(["gcc", "-dumpmachine"], env=env, text=True).strip()
    return result


def prepare(name, source, materials, log, env):
    pin = C.PINS[name]
    C.source_tar(name, C.regular(materials / pin["archive"]), source)
    for patch in pin["patches"]:
        C.require(C.digest(C.regular(materials / patch)) == pin["files"][patch], "patch checksum mismatch")
        run(["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-i", str(materials / patch)], source, log, env)
    # The patches change configure.ac/config.h.in, not Makefile.am. Regenerate
    # those two files directly using the tarball's complete m4 inputs. This
    # avoids autoreconf replacing bundled gettext/libtool support files.
    for command in (["autoconf", "-f"], ["autoheader", "-f"]):
        run(command, source, log, env)
    if name == "libgpg-error":
        # Exact post-autoreconf edits in the pinned %prep, with _libdir=/usr/lib64.
        path = source / "src/gpg-error-config.in"
        data = path.read_text()
        C.require("libdir=@libdir@" in data and "@GPG_ERROR_CONFIG_HOST@" in data, "gpg-error config prep mismatch")
        path.write_text(data.replace("libdir=@libdir@", "libdir=@exec_prefix@/lib").replace("@GPG_ERROR_CONFIG_HOST@", "none"))
        path = source / "src/gpg-error-config-test.sh.in"
        data = path.read_text()
        C.require("--variable=host" in data, "gpg-error config test prep mismatch")
        path.write_text("".join(line for line in data.splitlines(keepends=True) if "--variable=host" not in line))
        path = source / "configure"
        data = path.read_text()
        old = 'sys_lib_dlsearch_path_spec="/lib /usr/lib'
        C.require(old in data, "gpg-error configure prep mismatch")
        path.write_text(data.replace(old, old + " /usr/lib64"))
        with log.open("a") as output:
            output.write("Applied pinned libgpg-error %prep config/config-test/configure substitutions.\n")


PROBE = r'''#include <gcrypt.h>
#include <string.h>
int main(void) {
  static const unsigned char expected[32] = {
    0xe3,0xb0,0xc4,0x42,0x98,0xfc,0x1c,0x14,0x9a,0xfb,0xf4,0xc8,0x99,0x6f,0xb9,0x24,
    0x27,0xae,0x41,0xe4,0x64,0x9b,0x93,0x4c,0xa4,0x95,0x99,0x1b,0x78,0x52,0xb8,0x55};
  unsigned char actual[32];
  if (!gcry_check_version("1.10.2")) return 1;
  if (gcry_control(GCRYCTL_DISABLE_SECMEM, 0)) return 2;
  if (gcry_control(GCRYCTL_INITIALIZATION_FINISHED, 0)) return 3;
  gcry_md_hash_buffer(GCRY_MD_SHA256, actual, "", 0);
  return memcmp(actual, expected, sizeof actual) != 0;
}
'''


def build(work, jobs):
    catalog = work / "catalog"
    env = environment(work)
    # Recheck every source before executing any of it.
    for name in C.PINS:
        materials = catalog / "sources" / name
        files = C.verified_sources(name, C.regular(materials / C.srpm_name(name)))
        for filename, body in files.items():
            C.require(C.regular(materials / filename) == body, "source preparation input changed")
    for name, pin in C.PINS.items():
        source = work / "src" / name
        source.mkdir(parents=True)
        record = catalog / "build" / name
        record.mkdir(parents=True)
        prepare(name, source, catalog / "sources" / name, record / "prepare.log", env)
        run(["./configure", *C.configure_options(name, str(work))], source, record / "configure.log", env)
        if name == "libgcrypt":
            path = source / "libtool"
            data = path.read_text()
            # Preserve the pinned spec's post-configure search-path adjustment.
            data = "".join(line.replace("/lib /usr/lib", "/usr/lib /lib64 /usr/lib64 /lib")
                           if line.startswith("sys_lib_dlsearch_path_spec") else line
                           for line in data.splitlines(keepends=True))
            path.write_text(data)
        run(["make", "-j" + str(jobs)], source, record / "make.log", env)
        if name == "libgpg-error":
            # A private dependency prefix supplies headers/config scripts to
            # Libgcrypt. None of these files are installed into the runner root.
            run(["make", "-j" + str(jobs), "-C", "src", "install"], source, record / "make.log", env)
        write(catalog / "lib" / (name + ".a"), (source / "src/.libs" / (name + ".a")).read_bytes())
        for filename in C.BUILD_FILES[3:] + ["src/" + h for h in pin["headers"]]:
            write(record / filename, (source / filename).read_bytes())
    write(catalog / "build/probe.c", PROBE)
    run(["gcc", "-static", "-I" + str(work / "src/libgcrypt/src"),
         "-I" + str(work / "src/libgpg-error/src"), str(catalog / "build/probe.c"),
         str(catalog / "lib/libgcrypt.a"), str(catalog / "lib/libgpg-error.a"),
         "-o", str(work / "probe")], work, catalog / "build/probe.log", env)
    run([str(work / "probe")], work, catalog / "build/probe.log", env)
    tools = C.regular(catalog / "build/toolchain.json")
    write(catalog / "BUILD.json", encoded({
        "schema": C.SCHEMA, "jobs": jobs, "work": str(work), "flags": C.FLAGS,
        "configure": {n: C.configure_options(n, str(work)) for n in C.PINS},
        "patches": {n: p["patches"] for n, p in C.PINS.items()}, "toolchain_sha256": C.digest(tools),
    }))
    write(catalog / "SOURCES.tsv", C.source_table(catalog))
    files = C.inventory(catalog)
    write(catalog / "MATERIALS.sha256", "".join(h + "  " + n + "\n" for n, h in sorted(files.items())))
    C.validate(catalog)


def publish(catalog, root):
    """Stage and validate before publication; marker last, rollback on errors.

    A process crash between archive renames leaves no valid marker. Subsequent
    reuse/collection fails closed until the next successful install. Slots are
    stopped by the enclosing provisioner while their libraries are reconciled.
    """
    build_id = C.validate(catalog)
    parent = C.no_links(root / "usr/share/kuasar-ci/native-crypto")
    libraries = C.no_links(root / "usr/lib64")
    parent.mkdir(parents=True, exist_ok=True)
    libraries.mkdir(parents=True, exist_ok=True)
    destination = C.no_links(parent / build_id)
    marker = C.no_links(libraries / ".kuasar-crypto-build-id")
    for name in C.PINS:
        target = C.no_links(libraries / (name + ".a"))
        if target.exists():
            C.regular(target)
    if destination.exists():
        C.validate(destination, build_id=build_id)
    else:
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=parent) as staging:
            staged = Path(staging) / "catalog"
            shutil.copytree(catalog, staged)
            C.validate(staged, build_id=build_id)
            staged.rename(destination)
    with tempfile.TemporaryDirectory(prefix=".crypto-", dir=libraries) as temporary:
        stage = Path(temporary)
        names = [n + ".a" for n in C.PINS] + [marker.name]
        existed = {}
        for name in names:
            target = libraries / name
            existed[name] = target.exists()
            if existed[name]:
                write(stage / (name + ".old"), C.regular(target))
        for name in C.PINS:
            write(stage / (name + ".a"), C.regular(destination / "lib" / (name + ".a")))
        write(stage / marker.name, build_id + "\n")
        try:
            marker.unlink(missing_ok=True)
            for name in C.PINS:
                os.replace(stage / (name + ".a"), libraries / (name + ".a"))
            C.validate(destination, libraries, build_id)
            os.replace(stage / marker.name, marker)
        except BaseException:
            for name in names:
                if existed[name]:
                    os.replace(stage / (name + ".old"), libraries / name)
                else:
                    (libraries / name).unlink(missing_ok=True)
            raise
    return destination


def obtain(sources, name, download):
    sources = C.no_links(sources)
    path = C.no_links(sources / C.srpm_name(name))
    if not path.exists():
        C.require(download, "missing pinned source RPM: " + str(path))
        sources.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".crypto-download-", dir=sources) as temp:
            candidate = Path(temp) / path.name
            subprocess.run(["curl", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https",
                            "--retry", "3", "--connect-timeout", "10", "--max-time", "180",
                            "--output", str(candidate), C.BASE_URL + path.name], check=True)
            C.verified_sources(name, C.regular(candidate))
            candidate.chmod(0o644)
            os.replace(candidate, path)
    data = C.regular(path)
    return data, C.verified_sources(name, data)


def invoke(work, root, host_build, command, *arguments):
    inner = work if host_build else Path("/") / work.relative_to(root)
    argv = ["/usr/bin/python3", str(inner / "recipe/static-crypto.py"), command, "--work", str(inner), *arguments]
    if not host_build:
        argv = ["chroot", str(root), *argv]
    return subprocess.check_output(argv, stderr=subprocess.STDOUT)


def install(args):
    root = C.no_links(args.root)
    C.require(root != Path("/"), "use an install root or disposable prefix, not the live filesystem")
    C.require(C.digest(Path(__file__).read_bytes()) == C.PROVIDER_SHA256, "provider recipe pin mismatch")
    temporary = C.no_links(root / "tmp")
    temporary.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kuasar-static-crypto-", dir=temporary) as name:
        work = Path(name)
        for filename in ("static-crypto.py", "static-crypto-catalog.py"):
            write(work / "recipe" / filename, (HERE / filename).read_bytes())
        if not args.host_build:
            for name, pin in C.PINS.items():
                version = subprocess.check_output(["chroot", str(root), "rpm", "-q", "--qf",
                                                   "%{VERSION}-%{RELEASE}", name + "-devel"], text=True)
                C.require(version == pin["version"], "devel package does not match pinned source: " + name)
        tools = invoke(work, root, args.host_build, "identity")
        try:
            current = C.installed(root)
        except (ValueError, OSError):
            current = None
        if current is not None and C.regular(current / "build/toolchain.json") == tools:
            print("static-crypto: validated warm reuse " + current.name)
            return
        catalog = work / "catalog"
        shutil.copytree(work / "recipe", catalog / "recipe")
        write(catalog / "build/toolchain.json", tools)
        for name, pin in C.PINS.items():
            srpm, sources = obtain(args.sources, name, args.download)
            write(catalog / "sources" / name / C.srpm_name(name), srpm)
            for filename, data in sources.items():
                write(catalog / "sources" / name / filename, data)
            for filename, data in C.source_tar(name, sources[pin["archive"]]).items():
                write(catalog / "licenses" / name / filename, data)
        try:
            invoke(work, root, args.host_build, "build", "--jobs", str(args.jobs))
        except subprocess.CalledProcessError as error:
            sys.stderr.buffer.write(error.output or b"")
            for path in sorted((catalog / "build").rglob("*.log")):
                print(str(path) + ":\n" + path.read_text(errors="replace")[-5000:], file=sys.stderr)
            raise
        print("static-crypto: installed " + str(publish(catalog, root)))


def main():
    os.umask(0o022)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    get = sub.add_parser("install")
    get.add_argument("--root", type=Path, required=True)
    get.add_argument("--sources", type=Path, required=True)
    get.add_argument("--download", action="store_true")
    get.add_argument("--host-build", action="store_true", help="test only: local compiler, disposable prefix")
    get.add_argument("--jobs", type=int, choices=(1, 2), default=2)
    copy = sub.add_parser("copy")
    copy.add_argument("--template", type=Path, required=True)
    copy.add_argument("--root", type=Path, required=True)
    for name in ("identity", "build"):
        internal = sub.add_parser(name, help=argparse.SUPPRESS)
        internal.add_argument("--work", type=Path, required=True)
        if name == "build":
            internal.add_argument("--jobs", type=int, choices=(1, 2), required=True)
    args = parser.parse_args()
    if args.command == "identity":
        sys.stdout.buffer.write(encoded(toolchain(args.work)))
    elif args.command == "build":
        build(args.work, args.jobs)
    else:
        root = C.no_links(args.root)
        C.require(root != Path("/"), "refusing the live root")
        lock = C.no_links(root / "usr/share/kuasar-ci/.static-crypto.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        if lock.exists():
            C.regular(lock)
        with lock.open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            if args.command == "install":
                install(args)
            else:
                catalog = C.installed(args.template)
                publish(catalog, root)
                C.installed(root)
                print("static-crypto: validated slot copy " + catalog.name)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit("static-crypto: " + str(error))
