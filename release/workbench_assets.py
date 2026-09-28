"""Validate workbench asset bytes and flat image receipts for one aggregate."""
import argparse
import json
from pathlib import Path
import re
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'test/e2e/lib'))
import workspace
import selection

ARCHES = ('x86_64', 'aarch64')
ASSET_LIMIT = 2 * 1024**3


def check_size(path):
    size = path.stat().st_size
    workspace.require(0 < size < ASSET_LIMIT,
                      f'workbench compressed asset must be smaller than 2 GiB; actual {size} bytes: {path.name}. '
                      'Preserve all offline inputs and the single archive per architecture; do not publish this asset.')
    return size


def validate(directory, version, arch, revision, *, receipt_directory=None):
    workspace.require(arch in ARCHES and re.fullmatch(r'[0-9a-f]{40}', revision), 'invalid workbench source/architecture')
    path = directory / selection.workbench_archive(version, arch)
    receipt = (receipt_directory or directory) / f'workbench-{arch}.json'
    workspace.require(path.is_file() and not path.is_symlink() and receipt.is_file() and not receipt.is_symlink(),
                      f'missing/unsafe workbench archive or receipt: {arch}')
    size = check_size(path)
    record = json.loads(receipt.read_text())
    workspace.require(record['aggregate_version'] == version and record['arch'] == arch
                      and record['source_revision'] == revision and record['archive'] == path.name,
                      'workbench receipt belongs to another aggregate/source/architecture')
    workspace.require(record['size'] == size and record['sha256'] == workspace.digest(path), 'workbench archive bytes differ from stage')
    with path.open('rb') as stream:
        workspace.require(stream.read(2) == b'\x1f\x8b', 'workbench must be a gzip-compressed Docker image archive')
    verified = workspace.verify_image_archive(path, 'linux/' + {'x86_64': 'amd64', 'aarch64': 'arm64'}[arch])
    workspace.require(verified['image_id'] == record['image_id'], 'workbench image ID differs from verified archive config')
    with tarfile.open(path, 'r:gz') as archive:
        manifest = json.load(archive.extractfile('manifest.json'))
        workspace.require(len(manifest) == 1 and manifest[0]['RepoTags'] ==
                          ['ghcr.io/kuasar-sandbox/workbench:' + version.removeprefix('release-')],
                          'workbench archive must import the same aggregate registry tag')
        config = json.load(archive.extractfile(manifest[0]['Config']))
        labels = config.get('config', {}).get('Labels', {})
        workspace.require(labels.get('org.opencontainers.image.version') == version
                          and labels.get('org.opencontainers.image.revision') == revision
                          and labels.get('org.opencontainers.image.source') == 'https://github.com/kuasar-sandbox/kuasar-sandbox',
                          'workbench config labels differ from selected release source')
    workspace.require(isinstance(record.get('compression_seconds'), (int, float)) and record['compression_seconds'] >= 0,
                      'missing actual workbench compression timing')
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('version')
    parser.add_argument('revision')
    parser.add_argument('--receipts', type=Path)
    args = parser.parse_args()
    print(json.dumps({arch: validate(args.directory, args.version, arch, args.revision,
                                   receipt_directory=args.receipts) for arch in ARCHES}, sort_keys=True))
