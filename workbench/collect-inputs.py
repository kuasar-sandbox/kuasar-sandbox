#!/usr/bin/env python3
"""Collect external image bytes selected by the existing public E2E runner."""
import argparse
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('workbench_e2e', str(ROOT / 'test/e2e/e2e'))
spec = importlib.util.spec_from_loader(loader.name, loader)
runner = importlib.util.module_from_spec(spec)
loader.exec_module(runner)
workspace = runner.workspace


def registry_reference(reference):
    """Make Docker Hub references explicit, avoiding host short-name policies."""
    repository = reference.split('@', 1)[0]
    if ':' in repository.rsplit('/', 1)[-1]:
        repository = repository.rsplit(':', 1)[0]
    first = repository.split('/', 1)[0]
    prefix = '' if '/' in repository and ('.' in first or ':' in first or first == 'localhost') else 'docker.io/'
    if '/' not in repository:
        prefix += 'library/'
    return prefix + reference, prefix + repository


def resolve(requests, environment):
    responses = {}
    records = []
    for arch, label, request in requests:
        reference, repository = registry_reference(request['reference'])
        if reference not in responses:
            raw = subprocess.check_output(['skopeo', 'inspect', '--raw', 'docker://' + reference],
                                          env=environment, timeout=180)
            identity = workspace.sha256_bytes(raw)
            if '@' in reference:
                workspace.require(reference.rsplit('@', 1)[1] == identity, 'registry response differs from requested digest')
            responses[reference] = raw, identity
        raw, identity = responses[reference]
        document = json.loads(raw)
        record = {**request, 'registry_digest': identity, 'resolved_reference': repository + '@' + identity,
                  'archive': f'images/{label}-{arch}.tar'}
        if 'manifests' in document:
            os_name, go_arch = request['platform'].split('/')
            matches = [entry for entry in document['manifests'] if
                       entry.get('platform', {}).get('os') == os_name and
                       entry.get('platform', {}).get('architecture') == go_arch]
            workspace.require(len(matches) == 1, f'no unique registry manifest for {reference} {request["platform"]}')
            record.update(index=raw.decode('utf-8'), index_digest=identity)
            descriptor = matches[0]
            raw = subprocess.check_output(['skopeo', 'inspect', '--raw', 'docker://' + repository + '@' + descriptor['digest']],
                                          env=environment, timeout=180)
            workspace.require(workspace.sha256_bytes(raw) == descriptor['digest'] and len(raw) == descriptor['size'],
                              'registry target manifest differs from index descriptor')
        manifest = json.loads(raw)
        record.update(manifest=raw.decode('utf-8'), manifest_digest=workspace.sha256_bytes(raw),
                      image_id=manifest['config']['digest'])
        workspace.verify_registry_identity(record, {'image_id': record['image_id'],
            'config_size': manifest['config']['size'], 'layers': len(manifest['layers'])})
        records.append(record)
    return records


def collect(cases, arches, output):
    requests = [(arch, label, request) for arch in arches
                for label, request in workspace.external_image_requests(cases, arch).items()]
    output = Path(output)
    workspace.require(not output.is_symlink(), 'dependency output cannot be a symlink')
    output.mkdir(parents=True, exist_ok=True)
    final = output / 'images.json'
    if final.exists() or final.is_symlink():
        for arch in arches:
            workspace.local_image_inputs(workspace.external_image_requests(cases, arch), output, True)
        return final
    pending = output / '.images.pending.json'
    # An incomplete collection retains its exact tag resolutions for retry.
    # This is a temporary flat images.json, never a prepared input directory.
    workspace.require(pending.exists() or not any(output.iterdir()), 'output is not an owned collection or empty directory')
    with tempfile.TemporaryDirectory(prefix='workbench-registry-') as directory:
        auth = Path(directory) / 'auth.json'
        auth.write_text('{"auths":{}}\n')
        auth.chmod(0o600)
        environment = {**os.environ, 'REGISTRY_AUTH_FILE': str(auth)}
        if pending.exists():
            records = json.loads(workspace.dependency_file(output, pending.name).read_text())
            expected = [(request['reference'], request['platform'], f'images/{label}-{arch}.tar')
                        for arch, label, request in requests]
            workspace.require([(record['reference'], record['platform'], record['archive']) for record in records] == expected,
                              'resumed image selection differs from requested cases/architectures')
        else:
            records = resolve(requests, environment)
            pending.write_text(json.dumps(records, sort_keys=True) + '\n')
            pending.chmod(0o444)
        for record in records:
            manifest = json.loads(record['manifest'])
            workspace.verify_registry_identity(record, {'image_id': record['image_id'],
                'config_size': manifest['config']['size'], 'layers': len(manifest['layers'])})
        workspace.require(not (output / 'images').is_symlink(), 'unsafe image archive directory')
        (output / 'images').mkdir(exist_ok=True)
        for record in records:
            path = output / workspace.relative_input(record['archive'])
            workspace.require(not path.is_symlink(), 'unsafe image archive symbolic link')
            if not path.exists():
                temporary = path.with_suffix('.part')
                workspace.require(not temporary.is_symlink(), 'unsafe partial image archive')
                if temporary.exists():
                    workspace.require(temporary.is_file(), 'partial image archive is not a file')
                    temporary.unlink()
                _, repository = registry_reference(record['reference'])
                os_name, go_arch = record['platform'].split('/')
                # Keep a stable archive tag: case cleanup can remove its own
                # temporary registry tag without deleting the prepared image.
                archive_tag = repository + ':workbench-' + record['image_id'].split(':')[1]
                subprocess.run(['skopeo', '--override-os', os_name, '--override-arch', go_arch,
                    'copy', '--retry-times', '2', 'docker://' + repository + '@' + record['manifest_digest'],
                    'docker-archive:' + str(temporary) + ':' + archive_tag], env=environment, check=True, timeout=900)
                temporary.rename(path)
            path = workspace.dependency_file(output, record['archive'])
            image = workspace.verify_image_archive(path, record['platform'])
            workspace.require(image['image_id'] == record['image_id'], 'collected image config differs from selection')
            workspace.verify_registry_identity(record, image)
            record['sha256'] = workspace.digest(path)
            path.chmod(0o444)
        temporary = output / '.images.complete.json'
        workspace.require(not temporary.is_symlink(), 'unsafe completed image description')
        if temporary.exists():
            workspace.require(temporary.is_file(), 'completed image description is not a file')
            temporary.unlink()
        with temporary.open('x') as stream:
            stream.write(json.dumps(records, sort_keys=True) + '\n')
        temporary.chmod(0o444)
        temporary.rename(final)
        pending.unlink()
    return final


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True, help='assembled canonical E2E case directory')
    parser.add_argument('--arch', action='append', choices=('x86_64', 'aarch64'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    workspace.require(len(args.arch) == len(set(args.arch)), 'duplicate collection architecture')
    print(collect([case.name for case in runner.discover(args.cases)], args.arch, args.output.absolute()))


if __name__ == '__main__':
    main()
