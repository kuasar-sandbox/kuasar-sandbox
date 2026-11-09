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


def aggregate(version, *, require_dual=True):
    """Resolve published bytes using only the selected tag's own manifest."""
    state = release.api_optional(f"repos/{PLATFORM}/releases/tags/{version}")
    if state is None or state.get('draft') is True:
        return None
    sha = exact_sha(release.tag_sha(PLATFORM, version))
    artifacts.require(state.get('tag_name') == version and state.get('draft') is False
                      and state.get('prerelease') == ('-preview.' in version),
                      'aggregate release identity mismatch')
    relative = 'releases/daily-preview.yaml' if '-preview.' in version else 'releases/release.yaml'
    manifest = source_text(PLATFORM, version, relative)
    selected, _, units = release.selection.parse_historical_manifest(manifest, f'{version}:{relative}', '-preview.' in version)
    artifacts.require(selected == version, 'aggregate tag does not select its published version')
    contract = release.selection.delivery(release.selection.read_simple_yaml(manifest, relative), relative)
    # SHA256SUMS is tolerated on old releases, but never read or admitted as an input.
    assets = [item for item in state['assets'] if item['name'] != 'SHA256SUMS']
    names = [item['name'] for item in assets]
    artifacts.require(len(names) == len(set(names)), 'duplicate aggregate asset names')
    base = {f'platform-{version}.tar.gz'}
    x86 = {artifacts.archive_name(unit, tag, 'x86_64') for unit, tag in units.items()}
    arm = {artifacts.archive_name(unit, tag, 'aarch64') for unit, tag in units.items()}
    expected = base | x86 | arm
    if contract in release.selection.MODERN_DELIVERIES:
        expected |= release.selection.delivery_assets(version, contract)
    elif set(names) == base | x86 and not require_dual:
        expected = base | x86
    artifacts.require(set(names) == expected, 'aggregate lacks its complete declared asset set')
    for asset in assets:
        artifacts.require(asset.get('state') == 'uploaded' and type(asset.get('id')) is int and asset['id'] > 0
                          and type(asset.get('size')) is int and asset['size'] > 0
                          and re.fullmatch(r'sha256:[0-9a-f]{64}', asset.get('digest') or ''),
                          'aggregate asset lacks an uploaded identity, size or SHA-256 digest')
    unit_records = {}
    for unit, tag in units.items():
        repository = REPOSITORIES[unit_owner(unit)]
        unit_records[unit] = {'version': tag, 'repository': repository,
                              'sha': exact_sha(release.tag_sha(repository, tag))}
    # Detect a tag moving across manifest retrieval. No Actions or release-body lookup.
    artifacts.require(release.tag_sha(PLATFORM, version) == sha, 'aggregate tag moved during selection')
    return {'repository': PLATFORM, 'version': version, 'sha': sha, 'release_id': state['id'],
            'units': unit_records, 'delivery': contract,
            'architectures': list(artifacts.ARCHES) if arm <= set(names) else ['x86_64'],
            'assets': [{key: asset[key] for key in ('id', 'name', 'size', 'digest')} for asset in assets]}


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
    raise ValueError("neither the maintained aggregate nor its explicit predecessor is a published complete baseline")


def maintained_units(platform_source, base_ref):
    """Only the trusted admission base selects component updates, never a PR."""
    relative = "releases/daily-preview.yaml" if base_ref == "main" else "releases/release.yaml"
    return release.selection.parse_historical_manifest(
        (platform_source / relative).read_text(), relative, base_ref == "main")[2]


def qualification_units(selected, maintained):
    """The aggregate tag fixes its tuple even when the maintained manifest advances."""
    return {unit: row['version'] for unit, row in selected['units'].items()}


CUTOVER_PATH = "ci/integration/dependency-cutovers.json"


