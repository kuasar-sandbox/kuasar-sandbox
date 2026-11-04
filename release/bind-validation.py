#!/usr/bin/env python3
"""Verify this publication stage, then copy its committed human release notes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ci/integration"))
import artifacts
import workbench_groups
sys.path.insert(0, str(Path(__file__).resolve().parent))
import publication_body
import release_notes


def verify_stage(bundle, plan, validation, workbench):
    artifacts.require(plan["mode"] == "exact-assets" and plan["baseline"].get("staged"), "release needs a staged exact-assets plan")
    artifacts.require(validation == artifacts.collect_results(plan, validation["architectures"]), "validation identity differs from staged plan")
    artifacts.require(plan.get('source_records') is not None, 'new publication requires selected tag source records')
    artifacts.require(json.loads((bundle / 'source-records.json').read_text()) == plan['source_records'],
                      'publisher source records differ from validated stage')
    expected = {record["name"]: record["digest"] for record in plan["baseline"]["assets"]}
    actual = {name: "sha256:" + value for name, value in artifacts.tree_files(bundle / "assets").items()}
    artifacts.require(actual == expected, "publisher bytes differ from validated stage")
    artifacts.require(plan['baseline'].get('delivery') == 'workbench-v1', 'new publication requires the declared workbench delivery contract')
    results = artifacts.check_workbench_results(plan['baseline']['version'], plan['baseline']['sha'], workbench,
                                                expected_assets=actual, case_files=plan['case_files'])
    for arch, result in results.items():
        workbench_groups.verify_live(result)
        artifacts.require(result['plan_id'] == artifacts.identity(plan) and result['framework_sha'] == plan['framework_sha']
                          and result['test_revisions'] == plan['test_revisions'], 'workbench executor provenance differs from plan')
        receipt = json.loads((bundle / 'workbench' / f'workbench-{arch}.json').read_text())
        artifacts.require(result['image_id'] == receipt['image_id'] and result['sha256'] == receipt['sha256'] and result['size'] == receipt['size'],
                          'workbench result differs from staged image receipt')
    return results


def bind(bundle, plan, validation, workbench=None, *, platform=None):
    verify_stage(bundle, plan, validation, workbench)
    version = plan['baseline']['version']
    units = {unit: row['version'] for unit, row in plan['baseline']['units'].items()}
    fields, url = release_notes.publication_notes(release_notes.ROOT, version, platform)
    notes = bundle / 'release-notes.md'
    narrative = release_notes.Narrative(notes.read_text(), fields, url, version=version, units=units)
    notes.write_text(publication_body.checked(narrative.text))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("validation", type=Path)
    parser.add_argument("--workbench", type=Path, required=True)
    parser.add_argument("--platform", type=Path, default=Path("platform"), help="admitted platform checkout")
    args = parser.parse_args()
    bind(args.bundle, json.loads(args.plan.read_text()), json.loads(args.validation.read_text()),
         {arch: json.loads((args.workbench / f"{arch}.json").read_text()) for arch in artifacts.ARCHES},
         platform=args.platform)
