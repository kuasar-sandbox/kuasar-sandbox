#!/usr/bin/env python3
"""Compose a target once and prepare immutable fixtures, without product work."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

import artifacts

BACKENDS = {
    "prometheus": "prom/prometheus:v3.5.0@sha256:63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996",
    "clickhouse": "clickhouse/clickhouse-server:25.8@sha256:0152dd511befe6a2c2ef53e930726179669b08116da78500b37c51c96ff5ee77",
}


def image_archive(path, go_arch):
    """Inspect Docker archive metadata without extracting layer paths."""
    with tarfile.open(path) as archive:
        manifests = json.load(archive.extractfile("manifest.json"))
        artifacts.require(len(manifests) == 1, "fixture must describe exactly one image")
        config_name = manifests[0]["Config"]
        artifacts.relative(config_name)
        data = archive.extractfile(config_name).read()
        config = json.loads(data)
        artifacts.require(config["architecture"] == go_arch and config["os"] == "linux", "fixture has the wrong image platform")
        return "sha256:" + hashlib.sha256(data).hexdigest()


def prepare(plan, arch, assets, delta, workspace):
    provenance = artifacts.compose(plan, arch, assets, delta, workspace)
    before = dict(provenance["files"])
    modes = dict(provenance["modes"])
    owners = {Path(case).parts[2] for case in provenance["profile"]["cases"]}
    go_arch = {"x86_64": "amd64", "aarch64": "arm64"}[arch]
    fixture_platform = "linux/" + go_arch
    fixtures, images = {}, {}
    if "accelerator" in owners:
        directory = workspace / "fixtures/manifest"
        subprocess.run(["python3", str(workspace / "test/e2e/accelerator/lib/manifest_fixture.py"),
                        str(directory), "--architecture", go_arch], check=True)
        for variant in ("a", "b"):
            path = directory / f"image-{variant}.tar"
            fixtures[str(path.relative_to(workspace))] = {"platform": fixture_platform,
                "image_id": image_archive(path, go_arch), "sha256": artifacts.digest(path), "owner": "accelerator"}
    if "guest-runtime" in owners:
        path = workspace / "images/guest-runtime.tar"
        tag = "kuasar-ci-guest-runtime:" + arch
        subprocess.run(["python3", str(workspace / "test/e2e/guest-runtime/fixture.py"), str(path),
                        "--tag", tag, "--architecture", go_arch], check=True)
        images["guest-runtime"] = {"reference": tag, "platform": fixture_platform, "image_id": image_archive(path, go_arch),
                                   "archive": str(path.relative_to(workspace)), "sha256": artifacts.digest(path), "owner": "guest-runtime"}
    references = {}
    if owners & {"sandboxer", "orchestrator", "platform"}:
        references["python"] = "python:3.12-slim"
    if "sandboxer" in owners:
        references["busybox"] = "busybox:latest"
    if "orchestrator" in owners:
        references.update(BACKENDS)
    with tempfile.TemporaryDirectory(prefix="integration-docker-") as config:
        environment = {**os.environ, "DOCKER_CONFIG": config}
        for label, reference in references.items():
            # Resolve once for this target and carry the resulting content ID
            # into E2E. Runtime tests never pull or choose replacement images.
            subprocess.run(["timeout", "3m", "docker", "pull", "--platform=" + fixture_platform, reference],
                           env=environment, check=True)
            records = json.loads(subprocess.check_output(["docker", "image", "inspect", reference], env=environment))
            artifacts.require(len(records) == 1 and records[0]["Architecture"] == go_arch and records[0]["Os"] == "linux",
                              "pulled image has the wrong platform")
            record = records[0]
            path = workspace / "images" / f"{label}.tar"
            subprocess.run(["docker", "image", "save", "--output", str(path), reference], env=environment, check=True)
            artifacts.require(image_archive(path, go_arch) == record["Id"], "saved fixture differs from resolved image")
            images[label] = {"reference": reference, "platform": fixture_platform, "image_id": record["Id"],
                             "repo_digests": record["RepoDigests"], "archive": str(path.relative_to(workspace)),
                             "sha256": artifacts.digest(path), "owner": "orchestrator" if label in BACKENDS else "platform"}
        if "orchestrator" in owners:
            artifacts.require(arch == "x86_64", "the declared orchestrator profile requires native x86 fixture preparation")
            for variant in ("base", "execute"):
                label = "orchestrator-" + variant
                reference = "kuasar-ci-" + label + ":" + artifacts.identity(plan)[:16]
                subprocess.run(["bash", str(workspace / "test/e2e/orchestrator/lib/prepare_base_image.sh"),
                                images["python"]["reference"], reference, variant], env=environment, check=True)
                record = json.loads(subprocess.check_output(["docker", "image", "inspect", reference], env=environment))[0]
                path = workspace / "images" / (label + ".tar")
                subprocess.run(["docker", "image", "save", "--output", str(path), reference], env=environment, check=True)
                artifacts.require(image_archive(path, go_arch) == record["Id"], "prepared orchestrator fixture differs from saved bytes")
                images[label] = {"reference": reference, "platform": fixture_platform, "image_id": record["Id"],
                    "archive": str(path.relative_to(workspace)), "sha256": artifacts.digest(path), "owner": "orchestrator"}
    after = artifacts.tree_files(workspace)
    after_modes = artifacts.tree_modes(workspace)
    artifacts.require(all(after.get(name) == value and after_modes.get(name) == modes[name] for name, value in before.items()),
                      "fixture preparation modified composed products/tests")
    additions = set(after) - set(before) - {"provenance.json"}
    allowed = set(fixtures) | {record["archive"] for record in images.values()}
    artifacts.require(additions == allowed, "fixture generator wrote undeclared workspace files")
    (workspace / "provenance.json").unlink()
    provenance.update(fixtures=fixtures, images=images, files=artifacts.tree_files(workspace), modes=artifacts.tree_modes(workspace))
    (workspace / "provenance.json").write_bytes(artifacts.canonical(provenance) + b"\n")
    artifacts.verify_workspace(workspace, plan, arch)
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--arch", choices=artifacts.ARCHES, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--delta", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    prepare(json.loads(args.plan.read_text()), args.arch, args.assets.resolve(), args.delta.resolve(), args.workspace.resolve())


if __name__ == "__main__":
    main()
