#!/usr/bin/env python3
"""Bind release notes to the exact staged bytes and both successful architecture selections."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ci/integration"))
import artifacts


def bind(bundle, plan, validation, workbench=None):
    artifacts.require(plan["mode"] == "exact-assets" and plan["baseline"].get("staged"), "release needs a staged exact-assets plan")
    artifacts.require(validation == artifacts.collect_results(plan, validation["architectures"]), "validation identity differs from staged plan")
    pins = {owner: record["sha"] for owner, record in plan["test_revisions"].items() if owner != "platform"}
    artifacts.require(json.loads((bundle / "test-revisions.json").read_text()) == pins,
                      "publisher test pins differ from validated stage")
    expected = {record["name"]: record["digest"] for record in plan["baseline"]["assets"]}
    actual = {name: "sha256:" + value for name, value in artifacts.tree_files(bundle / "assets").items()}
    artifacts.require(actual == expected, "publisher bytes differ from validated stage")
    notes = bundle / "release-notes.md"
    text = notes.read_text()
    artifacts.require("<!-- kuasar-integration-validation " not in text, "reserved validation marker already present")
    binding = {"aggregate_sha": plan["baseline"]["sha"], "plan_id": artifacts.identity(plan),
               "framework_sha": plan["framework_sha"], "test_revisions": plan["test_revisions"],
               "assets": {name: value for name, value in actual.items() if name != "SHA256SUMS"},
               "architectures": validation["architectures"]}
    artifacts.require(plan['baseline'].get('delivery') == 'workbench-v1', 'new publication requires the declared workbench delivery contract')
    binding['delivery'] = 'workbench-v1'
    binding['workbench'] = artifacts.check_workbench_results(plan['baseline']['version'], plan['baseline']['sha'], workbench,
                                                           expected_assets=binding['assets'], case_files=plan['case_files'])
    for arch, result in binding['workbench'].items():
        artifacts.require(result['plan_id'] == artifacts.identity(plan) and result['framework_sha'] == plan['framework_sha']
                          and result['test_revisions'] == plan['test_revisions'], 'workbench executor provenance differs from plan')
        receipt = json.loads((bundle / 'workbench' / f'workbench-{arch}.json').read_text())
        artifacts.require(result['image_id'] == receipt['image_id'] and result['sha256'] == receipt['sha256'],
                          'workbench result differs from staged image receipt')
    notes.write_text(text + "\n<!-- kuasar-integration-validation " + artifacts.canonical(binding).decode() + " -->\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("validation", type=Path)
    parser.add_argument("--workbench", type=Path, required=True)
    args = parser.parse_args()
    bind(args.bundle, json.loads(args.plan.read_text()), json.loads(args.validation.read_text()),
         {arch: json.loads((args.workbench / f"{arch}.json").read_text()) for arch in artifacts.ARCHES})