def cutover_request(text, records):
    """Only a reviewed trusted-base record admits a new dependency tuple."""
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            artifacts.require(key not in result, 'duplicate cutover JSON key')
            result[key] = value
        return result
    document = json.loads(text, object_pairs_hook=unique_pairs)
    artifacts.require(isinstance(document, dict) and set(document) == {'schema', 'requests'}
                      and type(document['schema']) is int and document['schema'] == 1
                      and isinstance(document['requests'], list), 'invalid dependency cutover configuration')
    seen, matched = set(), None
    candidate_keys = {'repository', 'pull_request_number', 'base_ref', 'base_sha', 'head_sha', 'candidate_sha'}
    for request in document['requests']:
        artifacts.require(isinstance(request, dict) and set(request) == {'candidate', 'units'}, 'invalid cutover request')
        candidate, units = request['candidate'], request['units']
        artifacts.require(isinstance(candidate, dict) and set(candidate) == candidate_keys
                          and candidate['repository'] in REPOSITORIES.values()
                          and type(candidate['pull_request_number']) is int and candidate['pull_request_number'] > 0
                          and isinstance(candidate['base_ref'], str)
                          and re.fullmatch(r'main|release/v[0-9]+\.[0-9]+\.x', candidate['base_ref']),
                          'invalid cutover candidate')
        for key in ('base_sha', 'head_sha', 'candidate_sha'):
            exact_sha(candidate[key])
        identity = (candidate['repository'], candidate['pull_request_number'])
        artifacts.require(identity not in seen, 'duplicate cutover candidate')
        seen.add(identity)
        artifacts.require(isinstance(units, dict) and units and set(units) <= set(artifacts.UNITS), 'invalid cutover units')
        for unit, pin in units.items():
            artifacts.require(REPOSITORIES[unit_owner(unit)] != candidate['repository'], 'cutover cannot replace candidate owner')
            artifacts.require(isinstance(pin, dict) and set(pin) == {'tag', 'sha', 'release_id', 'assets'}, 'invalid cutover pin')
            source_inputs.tag_sources.validate_tag(unit, pin['tag'])
            exact_sha(pin['sha'])
            artifacts.require(type(pin['release_id']) is int and pin['release_id'] > 0, 'invalid cutover release ID')
            expected = {artifacts.archive_name(unit, pin['tag'], arch) for arch in artifacts.ARCHES}
            artifacts.require(isinstance(pin['assets'], list), 'invalid cutover asset set')
            assets = [row for row in pin['assets'] if row.get('name') != 'SHA256SUMS']
            artifacts.require(len(assets) == len(expected), 'invalid cutover asset set')
            names, ids = [], []
            for asset in assets:
                artifacts.require(isinstance(asset, dict) and set(asset) == {'name', 'id', 'size', 'digest'}
                                  and isinstance(asset['name'], str)
                                  and type(asset['id']) is int and asset['id'] > 0
                                  and type(asset['size']) is int and asset['size'] > 0
                                  and isinstance(asset['digest'], str)
                                  and re.fullmatch(r'sha256:[0-9a-f]{64}', asset['digest']), 'invalid cutover asset pin')
                names.append(asset['name']); ids.append(asset['id'])
            artifacts.require(names == sorted(expected) and len(ids) == len(set(ids)), 'cutover assets must be exact and unique')
        primary = records[0]
        if identity == (primary['repository'], primary['pull_request_number']):
            artifacts.require(len(records) == 1 and candidate == primary, 'cutover candidate identity changed')
            matched = request
    return matched


def verify_cutover_publication(unit, pin):
    repository = REPOSITORIES[unit_owner(unit)]
    public(repository)
    state = release.api(f"repos/{repository}/releases/tags/{quote(pin['tag'], safe='')}")
    sha = exact_sha(release.tag_sha(repository, pin['tag']))
    check_published_unit(unit, pin['tag'], state, sha)
    assets = sorted(({key: row.get(key) for key in ('name', 'id', 'size', 'digest')}
                     for row in state['assets'] if row['name'] != 'SHA256SUMS'), key=lambda row: row['name'])
    artifacts.require(sha == pin['sha'] and state.get('id') == pin['release_id'] and assets == [row for row in pin['assets'] if row['name'] != 'SHA256SUMS'],
                      'cutover publication identity changed: ' + unit)


def dependency_cutover(platform_source, base_ref, platform_sha, records):
    path = platform_source / CUTOVER_PATH
    if not path.exists() and not path.is_symlink():
        return None
    artifacts.require(path.is_file() and not path.is_symlink(), 'cutover configuration must be regular')
    request = cutover_request(path.read_text(), records)
    if request is None:
        return None
    for unit, pin in request['units'].items():
        verify_cutover_publication(unit, pin)
    return {'platform_sha': platform_sha, 'base_ref': base_ref,
            'configuration_sha256': artifacts.digest(path), 'request': request}


