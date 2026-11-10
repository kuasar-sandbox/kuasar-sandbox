#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Assemble a native node-infra OCI image from exact, prebuilt platform products."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import subprocess
import tarfile
import tempfile


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ARCHES = {"x86_64": "amd64", "aarch64": "arm64", "amd64": "amd64", "arm64": "arm64"}
sys.path.insert(0, str(ROOT / "release"))
import node_infra_assets

REQUIRED = ("node-ctl", "sandbox-ctl", "sandbox-init", "cloud-hypervisor", "connector-ctl", "vmlinux", "sandbox-runtime.bundle", "manifest-ctl", "cache-ctl", "store-ctl", "flatten-ctl")


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, text=True, **kwargs)


def validate_elf(path, arch):
    with path.open("rb") as stream:
        header = stream.read(20)
    machine = {"amd64": 62, "arm64": 183}[arch]
    if len(header) < 20 or header[:6] != b"\x7fELF\x02\x01" or struct.unpack("<H", header[18:20])[0] != machine:
        raise ValueError(f"{path} is not a native linux/{arch} ELF executable")


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_archive(path, arch, version, revision):
    with tarfile.open(path, "r:") as archive:
        def read(name):
            member = archive.getmember(name)
            if not member.isfile() or member.size > 16 * 1024 * 1024:
                raise ValueError(f"invalid OCI metadata member {name}")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"unreadable OCI member {name}")
            return stream.read()

        def blob(descriptor):
            digest = descriptor["digest"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("invalid OCI blob digest")
            data = read("blobs/sha256/" + digest.split(":", 1)[1])
            if len(data) != descriptor["size"] or hashlib.sha256(data).hexdigest() != digest.split(":", 1)[1]:
                raise ValueError("OCI descriptor size or digest differs from blob")
            return data

        index = json.loads(read("index.json"))
        if len(index.get("manifests", [])) != 1:
            raise ValueError("native node-infra archive must contain exactly one manifest")
        descriptor = index["manifests"][0]
        manifest = json.loads(blob(descriptor))
        if manifest.get("mediaType") != "application/vnd.oci.image.manifest.v1+json":
            raise ValueError("node-infra output is not an OCI image manifest")
        config = json.loads(blob(manifest["config"]))
        if config.get("os") != "linux" or config.get("architecture") != arch:
            raise ValueError("image platform does not match selected architecture")
        labels = config.get("config", {}).get("Labels", {})
        expected = {"org.opencontainers.image.version": version, "org.opencontainers.image.revision": revision,
                    "org.opencontainers.image.source": "https://github.com/kuasar-sandbox/kuasar-sandbox",
                    "io.kuasar-sandbox.node-infra.release": version, "io.kuasar-sandbox.node-infra.schema": "1"}
        if any(labels.get(key) != value for key, value in expected.items()):
            raise ValueError("image identity labels differ from build inputs")
        for layer in manifest["layers"]:
            if layer.get("mediaType") != "application/vnd.oci.image.layer.v1.tar+gzip":
                raise ValueError("image layers must be gzip compressed for digest-preserving publication")
        return {"image_id": manifest["config"]["digest"], "manifest_digest": descriptor["digest"],
                "manifest_size": descriptor["size"], "media_type": descriptor["mediaType"]}


def build(args):
    arch = ARCHES[args.arch]
    release_arch = {"amd64": "x86_64", "arm64": "aarch64"}[arch]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", args.version):
        raise ValueError("invalid aggregate release version")
    selection = json.loads(args.selection.read_text())
    for unit in ("accelerator", "connector", "sandboxer", "orchestrator", "runtime", "vmlinux"):
        if not re.fullmatch(r"[0-9a-f]{40}", selection[unit]["sha"]):
            raise ValueError(f"{unit} does not have an exact source SHA")
    revision = args.source_revision or run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("platform source revision must be an exact commit")
    product_root = args.products.resolve()
    products = product_root / "bin"
    for name in REQUIRED:
        path = products / name
        if not path.is_file():
            raise ValueError(f"missing selected product {path}")
        if name not in ("vmlinux", "sandbox-runtime.bundle"):
            validate_elf(path, arch)
    args.output.mkdir(parents=True, exist_ok=True)
    destination = args.output / f"node-infra-{args.version}-linux-{arch}.oci.tar.gz"
    docker_destination = args.output / f"node-infra-{args.version}-linux-{arch}.docker.tar.gz"
    receipt_path = args.output / f"node-infra-{release_arch}.json"
    if destination.exists() or receipt_path.exists() or docker_destination.exists():
        raise ValueError("node-infra output already exists; retain the original release bytes")
    receipt = {"schema": 1, "aggregate_version": args.version, "arch": release_arch,
               "source_revision": revision, "components": selection}
    with tempfile.TemporaryDirectory(prefix="node-infra-image-") as temporary:
        context = Path(temporary)
        helper = args.runtime_helper
        if helper is None:
            if args.sources is None:
                raise ValueError("provide --runtime-helper or exact --sources for helper compilation")
            helper = context / "node-infra-runtime"
            run(["python3", HERE.parent / "runtime/build.py", "--sources", args.sources,
                 "--selection", args.selection.resolve(), "--arch", args.arch, "--output", helper])
        validate_elf(helper, arch)
        shutil.copytree(products, context / "bin", symlinks=False)
        # Component archives own their runtime support and redistribution
        # material. Preserve that exact tree, including bundled source offers.
        if not (product_root / "share/licenses").is_dir():
            raise ValueError("selected products are missing share/licenses")
        shutil.copytree(product_root / "share", context / "share", symlinks=False)
        own_license = context / "share/licenses/node-infra"
        own_license.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "LICENSE", own_license / "LICENSE")
        for name in ("NOTICE", "COPYING"):
            if (ROOT / name).is_file():
                shutil.copy2(ROOT / name, own_license / name)
        shutil.copytree(HERE / "rootfs", context / "rootfs")
        shutil.copy2(HERE / "Containerfile", context / "Containerfile")
        for name in ("service", "entrypoint"):
            shutil.copy2(HERE.parent / "runtime" / name, context / name)
        if helper.resolve() != (context / "node-infra-runtime").resolve():
            shutil.copy2(helper, context / "node-infra-runtime")
        (context / "release.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        tag = f"localhost/node-infra-build:{args.version}-{arch}-{context.name.rsplit('-', 1)[-1]}"
        pull = "--pull=false" if args.engine == "docker" else "--pull=never"
        build_command = [args.engine, "build", pull, "--platform", "linux/" + arch,
                         "--build-arg", "RELEASE_VERSION=" + args.version,
                         "--build-arg", "SOURCE_REVISION=" + revision,
                         "--tag", tag, "--file", context / "Containerfile"]
        if args.base_image:
            build_command += ["--build-arg", "BASE_IMAGE=" + args.base_image]
        if args.build_network:
            build_command += ["--network", args.build_network]
        run(build_command + [context])
        try:
            raw_archive = context / "image.oci.tar"
            transport = "docker-daemon:" if args.engine == "docker" else "containers-storage:"
            run(["skopeo", "copy", "--format", "oci", "--dest-compress", transport + tag,
                 "oci-archive:" + str(raw_archive) + ":" + args.version])
            receipt.update(inspect_archive(raw_archive, arch, args.version, revision))
            with tempfile.NamedTemporaryFile(prefix=".node-infra-", dir=args.output, delete=False) as stream:
                pending = Path(stream.name)
                try:
                    with raw_archive.open("rb") as source:
                        with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as compressed:
                            shutil.copyfileobj(source, compressed, length=1024 * 1024)
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.link(pending, destination)  # atomic and refuses an existing asset
                finally:
                    pending.unlink(missing_ok=True)
            receipt.update(archive=destination.name, sha256=sha256_file(destination), size=destination.stat().st_size)
            if args.delivery == "node-infra-v2":
                docker_raw = context / "image.docker.tar"
                run(["skopeo", "copy", "oci-archive:" + str(raw_archive),
                     "docker-archive:" + str(docker_raw) + ":localhost/node-infra:" + args.version])
                node_infra_assets.inspect_docker(docker_raw, release_arch, image_id=receipt["image_id"],
                                                version=args.version, revision=revision)
                with docker_destination.open("xb") as stream:
                    with docker_raw.open("rb") as source, gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as compressed:
                        shutil.copyfileobj(source, compressed, length=1024 * 1024)
                receipt["docker_archive"] = {"name": docker_destination.name, "sha256": sha256_file(docker_destination),
                                             "size": docker_destination.stat().st_size}
            with receipt_path.open("x") as stream:
                stream.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
            print(json.dumps({"archive": str(destination), "receipt": str(receipt_path)}, sort_keys=True))
        finally:
            run([args.engine, "image", "rm", tag])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--delivery", choices=("node-infra-v1", "node-infra-v2"), default="node-infra-v2")
    parser.add_argument("--arch", choices=ARCHES, required=True)
    parser.add_argument("--products", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-helper", type=Path)
    parser.add_argument("--sources", type=Path)
    parser.add_argument("--source-revision")
    parser.add_argument("--engine", choices=("podman", "docker"), default="podman")
    parser.add_argument("--base-image", help="base image already available to the selected engine, preferably a digest reference")
    parser.add_argument("--build-network", help="explicit network mode for the image build only")
    build(parser.parse_args())


if __name__ == "__main__":
    main()
