"""Remove only an authorized Preview's owned, unshared workbench versions."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import tempfile

import preview_coordinator as coordinator
import selection
import workbench_registry as registry

PACKAGE = 'orgs/kuasar-sandbox/packages/container/workbench'
SOURCE = 'https://github.com/' + coordinator.PLATFORM_REPOSITORY


def owned_tags(version):
    tag = version.removeprefix('release-')
    return {tag, *(tag + '-' + arch for arch in registry.PLATFORMS)}


def candidates(versions, version, protected, descendants=()):
    owned = owned_tags(version)
    result, retained = [], []
    for item in versions:
        tags = set(item.get('metadata', {}).get('container', {}).get('tags', []))
        if not tags & owned and item['name'] not in descendants:
            continue  # Untagged images need an observed target parent in this run.
        registry.require(isinstance(item.get('id'), int) and item['id'] > 0
                         and re.fullmatch(r'sha256:[0-9a-f]{64}', item.get('name', '')), 'invalid owned package version')
        if tags - owned or item['name'] in protected:
            retained.append(item['id'])
        else:
            result.append(item)
    return result, retained


def verify_source(identity, version, revision, auth, *, allow_index=True):
    raw = registry.inspect_raw(registry.REPOSITORY + '@' + identity, auth)
    registry.require(registry.digest(raw) == identity, 'registry GC manifest identity differs')
    document = json.loads(raw)
    if 'manifests' in document:
        registry.require(allow_index, 'nested registry index is not a workbench image')
        platforms = set()
        registry.require(len(document['manifests']) == 2, 'owned workbench index is incomplete')
        for descriptor in document['manifests']:
            platform = descriptor.get('platform', {})
            arch = platform.get('architecture')
            registry.require(platform.get('os') == 'linux' and arch in registry.PLATFORMS.values() and arch not in platforms,
                             'owned index has unexpected/duplicate platform')
            platforms.add(arch)
            verify_source(descriptor['digest'], version, revision, auth, allow_index=False)
    else:
        raw_config = registry.inspect_raw(registry.REPOSITORY + '@' + identity, auth, config=True)
        registry.require(document.get('config', {}).get('digest') == registry.digest(raw_config), 'GC config identity differs')
        config = json.loads(raw_config)
        registry.require(config.get('os') == 'linux' and config.get('architecture') in registry.PLATFORMS.values(),
                         'owned image has an unexpected platform')
        labels = config.get('config', {}).get('Labels', {})
        registry.require(labels.get('org.opencontainers.image.source') == SOURCE
                         and labels.get('org.opencontainers.image.version') == version
                         and labels.get('org.opencontainers.image.revision') == revision,
                         'refusing to delete an image from another source/version')


def shared_closure(versions, version, protected, auth, *, scope=None):
    owned = owned_tags(version)
    if scope is None:
        scope = {item['name'] for item in versions
                 if set(item.get('metadata', {}).get('container', {}).get('tags', [])) & owned}
    protected = set(protected)
    for item in versions:
        tags = set(item.get('metadata', {}).get('container', {}).get('tags', []))
        # Include tagless, non-target indexes as protection roots. This broadens
        # protection, never the deletion scope.
        if tags - owned or item['name'] not in scope:
            protected.add(item['name'])
    # A shared multiarch parent protects its children even when those children
    # still carry only this Preview's architecture tags.
    pending = list(protected)
    visited = set()
    while pending:
        identity = pending.pop()
        if identity in visited:
            continue
        visited.add(identity)
        registry.require(len(visited) <= 10000 and re.fullmatch(r'sha256:[0-9a-f]{64}', identity),
                         'invalid or excessive shared registry references')
        raw = registry.inspect_raw(registry.REPOSITORY + '@' + identity, auth)
        registry.require(registry.digest(raw) == identity, 'shared registry identity differs')
        for descriptor in json.loads(raw).get('manifests', []):
            protected.add(descriptor['digest'])
            pending.append(descriptor['digest'])
    return protected


def collect(version, revision):
    registry.require(selection.AGGREGATE_RE.fullmatch(version) and '-preview.' in version
                     and re.fullmatch(r'[0-9a-f]{40}', revision), 'registry GC requires an exact Preview source')
    registry.require(not any(coordinator.run_active(run) for run in coordinator.aggregate_runs(version)),
                     'aggregate publication is still active')
    package = coordinator.api_optional(PACKAGE)
    if package is None:
        return {'deleted': [], 'retained_shared': []}
    registry.require(package.get('repository', {}).get('full_name') == coordinator.PLATFORM_REPOSITORY,
                     'workbench registry package is not owned by the platform repository')
    versions = coordinator.paginated(PACKAGE + '/versions?per_page=100')
    token, actor = os.environ.get('GH_TOKEN'), os.environ.get('GITHUB_ACTOR')
    registry.require(token and actor, 'registry GC needs the package-authorized job identity')
    with tempfile.TemporaryDirectory(prefix='workbench-gc-') as directory:
        auth = Path(directory) / 'config.json'
        auth.write_text(json.dumps({'auths': {'ghcr.io': {'auth': base64.b64encode((actor + ':' + token).encode()).decode()}}}))
        auth.chmod(0o600)
        tagged, retained = candidates(versions, version, set())
        descendants, indexes = set(), set()
        # Only exact target tags and children of their observed indexes may be
        # deleted. Existing untagged orphans are deliberately left alone.
        for item in tagged:
            raw = registry.inspect_raw(registry.REPOSITORY + '@' + item['name'], auth)
            registry.require(registry.digest(raw) == item['name'], 'registry GC manifest identity differs')
            document = json.loads(raw)
            if 'manifests' in document:
                indexes.add(item['name'])
                descendants.update(row['digest'] for row in document['manifests'])
        pending, shared = candidates(versions, version, set(), descendants)
        retained.extend(shared)
        scope = {item['name'] for item in pending}
        protected = shared_closure(versions, version, set(), auth, scope=scope)
        pending, shared = candidates(pending, version, protected, descendants)
        retained.extend(shared)
        # Validate every selected image before deleting any. Shared versions are
        # retained whole: the package API cannot safely delete only one tag.
        for item in pending:
            verify_source(item['name'], version, revision, auth)
        deleted, retained_roots = [], set()
        for item in sorted(pending, key=lambda row: (row['name'] not in indexes, row['id'])):
            # Re-read tags just before deletion; a newly shared version is no
            # longer ours to remove. Never use a registry-wide prune operation.
            current_versions = coordinator.paginated(PACKAGE + '/versions?per_page=100')
            current_candidates, _ = candidates(current_versions, version, set(), descendants)
            current_scope = scope & {row['name'] for row in current_candidates}
            protected = shared_closure(current_versions, version, retained_roots, auth, scope=current_scope)
            endpoint = PACKAGE + '/versions/' + str(item['id'])
            current = coordinator.api_optional(endpoint)
            if current is None:
                deleted.append(item['id'])
                continue
            fresh, shared = candidates([current], version, protected, descendants)
            if shared or not fresh:
                retained.append(item['id'])
                # The detail endpoint may observe a new shared/tagless parent
                # before pagination does. Protect its descendants for this run.
                retained_roots.add(current['name'])
                continue
            registry.require(current['name'] == item['name'], 'package identity changed before deletion')
            result = coordinator.gh('api', '--method', 'DELETE', endpoint, '--silent', check=False)
            registry.require(result.returncode == 0 or '(HTTP 404)' in result.stderr,
                             result.stderr.strip() or 'registry deletion failed')
            registry.require(coordinator.api_optional(endpoint) is None, 'registry version still exists after deletion')
            deleted.append(item['id'])
    return {'deleted': deleted, 'retained_shared': sorted(set(retained))}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version')
    parser.add_argument('revision')
    args = parser.parse_args()
    print(json.dumps(collect(args.version, args.revision), sort_keys=True))