def verify_dependency_cutover(plan):
    """Recheck live trusted configuration and immutable publications at acceptance."""
    evidence = plan.get('dependency_cutover')
    if evidence is None:
        return
    artifacts.check_plan(plan)
    artifacts.require(plan['mode'] == 'source' and evidence['platform_sha'] ==
                      plan['run_inputs']['admission-platform']['sha'], 'cutover admission platform changed')
    request = evidence['request']
    artifacts.require(evidence['base_ref'] == plan['candidate_records'][0]['base_ref'], 'cutover branch changed')
    # Never read a candidate tree. A missing/revoked entry fails closed; unrelated
    # reviewed entries may change without invalidating this exact request.
    value = release.api(f"repos/{PLATFORM}/contents/{CUTOVER_PATH}?ref={quote(evidence['base_ref'], safe='')}")
    artifacts.require(value.get('type') == 'file' and value.get('encoding') == 'base64', 'missing trusted cutover configuration')
    current = cutover_request(base64.b64decode(value['content'], validate=False).decode(), plan['candidate_records'])
    artifacts.require(current == request, 'dependency cutover was changed or revoked')
    for unit, pin in request['units'].items():
        record = plan['source_records'][unit]
        artifacts.require(record['tag'] == pin['tag'] and record['sha'] == pin['sha']
                          and plan['maintained_deltas'].get(unit) == record, 'cutover source differs from reviewed pin')
        verify_cutover_publication(unit, pin)


def published_unit(unit, tag):
    """A selected source delta must already be a complete exact publication."""
    source_inputs.tag_sources.validate_tag(unit, tag)
    repository = REPOSITORIES[unit_owner(unit)]
    public(repository)
    state = release.api_optional(f"repos/{repository}/releases/tags/{quote(tag, safe='')}")
    sha = exact_sha(release.tag_sha(repository, tag))
    return check_published_unit(unit, tag, state, sha)


