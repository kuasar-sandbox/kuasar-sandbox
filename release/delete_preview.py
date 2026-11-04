"""Delete a confirmed Preview, retaining ambiguous or concurrently changed objects."""
import argparse
import base64
import json
import re

import preview_coordinator as coordinator
import preview_gc as gc
import workbench_gc


def require(condition, message):
    if not condition:
        raise ValueError(message)


def release_for(tag):
    rows = [row for row in coordinator.paginated(
        f'repos/{coordinator.PLATFORM_REPOSITORY}/releases?per_page=100') if row.get('tag_name') == tag]
    require(len(rows) <= 1, 'multiple releases use the Preview tag')
    return rows[0] if rows else None


def read_snapshot(ref, tag):
    state = coordinator.api(f'repos/{coordinator.PLATFORM_REPOSITORY}/contents/releases/daily-preview.yaml?ref={coordinator.quote(ref, safe="")}')
    text = base64.b64decode(state['content'].replace('\n', ''), validate=True).decode()
    selected, _, units = coordinator.selection.parse_historical_manifest(text, ref, True)
    require(selected == tag, 'source does not select the requested aggregate Preview')
    return gc.CanonicalSnapshot(ref, units, coordinator.selection.delivery(
        coordinator.selection.read_simple_yaml(text, ref), ref))


def canonical_gc_snapshot(tag, sha, release):
    stable = tag.split('-preview.', 1)[0]
    stable_release = coordinator.api(f'repos/{coordinator.PLATFORM_REPOSITORY}/releases/tags/{stable}')
    stable_sha = coordinator.tag_sha(coordinator.PLATFORM_REPOSITORY, stable)
    require(stable_sha is not None, 'GC requires the version line Stable tag')
    gc.stable_release(stable, {stable: stable_release}, {stable: stable_sha})
    require(gc.grace_elapsed(stable_release['published_at'])[0], 'GC retention window has not elapsed')
    choices = gc.canonical_snapshots(stable_sha, stable).get(tag, [])
    return gc.resolve_canonical_snapshot(tag, release, sha, choices, stable_sha=stable_sha)


def admitted_tagless_snapshot(tag, sha):
    line = tag.removeprefix('release-v').split('-preview.', 1)[0].rsplit('.', 1)[0]
    for branch in ('main', f'release/v{line}.x'):
        tip = coordinator.branch_sha(coordinator.PLATFORM_REPOSITORY, branch)
        if tip is None:
            continue
        # Full-history trusted checkout normally already contains this tip.
        # If the branch just advanced, refresh that branch, never a guessed SHA.
        if not gc.git('cat-file', '-t', tip, check=False):
            gc.git('fetch', '--no-tags', 'origin', f'refs/heads/{branch}')
        if gc.first_parent_contains(sha, tip):
            return read_snapshot(sha, tag)
    raise ValueError('tagless publication source is not on an admitted branch; retain for inspection')


def delete_endpoint(endpoint, read_endpoint=None):
    result = coordinator.gh('api', '--method', 'DELETE', endpoint, '--silent', check=False)
    require(result.returncode == 0 or '(HTTP 404)' in result.stderr,
            result.stderr.strip() or 'GitHub deletion failed')
    require(coordinator.api_optional(read_endpoint or endpoint) is None, 'GitHub object remains after deletion')


def collect(tag, sha, mode):
    require(gc.AGGREGATE_PREVIEW_RE.fullmatch(tag), 'invalid aggregate Preview tag')
    require(re.fullmatch(r'[0-9a-f]{40}', sha), 'source-sha must be a full lowercase SHA')
    require(mode in ('incomplete', 'gc'), 'mode must be incomplete or gc')
    repository = coordinator.PLATFORM_REPOSITORY
    tag_endpoint = f'repos/{repository}/git/ref/tags/{tag}'
    original_ref = coordinator.api_optional(tag_endpoint)
    release = release_for(tag)
    if original_ref is None and release is None:
        return {'deleted': [], 'status': 'already absent'}
    if original_ref is not None:
        require(coordinator.resolve_tag_target(repository, original_ref.get('object')) == sha, 'tag source identity changed')
    elif release is not None:
        require(release.get('target_commitish') == sha, 'tagless Release source identity is unknown or changed')
    if release is not None:
        require(release.get('draft') is True or (release.get('draft') is False and release.get('prerelease') is True),
                'refusing to delete a non-preview release')
        require(release.get('immutable') is not True, 'immutable Preview cannot be deleted')
        require(type(release.get('id')) is int and release['id'] > 0, 'invalid Release ID')
    if mode == 'gc':
        snapshot = canonical_gc_snapshot(tag, sha, release)
    elif original_ref is None:
        snapshot = admitted_tagless_snapshot(tag, sha)
    else:
        snapshot = read_snapshot(tag, tag)
    expected = coordinator.platform_asset_names_for_components(tag, snapshot.components, delivery=snapshot.delivery)
    dual = expected | {name.replace('x86_64', 'aarch64') for name in expected}
    if release is not None:
        complete = coordinator.complete_assets(release, [expected, dual]) and release.get('draft') is False
        require(mode != 'incomplete' or not complete, 'refusing incomplete recovery for a complete aggregate Preview')
    require(not any(coordinator.run_active(run) for run in coordinator.aggregate_runs(tag)),
            'aggregate publication is still active')
    # Human prose is irrelevant; object identity, state and assets must be stable.
    fields = ('id', 'tag_name', 'target_commitish', 'draft', 'prerelease', 'immutable', 'assets')
    def recheck():
        require(coordinator.api_optional(tag_endpoint) == original_ref, 'tag changed before deletion')
        current = release_for(tag)
        require((current is None and release is None) or (current is not None and release is not None and
                all(current.get(key) == release.get(key) for key in fields)), 'Release changed before deletion')
    recheck()
    registry_result = None
    if snapshot.delivery == coordinator.selection.DELIVERY:
        registry_result = workbench_gc.collect(tag, sha)
    recheck()  # Registry traversal can be slow; do not delete from stale state.
    deleted = []
    if release is not None:
        delete_endpoint(f'repos/{repository}/releases/{release["id"]}')
        deleted.append('release')
    if original_ref is not None:
        require(release_for(tag) is None, 'a Release appeared before tag deletion; retain the tag')
        fresh = coordinator.api_optional(tag_endpoint)
        if fresh is not None:
            require(fresh == original_ref, 'tag changed before deletion')
            delete_endpoint(f'repos/{repository}/git/refs/tags/{tag}', tag_endpoint)
        deleted.append('tag')
    return {'deleted': deleted, 'registry': registry_result}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tag')
    parser.add_argument('source_sha')
    parser.add_argument('mode', choices=('incomplete', 'gc'))
    args = parser.parse_args()
    print(json.dumps(collect(args.tag, args.source_sha, args.mode), sort_keys=True))
