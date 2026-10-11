"""Read-only admission of original helper producer evidence and archive inventory.

This module never builds a historical helper or invents an owner declaration.
Its caller must first authenticate the published aggregate and its original plan.
"""
import importlib.util
import hashlib
import json
from pathlib import Path
import re
import tarfile
import struct
import subprocess

import helper_inputs

C = helper_inputs.contract
ROOT = Path(__file__).resolve().parents[1]


def gh(*arguments):
    return subprocess.run(['gh', *arguments], check=True, capture_output=True, text=True, timeout=120)


def api(path):
    return json.loads(gh('api', path).stdout)


def file_digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def producer(files, arch, run_id, original, roots):
    receipts = [value for name, value in files.items() if name.endswith('/receipt.json')]
    scopes = [value for name, value in files.items() if re.fullmatch(r'workbench-cache-scope-[^/]+\.json', name)]
    C.require(len(receipts) == len(scopes) == 1, 'ambiguous original helper producer evidence')
    receipt, scope = map(C.loads, (receipts[0], scopes[0]))
    C.require(receipt['arch'] == arch and receipt['mode'] == 'build' and
              receipt['framework_sha'] == original['framework_sha'] and receipt['conclusion'] == 'success'
              and receipt['cleanup_exit_code'] == receipt['delete_output_exit_code'] == 0,
              'historical helper producer identity/conclusion mismatch')
    C.require(receipt['cache']['WB_CACHE_COVERAGE'] == 'aggregate-helpers' and
              receipt['image']['image_id'] == receipt['selection']['architectures'][arch]['image_id'] and
              receipt['selection']['framework_sha'] == original['framework_sha'],
              'historical helper producer image/selection mismatch')
    commands = receipt['commands']
    C.require(commands and all(row['exit_code'] == 0 for row in commands), 'historical helper producer command failed')
    builds = [row['argv'] for row in commands if row['argv'][:4] == ['bash', '-euo', 'pipefail', '-c']]
    expected = ('python3 -B /inputs/release/release/build-e2e-helpers.py \\\n'
                '  --sources /src --version ' + repr(original['baseline']['version']) + ' --arch "$TARGET_ARCH" \\\n'
                '  --output /src/.ci/e2e-helpers --wheels-output /src/.ci/e2e-wheels --platform-source /src/platform\n')
    C.require(len(builds) == 1 and builds[0][4:] == [expected], 'unrecognized original helper producer recipe invocation')
    C.require(scope['repository'] == 'kuasar-sandbox/kuasar-sandbox' and scope['run_id'] == str(run_id)
              and scope['event'] == 'workflow_dispatch' and scope['namespace'] == scope['scope'] == 'trusted',
              'historical helper producer source scope mismatch')
    rows = scope['sources']
    C.require(len(rows) == len(roots) and {row['path'] for row in rows} == set(roots),
              'incomplete historical helper dependency closure')
    spec = importlib.util.spec_from_file_location('helper_original_cache_scope', ROOT / 'ci/hosted/cache-scope.py')
    cache = importlib.util.module_from_spec(spec); spec.loader.exec_module(cache)
    for row in rows:
        root, facts = roots[row['path']]
        C.require(row['kind'] == 'git' and row['clean'] is True and row['on_main'] is True and
                  row['repository'] == facts['repository'] and row['sha'] == facts['sha'],
                  'historical helper dependency source mismatch')
        content, _ = cache.snapshot(root, git=True)
        C.require(content == row['content_sha256'], 'historical helper dependency content changed')
    return {'receipt': receipts[0], 'source_scope': scopes[0]}


def inventory(archive, original, sources):
    """Derive ownership by unique original source identity, never filename."""
    expected = next(row for row in original['baseline']['assets']
                    if row['name'] == 'platform-' + original['baseline']['version'] + '.tar.gz')
    C.require(archive.stat().st_size == expected['size'] and
              'sha256:' + file_digest(archive) == expected['digest'], 'historical helper archive changed')
    C.require(set(sources) == set(C.OWNERS) - {'platform'}, 'incomplete historical helper source set')
    for owner, source in sources.items():
        unit = 'runtime' if owner == 'guest-runtime' else owner
        C.require(original.get('source_records', {}).get(unit) == source and
                  original['test_revisions'][owner]['sha'] == source['sha'] and
                  original['test_revisions'][owner]['repository'] == source['repository'],
                  'historical helper source differs from original tag/test provenance')
    identities = [row['sha'] for row in sources.values()]
    C.require(len(set(identities)) == len(identities) and original['framework_sha'] not in identities
              and original['test_revisions']['platform']['sha'] == original['framework_sha'],
              'ambiguous historical helper owner/source identity')
    records, metadata = {}, {}
    with tarfile.open(archive, 'r:gz') as tar:
        entries = {}
        for member in tar:
            name = member.name.removeprefix('./').rstrip('/')
            if name in ('', '.') and member.isdir(): continue
            C.safe_path(name)
            C.require(name not in entries and (member.isfile() or member.isdir())
                      and member.uid == member.gid == 0 and not member.mode & 0o7022,
                      'unsafe or duplicate historical archive entry')
            entries[name] = member
        for arch in C.ARCHES:
            prefix = 'test/e2e/helpers/' + arch + '/'
            raw = tar.extractfile(entries[prefix + 'helpers.json']).read().decode()
            meta = C.loads(raw)
            C.require(meta['arch'] == arch and meta['framework_sha'] == original['framework_sha'] and
                      meta['test_revisions'] == {o: s['sha'] for o, s in sources.items()} and
                      meta.get('source_records') == original['source_records'], 'historical helper metadata provenance changed')
            names = {name[len(prefix):] for name, member in entries.items() if name.startswith(prefix) and member.isfile()}
            C.require(names == set(meta['helpers']) | {'helpers.json'}, 'historical helper inventory differs from archive')
            available = {}
            for name, record in meta['helpers'].items():
                C.require(re.fullmatch('[a-z][a-z0-9_-]*', name) and set(record) == {'sha256', 'source_sha'},
                          'unsupported historical helper metadata')
                member = entries[prefix + name]; data = tar.extractfile(member).read()
                C.require(C.digest(data) == record['sha256'], 'historical helper bytes changed')
                C.require(len(data) >= 64 and data[:7] == b'\x7fELF\x02\x01\x01' and
                          struct.unpack_from('<H', data, 18)[0] == {'x86_64': 62, 'aarch64': 183}[arch] and
                          member.mode & 0o500 == 0o500, 'historical helper mode/architecture changed')
                matches = [owner for owner, source in sources.items() if source['sha'] == record['source_sha']]
                if record['source_sha'] == original['framework_sha']:
                    C.require(not matches, 'ambiguous historical platform helper'); continue
                C.require(len(matches) == 1, 'missing or ambiguous historical helper owner')
                available[name] = {'owner': matches[0], **record, 'size': len(data), 'mode': member.mode}
            records[arch], metadata[arch] = available, raw
    C.require(set(records['x86_64']) == set(records['aarch64']) and
              all(records['x86_64'][n]['owner'] == records['aarch64'][n]['owner'] for n in records['x86_64']),
              'historical helper architecture inventories differ')
    return records, metadata


