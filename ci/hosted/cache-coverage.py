#!/usr/bin/env python3
"""Separate the existing Workbench cache producers by their actual build scope."""

import hashlib
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("cache_coverage_artifacts", ROOT / "ci/integration/artifacts.py")
artifacts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(artifacts)

RELEASES = {
    "release-accelerator-build": ["rocksdb"],
    "release-runtime-build": ["envd", "erofs"],
    "release-connector": [],
    "release-sandboxer": ["cloud-hypervisor"],
    "release-orchestrator": [],
    "release-vmlinux": ["vmlinux"],
}
KINDS = (*RELEASES, "integration-delta", "aggregate-helpers")


def digest(record):
    return hashlib.sha256(artifacts.canonical(record)).hexdigest()


def names(value, allowed, label):
    artifacts.require(isinstance(value, list) and all(isinstance(item, str) and item in allowed for item in value)
                      and len(value) == len(set(value)), "invalid cache coverage " + label)
    return sorted(value)


def identity(name: str, sources: Path, arch: str) -> dict:
    """This is cache completeness, not source admission or a build dispatcher.

    The caller puts this digest in both the exact key and restore prefix. A
    successful helper build must not occupy a product or release build key.
    Source pins and native recipe/toolchain inputs retain their existing keys.
    """
    artifacts.require(name in KINDS, "unknown Workbench cache coverage")
    artifacts.require(arch in artifacts.ARCHES, "invalid native cache coverage architecture")
    result = {"schema": 1, "kind": name, "arch": arch}
    if name in RELEASES:
        result["native"] = RELEASES[name]
    elif name == "integration-delta":
        plan = json.loads((sources / ".ci/plan.json").read_text())
        lane = plan["lanes"][arch]
        result.update(products=names(lane["products"], artifacts.PRODUCTS, "products"),
                      embedded_products=names(lane.get("embedded_products", []), {"envd"}, "embedded products"),
                      helpers=artifacts.planned_helpers(lane["selection"]))
    elif name == "aggregate-helpers":
        # The existing producer owns its helper list and demo wheel selection;
        # do not create a second helper manifest in the cache wrapper.
        result["recipes"] = {relative: artifacts.digest(ROOT / relative) for relative in
                             ("release/build-e2e-helpers.py", "ci/integration/build_demo_wheels.py")}
    return result
