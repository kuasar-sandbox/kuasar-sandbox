#!/usr/bin/env python3
"""Resolve one exact published aggregate and admitted candidate product delta."""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote

import artifacts
import workbench_groups
import capability_routing
import source_inputs

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("preview_coordinator", ROOT / "release/preview_coordinator.py")
release = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = release
spec.loader.exec_module(release)
PLATFORM = "kuasar-sandbox/kuasar-sandbox"
REPOSITORIES = {owner: f"kuasar-sandbox/{owner}" for owner in artifacts.OWNERS if owner != "platform"}
REPOSITORIES["platform"] = PLATFORM
LIBRARIES = {
    "accelerator": (), "connector": (), "sandboxer": ("accelerator", "connector"),
    "guest-runtime": ("accelerator",), "orchestrator": ("accelerator", "connector", "sandboxer"),
}
PROFILE_BINDING = re.compile(r"<!-- kuasar-integration-validation (\{[^\r\n]*\}) -->")


def source_text(repository, tag, path):
    artifacts.require(re.fullmatch(r'release-v[0-9]+\.[0-9]+\.[0-9]+(?:-preview\.[0-9]{8}(?:\.[1-9][0-9]*)?)?', tag),
                      'published manifest retrieval requires an aggregate tag')
    value = release.api(f"repos/{repository}/contents/{path}?ref={quote(tag, safe='')}")
    artifacts.require(value.get("encoding") == "base64" and value.get("type") == "file", "expected exact source file")
    return base64.b64decode(value["content"]).decode()


def unit_owner(unit):
    return "guest-runtime" if unit in ("runtime", "vmlinux") else unit


def exact_sha(value):
    artifacts.require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value), "expected exact source SHA")
    return value


def public(repository):
    state = release.api(f"repos/{repository}")
    artifacts.require(state.get("full_name") == repository and state.get("visibility") == "public"
                      and state.get("private") is False, f"public artifact lane cannot qualify a non-public caller/companion: {repository}")


def bound_plan(binding, version, sha, assets, runs):
    """Read original evidence, including retained legacy plans; never infer owners."""
    plan = binding.get('validation_plan')
    if plan is None:
        plans = []
        for run in runs:
            listing = release.api(f"repos/{PLATFORM}/actions/runs/{run['id']}/artifacts?per_page=100")
            artifacts.require(listing.get('total_count', 0) <= 100, 'ambiguous historical plan artifact listing')
            candidates = [item for item in listing.get('artifacts', [])
                          if item['name'] == f"integration-plan-{run['id']}" and not item.get('expired')]
            for item in candidates:
                with tempfile.TemporaryDirectory(prefix='historical-plan-') as directory:
                    release.gh('run', 'download', str(run['id']), '--repo', PLATFORM,
                               '--name', item['name'], '--dir', directory)
                    path = Path(directory) / 'plan.json'
                    artifacts.require(path.is_file() and not path.is_symlink(), 'historical plan artifact is missing')
                    candidate = json.loads(path.read_text())
                    if artifacts.identity(candidate) == binding.get('plan_id'):
                        plans.append(candidate)
        artifacts.require(len(plans) == 1, 'missing or ambiguous original validated integration-plan evidence')
        plan = plans[0]
    artifacts.require(artifacts.identity(plan) == binding.get('plan_id'), 'published plan identity mismatch')
    if plan.get('schema') == 4:
        artifacts.check_plan(plan)
        artifacts.collect_results(plan, binding['architectures'])
    artifacts.require(plan.get('mode') == 'exact-assets' and plan['baseline']['version'] == version
                      and plan['baseline']['sha'] == sha and plan['framework_sha'] == binding['framework_sha']
                      and plan['test_revisions'] == binding['test_revisions'], 'published plan provenance mismatch')
    expected = {row['name']: row['digest'] for row in plan['baseline']['assets']}
    artifacts.require(len(expected) == len(plan['baseline']['assets']) and expected == assets,
                      'published plan complete asset inventory mismatch')
    artifacts.require({name: digest for name, digest in expected.items() if name != 'SHA256SUMS'} == binding['assets'],
                      'published plan asset inventory mismatch')
    selected_cases = plan.get('case_files')
    artifacts.require(isinstance(selected_cases, dict) and set(selected_cases) == set(artifacts.OWNERS),
                      'original validated plan lacks exact case ownership evidence')
    # This validates duplicate IDs, filenames and architecture coverage without
    # reading historical independent test sources.
    for arch, result in binding['architectures'].items():
        original_selection = plan['lanes'][arch].get('selection')
        artifacts.require((plan['schema'] >= 4 or artifacts.compatible_suite_selection(original_selection, ['platform'], arch, selected_cases))
                          and result.get('selection') == original_selection
                          and result.get('plan_id') == binding['plan_id'], 'published case selection differs from original plan')
    for result in binding.get('workbench', {}).values():
        artifacts.require(result.get('plan_id') == binding['plan_id']
                          and result.get('framework_sha') == binding['framework_sha']
                          and result.get('test_revisions') == binding['test_revisions'],
                          'published workbench provenance differs from original plan')
    for key in ('case_files', 'source_records'):
        if key in binding:
            artifacts.require(binding[key] == plan.get(key), 'published ' + key + ' differs from original plan')
    return plan


