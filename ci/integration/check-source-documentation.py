#!/usr/bin/env python3
"""Exercise selected documentation assembly and archive consumers without installing."""
from contextlib import contextmanager
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[2]
OWNERS = ('platform', 'accelerator', 'connector', 'guest-runtime', 'sandboxer', 'orchestrator')
GUIDE_METADATA = {'release/guide-inputs.txt', 'release/inputs/guide-inputs.yaml',
                  'releases/guide-inputs.yaml', 'release/guide-migrations.json'}
PLATFORM_DELIVERY = {
    'test/e2e/assemble_docs.py', 'test/e2e/assemble.sh', 'test/e2e/package_inputs.py',
    'ci/check_docs.py', 'ci/integration/check-published-download.py', 'release/package-platform.sh',
    'test/release/test_documentation_package.py', 'test/release/published_download_test.py',
    'test/release/test_e2e_package.py', 'test/release/test_tag_selection.py', 'test/release/test-release.sh',
    'ci/integration/check-source-documentation.py', 'ci/integration/test-source-documentation.py',
}
FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def required(plan):
    scope = plan.get('source_scope')
    if scope is None:
        return False
    require(plan.get('mode') == 'source' and isinstance(scope, dict)
            and type(scope.get('schema')) is int and scope['schema'] == 1,
            'invalid documentation source scope')
    changes = scope.get('changes')
    require(isinstance(changes, dict) and set(changes) <= set(OWNERS), 'invalid documentation change owners')
    for owner, paths in changes.items():
        require(isinstance(paths, list) and all(isinstance(path, str) for path in paths)
                and paths == sorted(set(paths)), 'invalid documentation change paths')
    return any(set(paths) & GUIDE_METADATA or owner == 'platform' and set(paths) & PLATFORM_DELIVERY
               for owner, paths in changes.items())


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE)


def selected_sources(plan, sources):
    require(sources.is_dir() and not sources.is_symlink(), 'missing private source directory')
    sources = sources.resolve()
    revisions = plan.get('test_revisions')
    require(isinstance(revisions, dict) and set(revisions) == set(OWNERS),
            'documentation validation requires all six exact test revisions')
    roots, refs, identities, trees = {}, {}, {}, {}
    for owner in OWNERS:
        root = sources / owner
        record = revisions[owner]
        repository = 'kuasar-sandbox/' + ('kuasar-sandbox' if owner == 'platform' else owner)
        require(isinstance(record, dict) and record.get('repository') == repository
                and isinstance(record.get('sha'), str) and re.fullmatch('[0-9a-f]{40}', record['sha']),
                'invalid selected documentation revision: ' + owner)
        require(root.is_dir() and not root.is_symlink() and root.resolve().parent == sources,
                'missing private documentation owner: ' + owner)
        top = git(root, 'rev-parse', '--show-toplevel').decode().strip()
        head, tree = git(root, 'rev-parse', 'HEAD', 'HEAD^{tree}').decode().splitlines()
        require(Path(top).resolve() == root.resolve() and head == record['sha'],
                'documentation source HEAD mismatch: ' + owner)
        git(root, 'diff', '--no-ext-diff', '--no-textconv', '--exit-code', 'HEAD', '--')
        entries = {}
        for row in git(root, 'ls-tree', '-rz', '--full-tree', 'HEAD').split(b'\0'):
            if not row:
                continue
            meta, name = row.split(b'\t', 1)
            mode, kind, blob = meta.decode().split()
            entries[name.decode('utf-8')] = {'mode': mode, 'kind': kind, 'blob': blob}
        roots[owner], refs[owner], trees[owner] = root, head, entries
        identities[owner] = {'repository': repository, 'sha': head, 'tree': tree}
    return roots, refs, identities, trees


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, 'cannot load documentation implementation: ' + str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def selected_implementations(platform):
    previous_path = sys.path[:]
    previous_inputs = sys.modules.get('package_inputs')
    try:
        inputs = load_module('package_inputs', platform / 'test/e2e/package_inputs.py')
        sys.modules['package_inputs'] = inputs
        sys.path.insert(0, str(platform / 'test/e2e'))
        assembler = load_module('_selected_documentation_assembler', platform / 'test/e2e/assemble_docs.py')
        consumer = load_module('_selected_documentation_consumer', platform / 'ci/integration/check-published-download.py')
        yield assembler, consumer
    finally:
        sys.path[:] = previous_path
        if previous_inputs is None:
            sys.modules.pop('package_inputs', None)
        else:
            sys.modules['package_inputs'] = previous_inputs


