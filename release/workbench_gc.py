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


def protected_digests(releases, target):
    protected = set()
    for release in releases:
        if release.get('tag_name') == target:
            continue
        text = release.get('body') or ''
        if registry.MARKER not in text:
            continue
        registry.require(text.count(registry.MARKER) == 1, 'ambiguous live release validation binding')
        binding = json.loads(text.split(registry.MARKER)[1].split(' -->')[0])
        proof = binding.get('registry')
        if proof is None:
            continue  # Historical releases predate workbench publication.
        registry.require(proof.get('reference', '').startswith(registry.REPOSITORY + ':'), 'foreign live registry binding')
        protected.add(proof['digest'])
        protected.update(item['digest'] for item in proof['architectures'].values())
    return protected


def candidates(versions, version, protected):
    tag = version.removeprefix('release-')
    owned = {tag, *(tag + '-' + arch for arch in registry.PLATFORMS)}
    result, retained = [], []
    for item in versions:
        tags = set(item.get('metadata', {}).get('container', {}).get('tags', []))
        if not tags & owned:
            continue  # Never infer ownership of untagged or unrelated versions.
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
        labels = config.get('config', {}).get('Labels', {})
        registry.require(labels.get('org.opencontainers.image.source') == SOURCE
                         and labels.get('org.opencontainers.image.version') == version
                         and labels.get('org.opencontainers.image.revision') == revision,
                         'refusing to delete an image from another source/version')


def shared_closure(versions, version, protected, auth):
    tag = version.removeprefix('release-')
    owned = {tag, *(tag + '-' + arch for arch in registry.PLATFORMS)}
    protected = set(protected)
    for item in versions:
        tags = set(item.get('metadata', {}).get('container', {}).get('tags', []))
        if tags - owned:
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
    releases = coordinator.paginated(f'repos/{coordinator.PLATFORM_REPOSITORY}/releases?per_page=100')
    protected = protected_digests(releases, version)
    versions = coordinator.paginated(PACKAGE + '/versions?per_page=100')
    token, actor = os.environ.get('GH_TOKEN'), os.environ.get('GITHUB_ACTOR')
    registry.require(token and actor, 'registry GC needs the package-authorized job identity')
    with tempfile.TemporaryDirectory(prefix='workbench-gc-') as directory:
        auth = Path(directory) / 'config.json'
        auth.write_text(json.dumps({'auths': {'ghcr.io': {'auth': base64.b64encode((actor + ':' + token).encode()).decode()}}}))
        auth.chmod(0o600)
        protected = shared_closure(versions, version, protected, auth)
        pending, retained = candidates(versions, version, protected)
        # Validate every selected image before deleting any. Shared versions are
        # retained whole: the package API cannot safely delete only one tag.
        for item in pending:
            verify_source(item['name'], version, revision, auth)
        deleted = []
        for item in pending:
            # Re-read tags just before deletion; a newly shared version is no
            # longer ours to remove. Never use a registry-wide prune operation.
            current = coordinator.api(PACKAGE + '/versions/' + str(item['id']))
            fresh, shared = candidates([current], version, protected)
            if shared or not fresh:
                retained.append(item['id'])
                continue
            registry.require(current['name'] == item['name'], 'package identity changed before deletion')
            coordinator.gh('api', '--method', 'DELETE', PACKAGE + '/versions/' + str(item['id']), '--silent')
            deleted.append(item['id'])
    return {'deleted': deleted, 'retained_shared': retained}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version')
    parser.add_argument('revision')
    args = parser.parse_args()
    print(json.dumps(collect(args.version, args.revision), sort_keys=True))
