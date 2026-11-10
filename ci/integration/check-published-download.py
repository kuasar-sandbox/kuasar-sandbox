#!/usr/bin/env python3
"""Check published bytes with their bundled guide or an explicit reviewed guide."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = 'kuasar-sandbox/kuasar-sandbox'
VERSION = r'release-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-preview\.[0-9]{8}(?:\.[1-9][0-9]*)?)?'
GUIDE_SOURCES = ('bundled', 'current-checkout')


def download_asset(asset, destination, *, env=None):
    subprocess.run(['bash', '-c',
        'source "$1"; github_download_asset "$2" "$3" "$4"; verify_github_asset "$4" "$5"',
        'published-asset', str(ROOT / 'release/lib.sh'), REPOSITORY,
        str(asset['id']), str(destination), json.dumps(asset)], check=True, env=env)


def download_anonymous_asset(asset, destination, *, env):
    """Read a public asset by its fixed ID without publisher credentials."""
    if (type(asset.get('id')) is not int or asset['id'] <= 0
            or type(asset.get('size')) is not int or asset['size'] <= 0
            or not isinstance(asset.get('digest'), str)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', asset['digest'])):
        raise ValueError('anonymous asset lacks native identity, size or digest')
    destination = Path(destination)
    anonymous_env = {key: value for key, value in env.items()
                     if key not in ('GH_TOKEN', 'GITHUB_TOKEN')}
    with tempfile.NamedTemporaryFile(prefix=destination.name + '.tmp.',
                                     dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        subprocess.run([
            'curl', '--disable', '--fail', '--show-error', '--silent', '--location',
            '--retry', '4', '--retry-all-errors', '--connect-timeout', '10', '--max-time', '900',
            '-H', 'Accept: application/octet-stream',
            '-H', 'X-GitHub-Api-Version: 2022-11-28',
            '--output', str(temporary),
            f'https://api.github.com/repos/{REPOSITORY}/releases/assets/{asset["id"]}',
        ], env=anonymous_env, check=True)
        if temporary.stat().st_size != asset['size']:
            raise ValueError('anonymous GitHub asset size mismatch: ' + destination.name)
        hasher = hashlib.sha256()
        with temporary.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                hasher.update(chunk)
        if 'sha256:' + hasher.hexdigest() != asset['digest']:
            raise ValueError('anonymous GitHub asset digest mismatch: ' + destination.name)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def check_node_infra(release, root, version, arch, env):
    """Check the new delivery independently while retaining historical guides."""
    sys.path.insert(0, str(ROOT / 'release'))
    import node_infra_assets as node
    import selection
    import workbench_registry as registry
    rows = [item for item in release['assets'] if item['name'].startswith('node-infra-')]
    if not rows:
        return  # Historical aggregate asset sets remain unchanged.
    expected = {selection.node_infra_archive(version, item) for item in node.ARCHES}
    expected.add(selection.node_infra_deploy_archive(version))
    node.require(len(rows) == len({item['name'] for item in rows}) and expected <= {item['name'] for item in rows},
                 'published node-infra asset set is incomplete or duplicated')
    for item in rows:
        node.require(type(item.get('id')) is int and item['id'] > 0
                     and type(item.get('size')) is int and item['size'] > 0
                     and item.get('state') == 'uploaded'
                     and isinstance(item.get('digest'), str) and node.DIGEST.fullmatch(item['digest']),
                     'published node-infra asset lacks native identity, size or digest')
    source = release.get('target_commitish')
    node.require(isinstance(source, str) and re.fullmatch('[0-9a-f]{40}', source),
                 'published node-infra source is not an exact commit')
    assets = root / 'node-infra-assets'
    assets.mkdir()
    by_name = {item['name']: item for item in rows}
    deploy_name = selection.node_infra_deploy_archive(version)
    download_anonymous_asset(by_name[deploy_name], assets / deploy_name, env=env)
    document = node.read_deploy(assets / deploy_name, version, source, arches=node.ARCHES)
    if document['schema'] == 2:
        expected.update(selection.node_infra_docker_archive(version, item) for item in node.ARCHES)
    node.require({item['name'] for item in rows} == expected, 'published node-infra asset set is incomplete or duplicated')
    for image in document['images'].values():
        for key in ('archive', 'docker_archive'):
            if key not in image:
                continue
            item = by_name[image[key]['name']]
            node.require(item['size'] == image[key]['size']
                         and item['digest'] == 'sha256:' + image[key]['sha256'],
                         'published image asset differs from the deployment document')
    image = document['images'][arch]
    download_anonymous_asset(by_name[image['archive']['name']], assets / image['archive']['name'], env=env)
    if 'docker_archive' in image:
        name = image['docker_archive']['name']
        download_anonymous_asset(by_name[name], assets / name, env=env)
    node.install_inputs(assets, root / 'node-infra', version, arch, revision=source)
    anonymous = root / 'node-infra-anonymous.json'
    anonymous.write_text('{"auths":{}}\n')
    raw = registry.inspect_raw(node.REPOSITORY + ':' + version, anonymous)
    node.require(raw == node.index_bytes(document['images']) and registry.digest(raw) == document['index_digest'],
                 'public node-infra index differs from the downloaded deployment package')
    raw = registry.inspect_raw(image['reference'], anonymous)
    node.require(registry.digest(raw) == image['manifest_digest'], 'public node-infra native image differs')
    readback = root / 'node-infra-readback.tar'
    subprocess.run(['skopeo', 'copy', '--preserve-digests', '--authfile', str(anonymous),
                    'docker://' + image['reference'], 'oci-archive:' + str(readback)],
                   env=env, check=True, timeout=1800)
    observed = node.inspect_oci(readback, arch, version=version, revision=source)
    node.require(observed['image_id'] == image['id'] and observed['manifest_digest'] == image['manifest_digest'],
                 'public node-infra layers/config differ from the downloaded image')
    readback.unlink()
    print('Published node-infra download and public OCI bytes passed:', arch, image['reference'])


def platform_asset(release, version):
    if not re.fullmatch(VERSION, version):
        raise ValueError('invalid aggregate version')
    if (release.get('tag_name') != version or release.get('draft') is not False
            or release.get('prerelease') != ('-preview.' in version)):
        raise ValueError('expected a published aggregate Release')
    assets = [a for a in release['assets'] if a['name'] == f'platform-{version}.tar.gz']
    if len(assets) != 1:
        raise ValueError('expected exactly one platform package')
    asset = assets[0]
    if (type(asset.get('id')) is not int or asset['id'] <= 0
            or type(asset.get('size')) is not int or asset['size'] <= 0
            or asset.get('state') != 'uploaded'
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', asset.get('digest') or '')):
        raise ValueError('platform asset lacks native identity, size or digest')
    return asset


def guide_commands(text):
    fence = chr(96) * 3
    blocks = re.findall(r'(?m)^' + fence + r'bash\n(.*?)^' + fence + r'\s*$', text, re.S)
    if len(blocks) != 3 or any(not block.strip() for block in blocks):
        raise ValueError('expected download, installation and image-import commands')
    return '\n'.join(blocks)


def guide_script(archive, language):
    suffix = '_zh' if language == 'zh' else ''
    paths = {prefix + f'guide/{name}{suffix}.md'
             for name in ('installation', 'download') for prefix in ('', './')}
    with tarfile.open(archive, 'r:gz') as stream:
        matches = [m for m in stream.getmembers() if m.name in paths]
        if len(matches) != 1:
            raise ValueError('expected one bundled download guide')
        member = matches[0]
        if not member.isfile() or member.mode & 0o6002 or not 0 < member.size <= 1024 * 1024:
            raise ValueError('unsafe bundled download guide')
        text = stream.extractfile(member).read().decode('utf-8')
    return guide_commands(text)


def validate_guide_source(source, revision):
    if source not in GUIDE_SOURCES:
        raise ValueError('unsupported guide source')
    if source == 'bundled':
        if revision is not None:
            raise ValueError('bundled guides do not accept a guide revision')
    elif not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('current-checkout requires a full lowercase 40-character guide revision')


def checkout_guide_script(checkout, revision, language):
    validate_guide_source('current-checkout', revision)
    git = ['git', '-C', str(checkout)]
    head = subprocess.check_output(git + ['rev-parse', '--verify', 'HEAD^{commit}'], text=True).strip()
    if head != revision:
        raise ValueError('guide revision must equal the exact current checkout HEAD')
    suffix = '_zh' if language == 'zh' else ''
    relative = f'docs/installation{suffix}.md'
    status = subprocess.check_output(
        git + ['status', '--porcelain=v1', '--untracked-files=all', '--', relative], text=True)
    if status:
        raise ValueError('guide checkout has staged or worktree changes')
    committed = subprocess.check_output(git + ['show', f'{revision}:{relative}'])
    path = checkout / relative
    if (path.is_symlink() or not path.is_file() or not 0 < len(committed) <= 1024 * 1024
            or path.read_bytes() != committed):
        raise ValueError('guide worktree does not match its committed regular file')
    return guide_commands(committed.decode('utf-8'))


def selected_guide_script(archive, language, *, source='bundled', revision=None, checkout=ROOT):
    validate_guide_source(source, revision)
    if source == 'bundled':
        return guide_script(archive, language)
    return checkout_guide_script(checkout, revision, language)


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--guide-source', choices=GUIDE_SOURCES, default='bundled',
                        help='bundled by default; current-checkout explicitly checks a revised online guide')
    parser.add_argument('--guide-revision',
                        help='full exact checkout HEAD, required only for current-checkout')
    return parser


def main():
    parser = argument_parser()
    args = parser.parse_args()
    if not re.fullmatch(VERSION, args.version):
        parser.error('invalid aggregate version')
    try:
        validate_guide_source(args.guide_source, args.guide_revision)
    except ValueError as error:
        parser.error(str(error))
    arch = platform.machine()
    if arch not in ('x86_64', 'aarch64'):
        parser.error('unsupported native architecture')
    args.root.mkdir(parents=True, exist_ok=False)
    root = args.root.resolve()
    release = json.loads(subprocess.check_output(
        ['gh', 'api', f'repos/{REPOSITORY}/releases/tags/{args.version}'], text=True))
    asset = platform_asset(release, args.version)
    archive = root / 'bootstrap-platform.tar.gz'
    download_asset(asset, archive)
    script = selected_guide_script(archive, 'zh' if arch == 'aarch64' else 'en',
                                   source=args.guide_source, revision=args.guide_revision)
    print('Guide source:', args.guide_source, 'guide revision:', args.guide_revision or 'bundled',
          'aggregate:', args.version, 'platform digest:', asset['digest'], flush=True)
    # Execute the explicitly selected guide's unmodified commands anonymously.
    # The aggregate manifest and every downloaded product still belong to args.version.
    env = {key: value for key, value in os.environ.items() if key not in ('GH_TOKEN', 'GITHUB_TOKEN')}
    env.update(RELEASE_VERSION=args.version, DOWNLOAD_WORKBENCH='1', BOOTSTRAP_PLATFORM=str(archive),
               PUBLISHED_GUIDE_SOURCE=args.guide_source)
    script += """
cmp "$BOOTSTRAP_PLATFORM" "$DOWNLOAD_DIR/platform-$RELEASE_VERSION.tar.gz"
test "$(wc -l < "$DOWNLOAD_DIR/files.tsv")" -eq 8
# The imported archive and the public native version tag must resolve to one image.
docker pull "$IMAGE_TAG"
test "$(docker image inspect --format '{{.Id}}' "$IMAGE_TAG")" = "$IMAGE"
docker run --rm --entrypoint /bin/true "$IMAGE"
printf 'Published %s guide passed: %s %s, offline/GHCR image %s\n' "$PUBLISHED_GUIDE_SOURCE" "$RELEASE_VERSION" "$ARCH" "$IMAGE"
"""
    subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=root, env=env, check=True)
    sys.path.insert(0, str(ROOT / 'test/e2e/lib'))
    import helper_contract
    records = helper_contract.packages(root / f'kuasar-{args.version}/test/e2e/helpers', runtime_only=True)
    print('Installed runtime helper counts:', {arch: len(items) for arch, items in records.items()})
    check_node_infra(release, root, args.version, arch, env)


if __name__ == '__main__':
    main()