def fenced_code(text):
    """Keep literal fences and contents using the existing assembly contract.

    Assembly reads Markdown with universal newlines, so normalize only CR/LF
    spelling. Fence delimiters, language/info strings, body whitespace and order
    must otherwise survive exactly; prose and links outside fences may change.
    An unclosed fence extends to EOF, as it does in the assembler and checker.
    """
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    blocks, current, fence = [], None, None
    for line in text.splitlines(keepends=True):
        mark = FENCE.match(line)
        if fence is None:
            if mark:
                fence, current = mark.group(1), [line]
            continue
        current.append(line)
        if mark:
            token = mark.group(1)
            if (token[0] == fence[0] and len(token) >= len(fence)
                    and not line.strip()[len(token):].strip()):
                blocks.append(''.join(current))
                current, fence = None, None
    if current is not None:
        blocks.append(''.join(current))
    return blocks


def inventory(assembler, roots, trees):
    result, destinations, code = [], set(), {}
    for owner in OWNERS:
        for path in assembler.source_files(roots[owner], owner):
            path = Path(path)
            source = roots[owner] / path
            require(not path.is_absolute() and '..' not in path.parts and source.is_file()
                    and not any((roots[owner] / part).is_symlink() for part in (path, *path.parents)),
                    'unsafe selected documentation input: ' + owner + '/' + str(path))
            entry = trees[owner].get(path.as_posix())
            data = source.read_bytes()
            blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            require(entry is not None and entry['kind'] == 'blob' and entry['mode'] in ('100644', '100755')
                    and entry['blob'] == blob, 'documentation input differs from selected Git blob: ' + owner + '/' + str(path))
            destination = Path(assembler.destination(owner, path))
            require(not destination.is_absolute() and '..' not in destination.parts and destination.parts
                    and destination.as_posix() not in destinations, 'unsafe or duplicate assembled documentation destination')
            destinations.add(destination.as_posix())
            row = {'owner': owner, 'source': path.as_posix(), 'destination': destination.as_posix(),
                   'source_blob': blob, 'source_sha256': sha256(data), 'source_bytes': len(data)}
            if path.suffix.lower() == '.md':
                # Freeze selected input contents before calling candidate assemble().
                blocks = fenced_code(data.decode('utf-8'))
                code[destination.as_posix()] = blocks
                row.update(fenced_code_blocks=len(blocks), source_fenced_code_sha256=sha256(canonical(blocks)))
            result.append(row)
    require(result, 'empty selected documentation package')
    return sorted(result, key=lambda row: row['destination']), code


def source_commands(text):
    """Read the three literal source blocks independently of the archive consumer."""
    blocks, current = [], None
    for line in text.splitlines(keepends=True):
        if current is None and line.rstrip('\r\n') == '```bash':
            current = []
        elif current is not None and re.fullmatch(r'```[ \t]*\r?\n?', line):
            blocks.append(''.join(current))
            current = None
        elif current is not None:
            current.append(line)
    require(current is None and len(blocks) == 3 and all(block.strip() for block in blocks),
            'expected three complete source installation command blocks')
    return '\n'.join(blocks)