def check_published_units(units, plan):
    artifacts.require(units == plan['baseline'].get('units'),
                      'selected component tags differ from original validated plan')


def historical_profile(arch, cases=None):
    """Interpret immutable pre-cutover release evidence, never execute its entries."""
    cases = cases or {}
    owners = set(artifacts.OWNERS if arch == 'x86_64' else ('accelerator', 'guest-runtime'))
    exclusions = [{'owner': 'accelerator', 'case': 'storage.obs.sh', 'reason': 'credentialed OBS case is not selected'}]
    if not cases.get('accelerator'):
        exclusions = [{'owner': 'accelerator', 'case': 'e2e_obs.sh', 'reason': 'credentialed OBS suite is not selected'}]
    elif 'storage.obs.sh' not in cases['accelerator']:
        exclusions = []
    if arch == 'aarch64':
        exclusions.extend({'owner': owner, 'reason': 'no selected independent ARM non-KVM owner suite'}
                          for owner in sorted(set(artifacts.OWNERS) - owners))
    entries = []
    for owner in artifacts.OWNERS:
        if owner not in owners:
            continue
        if cases.get(owner):
            entries.extend(f'test/e2e/{owner}/cases/{name}' for name in cases[owner]
                           if not (owner == 'accelerator' and name == 'storage.obs.sh'))
        else:
            entries.append(f'test/e2e/{owner}/run_all.sh')
    return {'cases': entries,
            'required_products': sorted(set().union(*(artifacts.REQUIRED[owner] for owner in owners)) | {'mkfs.erofs'}),
            'exclusions': exclusions, 'name': 'x86-owner-kvm' if arch == 'x86_64' else 'arm-native-non-kvm'}


