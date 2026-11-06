#!/usr/bin/env python3
"""Run the published package's native download/install guide, then compare GHCR."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = 'kuasar-sandbox/kuasar-sandbox'
VERSION = r'release-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-preview\.[0-9]{8}(?:\.[1-9][0-9]*)?)?'


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
    fence = chr(96) * 3
    blocks = re.findall(r'(?m)^' + fence + r'bash\n(.*?)^' + fence + r'\s*$', text, re.S)
    if len(blocks) != 3 or any(not block.strip() for block in blocks):
        raise ValueError('expected download, installation and image-import commands')
    return '\n'.join(blocks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(VERSION, args.version):
        parser.error('invalid aggregate version')
    arch = platform.machine()
    if arch not in ('x86_64', 'aarch64'):
        parser.error('unsupported native architecture')
    args.root.mkdir(parents=True, exist_ok=False)
    root = args.root.resolve()
    release = json.loads(subprocess.check_output(
        ['gh', 'api', f'repos/{REPOSITORY}/releases/tags/{args.version}'], text=True))
    asset = platform_asset(release, args.version)
    archive = root / 'bootstrap-platform.tar.gz'
    subprocess.run(['bash', '-c',
        'source "$1"; github_download_asset "$2" "$3" "$4"; verify_github_asset "$4" "$5"',
        'published-guide-bootstrap', str(ROOT / 'release/lib.sh'), REPOSITORY,
        str(asset['id']), str(archive), json.dumps(asset)], check=True)
    script = guide_script(archive, 'zh' if arch == 'aarch64' else 'en')
    # Execute the package's unmodified commands anonymously, as documented.
    env = {key: value for key, value in os.environ.items() if key not in ('GH_TOKEN', 'GITHUB_TOKEN')}
    env.update(RELEASE_VERSION=args.version, DOWNLOAD_WORKBENCH='1', BOOTSTRAP_PLATFORM=str(archive))
    script += """
cmp "$BOOTSTRAP_PLATFORM" "$DOWNLOAD_DIR/platform-$RELEASE_VERSION.tar.gz"
test "$(wc -l < "$DOWNLOAD_DIR/files.tsv")" -eq 8
# The imported archive and the public native version tag must resolve to one image.
docker pull "$IMAGE_TAG"
test "$(docker image inspect --format '{{.Id}}' "$IMAGE_TAG")" = "$IMAGE"
docker run --rm --entrypoint /bin/true "$IMAGE"
printf 'Published guide passed: %s %s, offline/GHCR image %s\n' "$RELEASE_VERSION" "$ARCH" "$IMAGE"
"""
    subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=root, env=env, check=True)
    sys.path.insert(0, str(ROOT / 'test/e2e/lib'))
    import helper_contract
    records = helper_contract.packages(root / f'kuasar-{args.version}/test/e2e/helpers', runtime_only=True)
    print('Installed runtime helper counts:', {arch: len(items) for arch, items in records.items()})


if __name__ == '__main__':
    main()
