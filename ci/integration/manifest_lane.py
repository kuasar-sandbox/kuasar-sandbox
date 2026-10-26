"""Read-only release-plan qualification; never publish or claim future E2E."""
import importlib.util
from functools import lru_cache
from pathlib import Path
import re
import tempfile
from urllib.parse import quote

import docs_lane

# Reuse the existing published-reference readers, including historical evidence.
# These imports and every API call are from the trusted policy checkout.
spec = importlib.util.spec_from_file_location('manifest_resolver', Path(__file__).with_name('resolve-artifacts.py'))
resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolver)
release = resolver.release
selection = release.selection
require = docs_lane.require
MANIFESTS = ('releases/release.yaml', 'releases/daily-preview.yaml')


class References:
    """One observation per reference within this qualification, never Latest."""
    @lru_cache(maxsize=None)
    def unit(self, name, tag):
        unit = release.UNIT_BY_NAME[name]
        release.preview_selection.parse_tag(name, tag)
        sha = release.tag_sha(unit.repository, tag)
        state = release.api_optional(f'repos/{unit.repository}/releases/tags/{quote(tag, safe="")}')
        if state is not None:
            require(state.get('tag_name') == tag and type(state.get('draft')) is bool and
                    state.get('prerelease') is ('-preview.' in tag), 'component release identity mismatch')
            target = docs_lane.sha(state.get('target_commitish'))
            require(sha is None or target == sha, 'component tag and release target differ')
            if not state['draft']:
                # Existing reader checks public repository, exact target and the
                # complete dual-architecture asset identity/size/digest evidence.
                resolver.public(unit.repository)
                resolver.check_published_unit(name, tag, state, resolver.exact_sha(sha))
                return dict(state='published', sha=sha, release=state)
        return dict(state='pending-publication', sha=sha, release=state)

    @lru_cache(maxsize=None)
    def aggregate(self, version, *, predecessor=False):
        # A predecessor is historical comparison evidence, not the binary
        # baseline for a new dual-architecture execution. Keep its original
        # declared scope; current selections retain the default dual contract.
        return resolver.aggregate(version, require_dual=not predecessor)

    @lru_cache(maxsize=None)
    def tree(self, repository, sha):
        result = docs_lane.tree(repository, sha)
        require(not result['truncated'], 'incomplete dependency tree')
        return result


def dependency_projection(tree, dependency, consumer):
    paths = release.dependency_paths(dependency, consumer)
    return {path: row for path, row in tree['entries'].items()
            if row['type'] != 'tree' and not path.endswith('_test.go') and
            any(path == prefix or path.startswith(prefix + '/') for prefix in paths)}


def check_plan(version, previous, components, base_ref, references):
    if base_ref != 'main':
        line = re.fullmatch(r'release/v([0-9]+)\.([0-9]+)\.x', base_ref)
        require(line is not None and version.startswith(f'release-v{line[1]}.{line[2]}.'),
                'aggregate version does not belong to the source maintenance line')
    predecessor = references.aggregate(previous, predecessor=True) if previous else None
    require(previous is None or predecessor is not None, 'declared predecessor lacks published validation evidence')
    published = references.aggregate(version)
    if published:
        require({name: row['version'] for name, row in published['units'].items()} == components,
                'selection differs from the already published aggregate')
    observations = {name: references.unit(name, tag) for name, tag in components.items()}
    plans = {unit.name: release.Plan(unit, components[unit.name], None,
             observations[unit.name]['sha'] or '', components[unit.name], components[unit.name], 'fixed')
             for unit in release.UNITS}
    units, pending = {}, []
    for unit in release.UNITS:
        tag = components[unit.name]
        observed = observations[unit.name]
        dependencies = {name: components[name] for name in unit.dependencies}
        row = dict(repository=unit.repository, tag=tag, state=observed['state'],
                   sha=observed['sha'], dependencies=dependencies)
        if observed['state'] != 'published':
            # The release coordinator chooses and freezes the future source.
            # Do not invent a commit or require a future tag to exist at PR time.
            row['source_ref'] = release.preview_selection.component_source_ref(base_ref, unit.name, tag)
            pending.append(dict(unit=unit.name, requirement='publish-selected-tag'))
        else:
            state = observed['release']
            row['release_id'] = state['id']
            row['assets'] = [{key: asset[key] for key in ('id', 'name', 'size', 'digest')} for asset in state['assets']]
            if '-preview.' in tag:
                status = release.ReleaseStatus(state, observed['sha'], True)
                plan = plans[unit.name]
                original = release.preview_binding(status, tag)['dependencies']
                if original != release.dependency_binding(plan, plans):
                    pairs = [item.split('=', 1) for item in original.split(',') if item]
                    require(all(len(pair) == 2 for pair in pairs), 'invalid published dependency binding')
                    versions = dict(pairs)
                    require(len(versions) == len(pairs) and set(versions) == set(dependencies),
                            'published dependency set differs from release plan')
                    for name, old_tag in versions.items():
                        if old_tag == components[name]:
                            continue
                        old = references.unit(name, old_tag)
                        require(old['state'] == 'published', 'original dependency is not published')
                        new = observations[name]
                        if new['state'] != 'published':
                            pending.append(dict(unit=unit.name, dependency=name,
                                                requirement='verify-linked-inputs-before-reuse'))
                            continue
                        repository = release.UNIT_BY_NAME[name].repository
                        require(dependency_projection(references.tree(repository, old['sha']), name, unit.name) ==
                                dependency_projection(references.tree(repository, new['sha']), name, unit.name),
                                'published consumer has different linked dependency inputs: ' + unit.name)
                    plan = release.replace(plan, verified_dependency_binding=original)
                # Preserve the existing date/unit/source/dependency binding rules.
                release.validate_preview_reuse(plan, plans, status)
                row['preview_binding'] = release.preview_binding(status, tag)
        units[unit.name] = row
    return dict(version=version, previous=previous,
                predecessor_sha=predecessor['sha'] if predecessor else None,
                predecessor_architectures=sorted(predecessor['validation']) if predecessor else [],
                published_aggregate_sha=published['sha'] if published else None,
                units=units, pending=pending,
                acceptance='release exact-assets E2E and Workbench remain required')


def check_tree(head, base_ref, references):
    with tempfile.TemporaryDirectory(prefix='manifest-lane-') as directory:
        root = Path(directory)
        texts = {}
        for name in MANIFESTS:
            row = head['entries'].get(name)
            require(row is not None and row['type'] == 'blob' and row['mode'] == '100644',
                    'both maintained manifests must be ordinary files')
            texts[name] = docs_lane.blob(release.PLATFORM_REPOSITORY, row['sha']).decode('utf-8')
            path = root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(texts[name], encoding='utf-8')
        selection.validate_current_manifests(root)
        return {name: check_plan(*selection.parse_manifest(texts[name], name, name == MANIFESTS[1]),
                                 base_ref, references) for name in MANIFESTS}