def aggregate(version, *, require_dual=True):
    """Historical x86-only releases remain valid; normal dual lanes need ARM."""
    state = release.api_optional(f"repos/{PLATFORM}/releases/tags/{version}")
    if state is None:
        return None
    sha = release.tag_sha(PLATFORM, version)
    if state.get("draft") is not False or sha is None:
        return None
    artifacts.require(state.get("tag_name") == version and state.get("target_commitish") == sha
                      and state.get("prerelease") == ("-preview." in version), "aggregate release identity mismatch")
    relative = "releases/daily-preview.yaml" if "-preview." in version else "releases/release.yaml"
    manifest = source_text(PLATFORM, version, relative)
    selected, _, units = release.selection.parse_historical_manifest(manifest, f"{sha}:{relative}", "-preview." in version)
    artifacts.require(selected == version, "aggregate tag does not select its published version")
    contract = release.selection.delivery(release.selection.read_simple_yaml(manifest, relative), relative)
    base_names = {"SHA256SUMS", f"platform-{version}.tar.gz"}
    x86 = {artifacts.archive_name(unit, tag, "x86_64") for unit, tag in units.items()}
    arm = {artifacts.archive_name(unit, tag, "aarch64") for unit, tag in units.items()}
    names = [item["name"] for item in state["assets"]]
    artifacts.require(len(names) == len(set(names)), "duplicate aggregate asset names")
    if contract == release.selection.DELIVERY:
        expected_names = base_names | x86 | arm | {release.selection.workbench_archive(version, arch) for arch in artifacts.ARCHES}
        artifacts.require(set(names) == expected_names, 'new aggregate is missing its declared complete workbench asset set')
    elif set(names) not in (base_names | x86, base_names | x86 | arm):
        return None
    for asset in state["assets"]:
        artifacts.require(asset.get("state") == "uploaded" and isinstance(asset.get("size"), int)
                          and asset["size"] > 0 and re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or ""),
                          "aggregate has an unfinished or unverified asset")
    runs = [run for run in release.aggregate_runs(version)
            if run.get("conclusion") == "success" and run.get("status") == "completed"
            and run.get("display_title") == release.aggregate_run_title(version, sha)]
    if not runs:
        return None  # An interrupted current publication may use its declared predecessor.
    bindings = PROFILE_BINDING.findall(state.get("body") or "")
    validation = {"x86_64": {"name": "historical-real-kvm"}}
    tests = None
    if arm <= set(names):
        artifacts.require(len(bindings) == 1, "dual aggregate lacks its declared validation profiles")
        binding = json.loads(bindings[0])
        artifacts.require(binding["aggregate_sha"] == sha and set(binding["architectures"]) == set(artifacts.ARCHES),
                          "aggregate validation binding has the wrong source or architecture set")
        expected = {asset["name"]: asset["digest"] for asset in state["assets"] if asset["name"] != "SHA256SUMS"}
        artifacts.require(binding["assets"] == expected, "aggregate bytes differ from validated bytes")
        config = release.selection.read_simple_yaml(manifest, relative)
        evidence = bound_plan(binding, version, sha,
                              {asset['name']: asset['digest'] for asset in state['assets']}, runs)
        if 'test_revisions' in config:
            pins = release.selection.historical_test_revisions(config, relative)
        else:
            source_inputs.tag_sources.validate_records(binding.get('source_records'))
            pins = source_inputs.tag_sources.owner_revisions(binding['source_records'])
            for unit, record in binding['source_records'].items():
                artifacts.require(record['tag'] == units[unit], 'published source tag differs from manifest')
                observed = release.api(f"repos/{record['repository']}/commits/{quote(record['tag'], safe='')}")
                artifacts.require(observed['sha'] == record['sha'] and observed['commit']['tree']['sha'] == record['tree'],
                                  'published source tag identity changed')
        tests = artifacts.release_test_revisions(pins, sha)
        artifacts.require(binding.get('test_revisions') == tests, 'published test provenance differs from original selection')
        selected_files = evidence['case_files']
        bootstrap_cases = selected_files
        for arch, result in binding["architectures"].items():
            if 'selection' in result:
                actual = result['selection']
                artifacts.require((evidence['schema'] >= 4 or artifacts.compatible_suite_selection(actual, ['platform'], arch, selected_files)),
                                  'published case selection changed')
                expected = evidence['lanes'][arch]['selection']
            else:
                artifacts.require(contract != release.selection.DELIVERY, 'new aggregate requires the current explicit case selection')
                expected, actual = historical_profile(arch, bootstrap_cases), result.get('profile')
            artifacts.require(result["arch"] == arch and result["conclusion"] == "success"
                              and actual == expected
                              and result.get("test_revisions") == tests,
                              "aggregate did not pass its predeclared architecture profile")
        validation = binding["architectures"]
        if contract == release.selection.DELIVERY:
            artifacts.require(binding.get('delivery') == contract, 'missing declared workbench validation binding')
            artifacts.check_workbench_results(version, sha, binding.get('workbench'), expected_assets=binding['assets'], case_files=selected_files)
            for record in binding['workbench'].values():
                workbench_groups.verify_live(record)
            artifacts.check_registry_binding(version, binding)
    elif require_dual:
        raise ValueError(f"{version} is a valid historical x86-only baseline; explicit ARM initialization and new unit versions are required")
    unit_records = {}
    for unit, tag in units.items():
        repository = REPOSITORIES[unit_owner(unit)]
        unit_records[unit] = {"version": tag, "repository": repository, "sha": exact_sha(release.tag_sha(repository, tag))}
    if tests:
        check_published_units(unit_records, evidence)
    return {"repository": PLATFORM, "version": version, "sha": sha, "release_id": state["id"],
            "units": unit_records, "validation": validation, "test_revisions": tests, "delivery": contract,
            "validation_plan": evidence if tests else None,
            "case_files": selected_files if tests else None,
            "source_records": binding.get("source_records") if tests else None,
            "validation_run": max(runs, key=lambda run: run["id"])["html_url"],
            "workbench": binding.get("workbench") if contract == release.selection.DELIVERY else None,
            "registry": binding.get("registry") if contract == release.selection.DELIVERY else None,
            "assets": [{key: asset[key] for key in ("id", "name", "size", "digest")} for asset in state["assets"]]}