def admit(selected, roots, identities, original_platform, run_id, api, gh, scratch):
    """Called only for an authenticated original schema2 exact-assets baseline."""
    import subprocess
    import zipfile
    original = selected['validation_plan']
    C.require(original.get('schema') == 2 and 'helper_contract' not in original,
              'baseline does not authorize historical helper interpretation')
    sources = {owner: identities[owner] for owner in C.OWNERS if owner != 'platform'}
    for owner, source in sources.items():
        unit = 'runtime' if owner == 'guest-runtime' else owner
        C.require(source == original.get('source_records', {}).get(unit),
                  'changed candidate dependency cannot reuse historical helpers')
        helper_inputs.inspect(roots[owner], source)
    repository, version = selected['repository'], selected['version']
    run = api(f'repos/{repository}/actions/runs/{run_id}')
    C.require(run['status'] == 'completed' and run['conclusion'] == 'success' and
              run['head_sha'] == original['framework_sha'] and run['path'] == '.github/workflows/aggregate-release.yml' and
              run['event'] == 'workflow_dispatch' and run['head_repository']['full_name'] == repository,
              'historical helper run differs from original aggregate producer')
    listing = api(f'repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100')
    C.require(listing['total_count'] <= 100, 'ambiguous historical producer artifact listing')
    archive = scratch / ('platform-' + version + '.tar.gz')
    gh('release', 'download', version, '--repo', repository, '--pattern', archive.name, '--dir', str(scratch))
    records, metadata = inventory(archive, original, sources)
    evidence, artifact_records = {}, {}
    scope_roots = {owner: (roots[owner], sources[owner]) for owner in sources}
    scope_roots['platform'] = (original_platform, {'repository': repository, 'sha': original['framework_sha']})
    scope_roots['kernel-unit/guest-runtime'] = (roots['vmlinux'], original['source_records']['vmlinux'])
    for arch in C.ARCHES:
        matches = [row for row in listing['artifacts'] if re.fullmatch(r'workbench-evidence-helper-build-' + arch + r'-[0-9a-f]{32}', row['name'])
                   and row.get('expired') is False]
        C.require(len(matches) == 1, 'missing or ambiguous original helper producer artifact')
        artifact = matches[0]
        C.require(re.fullmatch('sha256:[0-9a-f]{64}', artifact.get('digest', '')) and
                  artifact['workflow_run']['id'] == run_id and artifact['workflow_run']['head_sha'] == original['framework_sha'],
                  'historical helper artifact identity mismatch')
        zip_path = scratch / (arch + '.zip')
        with zip_path.open('xb') as output:
            subprocess.run(['gh', 'api', f"repos/{repository}/actions/artifacts/{artifact['id']}/zip"],
                           stdout=output, check=True, timeout=120)
        C.require(zip_path.stat().st_size == artifact['size_in_bytes'] and zip_path.stat().st_size <= 10 * 1024 * 1024 and
                  'sha256:' + C.digest(zip_path.read_bytes()) == artifact['digest'], 'historical producer artifact bytes changed')
        files = {}
        with zipfile.ZipFile(zip_path) as zipfile_input:
            for entry in zipfile_input.infolist():
                if entry.is_dir(): continue
                C.safe_path(entry.filename)
                C.require(entry.filename not in files and entry.file_size < 1024 * 1024,
                          'unsafe historical producer artifact')
                if entry.filename.endswith('/receipt.json') or entry.filename.startswith('workbench-cache-scope-'):
                    files[entry.filename] = zipfile_input.read(entry).decode()
        evidence[arch] = producer(files, arch, run_id, original, scope_roots)
        artifact_records[arch] = {key: artifact[key] for key in ('id', 'name', 'digest', 'workflow_run')}
    result = {'schema': 2, 'owners': {'platform': helper_inputs.owner(roots['platform'], identities['platform'])},
              'history': {'plan': original, 'plan_id': C.digest(C.canonical(original)), 'helpers': records,
                          'metadata': metadata, 'producers': evidence, 'artifacts': artifact_records}}
    result['owners'].update({owner: {'source': source, 'historical': True} for owner, source in sources.items()})
    C.validate(result)
    return result