def check(plan, sources, output):
    result = {'schema': 1, 'kind': 'source-documentation', 'plan_id': sha256(canonical(plan)),
              'conclusion': 'failure', 'sources': {}, 'files': [], 'findings': [], 'installation': {}}
    try:
        result['required'] = required(plan)
        if not result['required']:
            result.update(conclusion='skipped', reason='no documentation delivery changes')
            return result
        roots, refs, identities, trees = selected_sources(plan, Path(sources))
        result['sources'] = identities
        # This reader belongs to the fixed executor, not the selected platform
        # source under test. Candidate implementations run only inside Workbench.
        checker = load_module('_trusted_documentation_checker', ROOT / 'ci/check_docs.py')
        with selected_implementations(roots['platform']) as (assembler, consumer), \
                tempfile.TemporaryDirectory(prefix='source-documentation-') as directory:
            temporary = Path(directory)
            package = temporary / 'package'
            package.mkdir()
            result['files'], source_code = inventory(assembler, roots, trees)
            result['fenced_code_blocks'] = sum(row.get('fenced_code_blocks', 0) for row in result['files'])
            assembler.assemble(package, roots, refs)
            actual = []
            for path in sorted(package.rglob('*')):
                require(not path.is_symlink() and (path.is_file() or path.is_dir()),
                        'unsafe assembled documentation entry: ' + str(path))
                if path.is_file():
                    actual.append(path.relative_to(package).as_posix())
            actual.sort()
            require(actual == [row['destination'] for row in result['files']],
                    'assembled documentation inventory differs from selected inputs')
            for row in result['files']:
                target = package / row['destination']
                data = target.read_bytes()
                row.update(package_sha256=sha256(data), package_bytes=len(data))
                if target.suffix.lower() == '.md':
                    blocks = fenced_code(data.decode('utf-8'))
                    row.update(package_fenced_code_sha256=sha256(canonical(blocks)),
                               fenced_code_preserved=blocks == source_code.get(row['destination']))
                    require(row['fenced_code_preserved'],
                            'fenced code changed during documentation assembly: ' + row['destination'])
                    result['findings'].extend(checker.check_file(package, target))
                else:
                    require(row['package_sha256'] == row['source_sha256'],
                            'legal input bytes changed during documentation assembly: ' + row['destination'])
            require(not result['findings'], 'assembled documentation navigation checks failed')
            archive = temporary / 'documentation.tar.gz'
            with tarfile.open(archive, 'w:gz') as stream:
                for row in result['files']:
                    stream.add(package / row['destination'], arcname=row['destination'], recursive=False)
            result['archive'] = {'sha256': sha256(archive.read_bytes()), 'members': len(actual)}
            for language, suffix in (('en', ''), ('zh', '_zh')):
                paths = {f'guide/{name}{suffix}.md' for name in ('installation', 'download')}
                choices = [row for row in result['files'] if row['owner'] == 'platform' and row['destination'] in paths]
                require(len(choices) == 1, 'expected one selected source installation guide: ' + language)
                source = roots['platform'] / choices[0]['source']
                expected = source_commands(source.read_text(encoding='utf-8'))
                observed = consumer.guide_script(archive, language)
                require(observed == expected, 'bundled installation commands differ from selected source: ' + language)
                result['installation'][language] = {'source': choices[0]['source'],
                    'destination': choices[0]['destination'], 'blocks': 3,
                    'script_sha256': sha256(observed.encode()), 'script_bytes': len(observed.encode()),
                    'matches_selected_source': True, 'commands_executed': False}
            result['installation_languages_match'] = (result['installation']['en']['script_sha256'] ==
                                                        result['installation']['zh']['script_sha256'])
            result.update(conclusion='success', markdown_files=sum(path.lower().endswith('.md') for path in actual))
        return result
    except (Exception, SystemExit) as error:
        result['error'] = str(error)
        raise
    finally:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(canonical(result) + b'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        plan = json.loads(args.plan.read_text())
    except (OSError, ValueError) as error:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical({'schema': 1, 'kind': 'source-documentation',
                                          'conclusion': 'failure', 'error': str(error)}) + b'\n')
        print('source documentation: ' + str(error), file=sys.stderr)
        return 1
    try:
        result = check(plan, args.sources, args.output)
    except (Exception, SystemExit) as error:
        print('source documentation: ' + str(error), file=sys.stderr)
        return 1
    print('Source documentation:', result['conclusion'], '-', len(result['files']), 'selected files')
    return 0


if __name__ == '__main__':
    sys.exit(main())