def baseline(platform_source, base_ref):
    preview = base_ref == "main"
    relative = "releases/daily-preview.yaml" if preview else "releases/release.yaml"
    # Baseline lookup is read-only, including during the first implementation PR
    # whose admitted base still contains a legacy manifest. It cannot authorize
    # a new release or retrieve the historical independent test sources.
    version, previous, _ = release.selection.parse_historical_manifest((platform_source / relative).read_text(), relative, preview)
    # Only maintained selections may recover an interrupted publication. Never
    # select each unit's latest release or silently switch to source builds.
    for selected in (version, previous):
        if selected:
            result = aggregate(selected)
            if result is not None:
                return result
    raise ValueError("neither the maintained aggregate nor its explicit predecessor is a published validated baseline")


def maintained_units(platform_source, base_ref):
    """Only the trusted admission base selects component updates, never a PR."""
    relative = "releases/daily-preview.yaml" if base_ref == "main" else "releases/release.yaml"
    return release.selection.parse_historical_manifest(
        (platform_source / relative).read_text(), relative, base_ref == "main")[2]


def qualification_units(selected, maintained):
    """Keep modern ordinary PR inputs on the fully validated aggregate tuple.

    aggregate() authenticates the original plan before this function runs.
    Legacy capability cutovers retain their existing published-tag admission;
    candidate manifests and missing declarations cannot select compatibility.
    """
    original = selected.get('validation_plan')
    if isinstance(original, dict) and original.get('schema') == 4:
        return {unit: row['version'] for unit, row in selected['units'].items()}
    return maintained


def published_unit(unit, tag):
    """A selected source delta must already be a complete exact publication."""
    source_inputs.tag_sources.validate_tag(unit, tag)
    repository = REPOSITORIES[unit_owner(unit)]
    public(repository)
    state = release.api_optional(f"repos/{repository}/releases/tags/{quote(tag, safe='')}")
    sha = exact_sha(release.tag_sha(repository, tag))
    artifacts.require(isinstance(state, dict) and state.get('tag_name') == tag
                      and state.get('draft') is False
                      and state.get('prerelease') == ('-preview.' in tag)
                      and state.get('target_commitish') == sha,
                      'maintained component tag lacks a published release: ' + unit + ' ' + tag)
    assets = state.get('assets', [])
    names = [row['name'] for row in assets]
    expected = {'SHA256SUMS'} | {artifacts.archive_name(unit, tag, arch) for arch in artifacts.ARCHES}
    artifacts.require(len(names) == len(set(names)) and set(names) == expected
                      and all(row.get('state') == 'uploaded' and type(row.get('size')) is int
                              and row['size'] > 0 and re.fullmatch(r'sha256:[0-9a-f]{64}', row.get('digest') or '')
                              for row in assets),
                      'maintained component release is incomplete or unverified: ' + unit + ' ' + tag)
    return sha


def maintained_products(units):
    """Rebuild changed tag products and every consumer of their source libraries.

    This deliberately conservative cutover closure avoids testing new owner
    sources against an old sibling binary. Kernel remains an independent unit;
    the removed optional erofs tool is never introduced as a build target.
    """
    owners = {unit_owner(unit) for unit in units if unit != 'vmlinux'}
    products = {name for name, unit in artifacts.PRODUCTS.items() if unit in units}
    for name, unit in artifacts.PRODUCTS.items():
        owner = unit_owner(unit)
        if owners.intersection(LIBRARIES.get(owner, ())):
            products.add(name)
    if products & {'sandbox-init', 'flatten-ctl'}:
        products.add('sandbox-runtime.bundle')
    return products - artifacts.OPTIONAL_LEGACY_PRODUCTS - ({'vmlinux'} if 'vmlinux' not in units else set())


def tree_changes(before, after):
    def files(root):
        rows = source_inputs.tag_sources.git(root, 'ls-tree', '-rz', 'HEAD').split('\0')
        return {row.split('\t', 1)[1]: row.split('\t', 1)[0] for row in rows if row}
    old, new = files(before), files(after)
    paths = sorted(path for path in old.keys() | new.keys() if old.get(path) != new.get(path))
    for path in paths:
        artifacts.relative(path)
    return paths