def check_published_unit(unit, tag, state, sha):
    """Validate one observed publication; shared with read-only manifest CI."""
    artifacts.require(isinstance(state, dict) and state.get('tag_name') == tag
                      and state.get('draft') is False
                      and state.get('prerelease') == ('-preview.' in tag),
                      'maintained component tag lacks a published release: ' + unit + ' ' + tag)
    assets = [row for row in state.get('assets', []) if row['name'] != 'SHA256SUMS']
    names = [row['name'] for row in assets]
    expected = {artifacts.archive_name(unit, tag, arch) for arch in artifacts.ARCHES}
    artifacts.require(len(names) == len(set(names)) and set(names) == expected
                      and all(row.get('state') == 'uploaded' and type(row.get('size')) is int
                              and row['size'] > 0 and type(row.get('id')) is int and row['id'] > 0 and re.fullmatch(r'sha256:[0-9a-f]{64}', row.get('digest') or '')
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
    cutover = dependency_cutover(admission_platform, base_ref, platform_sha, records)
    qualified = qualification_units(selected, maintained)
    if cutover:
        qualified = dict(qualified, **{unit: pin['tag'] for unit, pin in cutover['request']['units'].items()})
        artifacts.require(all(tag != selected['units'][unit]['version'] for unit, tag in qualified.items()
                              if unit in cutover['request']['units']), 'cutover must select a new dependency')
    for unit, tag in qualified.items():
        if tag == selected['units'][unit]['version']:
            continue
        sha = published_unit(unit, tag)
        if cutover and unit in cutover['request']['units']:
            artifacts.require(sha == cutover['request']['units'][unit]['sha'], 'cutover tag moved during resolution')
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
            role = 'release' if cutover and unit in cutover['request']['units'] else 'baseline'
            sources[owner] = {'repository': row['repository'], 'sha': row['sha'], 'role': role}
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
    if cutover:
        # Qualify the entire new source tuple, retaining ordinary architecture
        # capabilities and credentialed exclusions, not a new aggregate baseline.
        owners = list(artifacts.OWNERS)
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
    if cutover:
        plan['dependency_cutover'] = cutover
    import helper_inputs
    identities = {}
    for owner, root in roots.items():
        row = run_inputs[str(root.relative_to(input_root))]
        identities[owner] = {key: row[key] for key in ('repository', 'sha', 'tree', 'tag', 'ref') if key in row}
        if 'tag' in identities[owner]: identities[owner].pop('ref', None)
    plan['helper_contract'] = helper_inputs.discover(roots, identities)
    requirements = artifacts.capabilities_module().discover(roots, selected_files)
    capability_routing.apply(plan, requirements)
    if any(name.startswith('basic.node-infra-') for lane in plan['lanes'].values()
           for name in lane['selection']['cases']):
        # Candidate images use the admitted platform recipe and the same
        # baseline-plus-delta products as ordinary E2E. No component main lookup.
        components = {}
        for unit, record in selected['units'].items():
            owner = unit_owner(unit)
            sha = kernel_sha if unit == 'vmlinux' else sources[owner]['sha']
            components[unit] = {'repository': record['repository'], 'sha': sha,
                                'version': record['version'] if sha == record['sha'] else 'source-' + sha}
        plan['node_infra'] = {'version': selected['version'], 'source_revision': sources['platform']['sha'],
                              'components': components}
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
    names = {f"platform-{version}.tar.gz"} | {
        artifacts.archive_name(unit, tag, arch) for unit, tag in units.items() for arch in artifacts.ARCHES}
    contract = release.selection.delivery(release.selection.read_simple_yaml(manifest, relative), relative)
    artifacts.require(contract in release.selection.MODERN_DELIVERIES, 'new stages require the committed workbench delivery contract')
    names |= release.selection.delivery_assets(version, contract)
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
    if 'source_scope' in plan:
        return False
    for record in plan['candidate_records']:
        if record['repository'] == PLATFORM:
            paths = plan.get('admission_changes', {}).get('platform')
            artifacts.require(isinstance(paths, list), 'missing admitted platform comparison')
            if any(path.startswith('workbench/') for path in paths):
                return True
    return False


def attach_source_scope(plan, evidence, policy_sha):
    """Bind the trusted PR comparison, never the release-to-candidate delta."""
    import source_lane
    artifacts.require(plan.get('mode') == 'source', 'source scope cannot weaken exact-assets qualification')
    artifacts.require(evidence.get('policy_sha') == exact_sha(policy_sha) and
                      evidence.get('run_id') == os.environ['GITHUB_RUN_ID'] and
                      evidence.get('run_attempt') == os.environ['GITHUB_RUN_ATTEMPT'],
                      'stale source qualification policy/run/attempt')
    plan['source_scope'] = source_lane.scope_from_evidence(evidence)
    source_lane.verify_plan_scope(plan, evidence)
    artifacts.check_plan(plan)
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework-sha", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-cutover", type=Path)
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--workbench-output", type=Path)
    parser.add_argument("--source-scope", type=Path)
    args = parser.parse_args()
    if args.verify_cutover:
        verify_dependency_cutover(json.loads(args.verify_cutover.read_text()))
        return
    artifacts.require(args.output is not None, "resolution requires --output")
    # The existing event, exact-merge-parent and companion checks remain the
    # admission authority. This command is run only after those checks succeed.
    import shutil
    import tarfile
    args.output.parent.mkdir(parents=True, exist_ok=True)
    capsule = args.output.parent / 'source-inputs.tar'
    with tempfile.TemporaryDirectory(prefix='admitted-run-') as directory:
        if args.stage:
            artifacts.require(args.source_scope is None, 'exact-assets cannot use source-only qualification')
            plan = exact_assets_plan(args.framework_sha, args.stage)
            shutil.copyfile(args.stage / 'source-inputs.tar', capsule)
        else:
            root = Path(directory) / 'inputs'
            plan = source_plan(args.framework_sha, root)
            with tarfile.open(capsule, 'w') as archive:
                for entry in sorted(root.iterdir()):
                    archive.add(entry, arcname=entry.name)

    if args.source_scope:
        attach_source_scope(plan, json.loads(args.source_scope.read_text()), os.environ['DOCS_POLICY_SHA'])

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as output:
        output.write(artifacts.canonical(plan) + b"\n")
    if args.workbench_output:
        with args.workbench_output.open('x') as output:
            output.write('true\n' if workbench_requested(plan) else 'false\n')
    print(f"resolved aggregate {plan['baseline']['version']} plan={artifacts.identity(plan)}")


if __name__ == "__main__":
    main()