def product_source_map(products, sources, kernel_sha):
    result = {}
    for name in products:
        owner = unit_owner(artifacts.PRODUCTS[name])
        dependencies = {owner, *LIBRARIES[owner]}
        if name == "sandbox-init":
            dependencies = {"sandboxer"}
        if name == "sandbox-runtime.bundle":
            dependencies |= {"sandboxer"}
        if name in ("mkfs.erofs", "vmlinux"):
            dependencies = {"guest-runtime"}
        result[name] = {REPOSITORIES[dependency]: sources[dependency]["sha"] for dependency in sorted(dependencies)}
        if name == "vmlinux":
            result[name][REPOSITORIES["guest-runtime"]] = kernel_sha
    return result


def source_test_revisions(selected, platform_record, platform_sha, base_ref):
    """New CI tests use owner unit sources; legacy test facts remain historical."""
    pins = {owner: selected['units']['runtime' if owner == 'guest-runtime' else owner]['sha']
            for owner in artifacts.OWNERS if owner != 'platform'}
    records = artifacts.release_test_revisions(pins, platform_record['candidate_sha'] if platform_record else platform_sha)
    for record in records.values(): record['role'] = 'baseline'
    return records


def preserve_baseline_cases(baseline_cases, selected_cases, candidates):
    """A tag migration cannot silently drop tests from an unchanged owner."""
    artifacts.require(isinstance(baseline_cases, dict) and set(baseline_cases) == set(artifacts.OWNERS),
                      'baseline lacks validated case ownership evidence')
    for owner in sorted(set(artifacts.OWNERS) - set(candidates)):
        missing = sorted(set(baseline_cases[owner]) - set(selected_cases[owner]))
        artifacts.require(not missing,
                          'selected owner tag loses validated baseline cases; explicit new-tag cutover required: '
                          + owner + ': ' + ', '.join(missing))


def source_plan(framework_sha, input_root):
    primary = {"repository": os.environ["CANDIDATE_REPOSITORY"], "candidate_sha": os.environ["CANDIDATE_SHA"],
               "base_sha": os.environ["CANDIDATE_BASE_SHA"], "head_sha": os.environ["CANDIDATE_HEAD_SHA"],
               "base_ref": os.environ["CANDIDATE_BASE_REF"], "pull_request_number": int(os.environ["CANDIDATE_PR"])}
    records = [primary, *json.loads(os.environ["COMPANION_CANDIDATES"])]
    for record in records:
        public(record["repository"])
    platform_record = next((record for record in records if record["repository"] == PLATFORM), None)
    base_ref = platform_record["base_ref"] if platform_record else primary["base_ref"]
    platform_sha = platform_record["base_sha"] if platform_record else exact_sha(release.branch_sha(PLATFORM, base_ref))
    input_root.mkdir(parents=True, exist_ok=False)
    admission_platform = input_root / 'admission-platform'
    source_inputs.fetch_named(admission_platform, PLATFORM, 'refs/heads/' + base_ref, platform_sha)
    selected = baseline(admission_platform, base_ref)
    released = input_root / 'released'
    released.mkdir()
    tag_records = {}
    run_inputs = {}
    for unit, record in selected['units'].items():
        tag_records[unit] = source_inputs.tag_sources.fetch_unit(released / unit, unit, record['version'], record['sha'])
        run_inputs['released/' + unit] = dict(tag_records[unit], ref='refs/tags/' + record['version'])
    roots = {owner: released / unit for owner, unit in source_inputs.tag_sources.OWNER_UNITS.items()}
    roots['platform'] = admission_platform
    run_inputs['admission-platform'] = dict(source_inputs.inspect_platform(admission_platform, platform_sha), ref='refs/heads/' + base_ref)
    admission_changes = {}
    maintained = maintained_units(admission_platform, base_ref)
    tag_deltas = {}
    baseline_roots = dict(roots)
    for unit, tag in qualification_units(selected, maintained).items():
        if tag == selected['units'][unit]['version']:
            continue
        sha = published_unit(unit, tag)
        path = 'maintained/' + unit
        root = input_root / path
        tag_records[unit] = source_inputs.tag_sources.fetch_unit(root, unit, tag, sha)
        run_inputs[path] = dict(tag_records[unit], ref='refs/tags/' + tag)
        tag_deltas[unit] = tag_records[unit]
        if unit != 'vmlinux':
            roots[unit_owner(unit)] = root

    sources = {owner: {"repository": repository, "sha": selected["sha"] if owner == "platform" else
                      selected["units"]["runtime" if owner == "guest-runtime" else owner]["sha"], "role": "baseline"}
               for owner, repository in REPOSITORIES.items()}
    sources['platform'] = {'repository': PLATFORM, 'sha': platform_sha, 'role': 'baseline'}
    tests = source_test_revisions(selected, platform_record, platform_sha, base_ref)
    changes, owners = {}, []
    for unit, row in tag_deltas.items():
        owner = unit_owner(unit)
        if owner not in owners:
            owners.append(owner)
        if unit != 'vmlinux':
            sources[owner] = {'repository': row['repository'], 'sha': row['sha'], 'role': 'baseline'}
            tests[owner] = dict(sources[owner])
            changes[owner] = tree_changes(baseline_roots[owner], roots[owner])
    kernel_root = input_root / 'maintained/vmlinux' if 'vmlinux' in tag_deltas else released / 'vmlinux'
    kernel_sha = tag_records['vmlinux']['sha']
    for record in records:
        owner = "platform" if record["repository"] == PLATFORM else record["repository"].split("/")[1]
        if base_ref != "main" and owner != "platform":
            unit = "runtime" if owner == "guest-runtime" else owner
            expected_ref = release.preview_selection.component_source_ref(base_ref, unit, maintained[unit])
            artifacts.require(record["base_ref"] == expected_ref, "candidate targets a different release line")
        path = 'candidates/' + owner
        root = input_root / path
        observed = source_inputs.fetch_named(root, record['repository'],
            f"refs/pull/{record['pull_request_number']}/merge", record['candidate_sha'])
        run_inputs[path] = observed
        if owner == 'platform':
            for relative, preview in (('releases/release.yaml', False), ('releases/daily-preview.yaml', True)):
                release.selection.parse_manifest((root / relative).read_text(), relative, preview)
        changes[owner] = tree_changes(baseline_roots[owner], root)
        if owner == 'platform':
            admission_changes[owner] = changes[owner]
        roots[owner] = root
        sources[owner] = {"repository": record["repository"], "sha": exact_sha(record["candidate_sha"]),
                          "role": "candidate" if record is primary else "companion"}
        tests[owner] = dict(sources[owner])
        if owner not in owners:
            owners.append(owner)
        if owner == "guest-runtime":
            kernel_sha = record["candidate_sha"]
            kernel_root = root
    products = artifacts.changed_products(changes)
    # Reuse #150/#151's Makefile input projection instead of treating a change
    # to an unrelated native target as a kernel change.
    guest_changes = changes.get("guest-runtime", [])
    # Kernel is an independent release unit: its baseline source can differ
    # from runtime's source even though both live in guest-runtime.
    if kernel_root != released / "vmlinux":
        kernel_changes = tree_changes(released / "vmlinux", kernel_root)
        kernel_products = artifacts.changed_products({"guest-runtime": kernel_changes})
        products = sorted((set(products) - {"vmlinux"}) | ({"vmlinux"} if "vmlinux" in kernel_products else set()))
    else:
        kernel_changes = []
        products = [name for name in products if name != "vmlinux"]
    if "vmlinux" in products and "native-deps/Makefile" in kernel_changes:
        kernel_paths = {"native-deps/deps/common.sh", "native-deps/deps/build-vmlinux.sh"}
        other_kernel = any(path in kernel_paths or path.startswith(("native-deps/deps/vmlinux/", "native-deps/deps/linux-patches/")) for path in kernel_changes)
        old = (released / "vmlinux/native-deps/Makefile").read_text()
        new = (kernel_root / "native-deps/Makefile").read_text()
        if not other_kernel and release.preview_selection.vmlinux_make_inputs(old) == release.preview_selection.vmlinux_make_inputs(new):
            products.remove("vmlinux")
    products = sorted(set(products) | maintained_products(tag_deltas))
    embedded = ["envd"] if "runtime" in tag_deltas or any(path in ("native-deps/deps/build-envd.sh", "native-deps/deps/common.sh", "native-deps/Makefile") for path in guest_changes) else []
    selected_files = source_inputs.cases(roots)
    preserve_baseline_cases(selected.get('case_files'), selected_files, owners)
    plan = {"schema": 3, "mode": "source", "run_inputs": run_inputs,
            "source_records": tag_records, "maintained_deltas": tag_deltas, "admission_changes": admission_changes, "framework_sha": exact_sha(framework_sha), "baseline": selected,
            "candidate_records": records, "owners": sorted(owners), "changes": changes,
            "sources": sources, "kernel_sha": kernel_sha,
            "test_revisions": tests, "test_overlays": sorted(artifacts.OWNERS), "case_files": selected_files,
            "product_sources": product_source_map(products, sources, kernel_sha),
            "embedded_sources": {"envd": {REPOSITORIES["guest-runtime"]: sources["guest-runtime"]["sha"]}},
            "lanes": {arch: {"products": products, "embedded_products": embedded,
                             "performance": ["working-set-smoke"] if arch == "x86_64" and set(owners) & {"platform", "sandboxer"} else [],
                             "selection": artifacts.suite_selection(owners, arch, selected_files, changes=changes)} for arch in artifacts.ARCHES}}
    import helper_inputs
    identities = {}
    for owner, root in roots.items():
        row = run_inputs[str(root.relative_to(input_root))]
        identities[owner] = {key: row[key] for key in ('repository', 'sha', 'tree', 'tag', 'ref') if key in row}
        if 'tag' in identities[owner]: identities[owner].pop('ref', None)
    original = selected.get('validation_plan')
    historical = (isinstance(original, dict) and original.get('schema') == 2 and
                  'helper_contract' not in original)
    # The authenticated original plan selects compatibility, never an absent
    # declaration or a producer-controlled schema. Candidates use declarations.
    if historical and not (set(owners) - {'platform'}):
        import helper_history
        historical_platform = input_root / 'historical-platform'
        run_inputs['historical-platform'] = source_inputs.fetch_named(historical_platform, PLATFORM,
            'refs/tags/' + selected['version'], original['framework_sha'])
        run_id = int(selected['validation_run'].rsplit('/', 1)[1])
        with tempfile.TemporaryDirectory(prefix='historical-helper-admission-') as temporary:
            plan['helper_contract'] = helper_history.admit(selected, dict(roots, vmlinux=released / 'vmlinux'),
                identities, historical_platform, run_id, helper_history.api, helper_history.gh, Path(temporary))
    else:
        plan['helper_contract'] = helper_inputs.discover(roots, identities)
    requirements = artifacts.capabilities_module().discover(roots, selected_files)
    artifacts.require(not (isinstance(original, dict) and original.get('schema', 0) >= 4) or requirements is not None,
                      'selected sources cannot downgrade the E2E requirement generation')
    capability_routing.apply(plan, requirements)
    artifacts.check_plan(plan)
    return plan


def exact_assets_plan(framework_sha, stage):
    """A staged aggregate is a candidate, never a published baseline fallback."""
    version, sha = os.environ["RELEASE_VERSION"], exact_sha(os.environ["PLATFORM_SOURCE_SHA"])
    public(PLATFORM)
    relative = "releases/daily-preview.yaml" if "-preview." in version else "releases/release.yaml"
    with tempfile.TemporaryDirectory(prefix='staged-sources-') as directory:
        platform, units, source_records, platform_record, selected_files = source_inputs.staged_inputs(
            stage, version, sha, Path(directory) / 'inputs')
        manifest = (platform / relative).read_text()
        import helper_inputs
        roots = {owner: Path(directory) / 'inputs/sources' / unit for owner, unit in source_inputs.tag_sources.OWNER_UNITS.items()}
        roots['platform'] = platform
        identities = {owner: source_records[unit] for owner, unit in source_inputs.tag_sources.OWNER_UNITS.items()}
        identities['platform'] = platform_record
        helper_contract = helper_inputs.discover(roots, identities)
        case_requirements = artifacts.capabilities_module().discover(roots, selected_files)
    selected, _, units = release.selection.parse_manifest(manifest, relative, "-preview." in version)
    tests = artifacts.release_test_revisions(source_inputs.tag_sources.owner_revisions(source_records), sha)
    artifacts.require(selected == version, "staged aggregate is not selected by its exact platform source")
    artifacts.require((stage / "selection.tsv").read_text() == "".join(f"{unit}\t{units[unit]}\n" for unit in release.selection.UNITS),
                      "staged selection differs from exact source")
    names = {"SHA256SUMS", f"platform-{version}.tar.gz"} | {
        artifacts.archive_name(unit, tag, arch) for unit, tag in units.items() for arch in artifacts.ARCHES}
    contract = release.selection.delivery(release.selection.read_simple_yaml(manifest, relative), relative)
    artifacts.require(contract == release.selection.DELIVERY, 'new stages require the committed workbench delivery contract')
    names |= {release.selection.workbench_archive(version, arch) for arch in artifacts.ARCHES}
    files = artifacts.tree_files(stage / "assets")
    artifacts.require(set(files) == names, "a new aggregate must stage both product and workbench architecture asset sets")
    unit_records = {}
    for unit, tag in units.items():
        repository = REPOSITORIES[unit_owner(unit)]
        public(repository)
        actual_sha = exact_sha(release.tag_sha(repository, tag))
        artifacts.require(actual_sha == source_records[unit]['sha'], 'selected tag moved after source staging')
        unit_records[unit] = {"version": tag, "repository": repository, "sha": actual_sha}
    baseline = {"repository": PLATFORM, "version": version, "sha": sha, "units": unit_records, "staged": True,
                "delivery": contract,
                "assets": [{"name": name, "size": (stage / "assets" / name).stat().st_size,
                            "digest": "sha256:" + value} for name, value in sorted(files.items())]}
    sources = {owner: {"repository": repository, "sha": sha if owner == "platform" else
                      unit_records["runtime" if owner == "guest-runtime" else owner]["sha"], "role": "release"}
               for owner, repository in REPOSITORIES.items()}
    plan = {"schema": 3, "mode": "exact-assets", "helper_contract": helper_contract, "source_records": source_records,
            "platform_source": platform_record,
            "run_inputs": {'platform': platform_record, **{'sources/' + unit: record for unit, record in source_records.items()}}, "framework_sha": exact_sha(framework_sha), "baseline": baseline,
            "candidate_records": [], "owners": ["platform"], "sources": sources, "kernel_sha": unit_records["vmlinux"]["sha"],
            "test_revisions": tests, "test_overlays": [], "case_files": selected_files, "product_sources": {}, "embedded_sources": {},
            "lanes": {arch: {"products": [], "embedded_products": [], "performance": [],
                             "selection": artifacts.suite_selection(["platform"], arch, selected_files)}
                      for arch in artifacts.ARCHES}}
    capability_routing.apply(plan, case_requirements)
    artifacts.check_plan(plan)
    return plan


def workbench_requested(plan):
    # Compare the admitted PR's actual base, independently of the published
    # baseline. This also works for forks and platform companions without
    # executing or relying on candidate files in the trusted framework checkout.
    if plan['mode'] != 'source':
        return False
    for record in plan['candidate_records']:
        if record['repository'] == PLATFORM:
            paths = plan.get('admission_changes', {}).get('platform')
            artifacts.require(isinstance(paths, list), 'missing admitted platform comparison')
            if any(path.startswith('workbench/') for path in paths):
                return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework-sha", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--workbench-output", type=Path)
    args = parser.parse_args()
    # The existing event, exact-merge-parent and companion checks remain the
    # admission authority. This command is run only after those checks succeed.
    import shutil
    import tarfile
    args.output.parent.mkdir(parents=True, exist_ok=True)
    capsule = args.output.parent / 'source-inputs.tar'
    with tempfile.TemporaryDirectory(prefix='admitted-run-') as directory:
        if args.stage:
            plan = exact_assets_plan(args.framework_sha, args.stage)
            shutil.copyfile(args.stage / 'source-inputs.tar', capsule)
        else:
            root = Path(directory) / 'inputs'
            plan = source_plan(args.framework_sha, root)
            with tarfile.open(capsule, 'w') as archive:
                for entry in sorted(root.iterdir()):
                    archive.add(entry, arcname=entry.name)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as output:
        output.write(artifacts.canonical(plan) + b"\n")
    if args.workbench_output:
        with args.workbench_output.open('x') as output:
            output.write('true\n' if workbench_requested(plan) else 'false\n')
    print(f"resolved aggregate {plan['baseline']['version']} plan={artifacts.identity(plan)}")


if __name__ == "__main__":
    main()
