"""Checked run inputs and package inventories; no revision-selection overrides."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

import artifacts
import transport

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'release'))
import tag_sources
sys.path.insert(0, str(ROOT / 'test/e2e'))
import package_inputs


def inspect_platform(root, expected_sha):
    git = tag_sources.git
    artifacts.require(root.is_dir() and not root.is_symlink() and (root / '.git').is_dir(),
                      'missing admitted platform source')
    artifacts.require(Path(git(root, 'rev-parse', '--show-toplevel')).resolve() == root.resolve(),
                      'platform source is not a repository root')
    artifacts.require(git(root, 'config', '--get', 'remote.origin.url') ==
                      'https://github.com/kuasar-sandbox/kuasar-sandbox.git', 'wrong platform source repository')
    artifacts.require(git(root, 'rev-parse', 'HEAD') == expected_sha, 'admitted platform source identity changed')
    modified = git(root, 'diff', '--no-ext-diff', '--no-textconv', '--name-only', 'HEAD', '--')
    untracked = git(root, 'ls-files', '--others')
    artifacts.require(not modified and not untracked, 'admitted platform source is dirty: ' + modified + untracked)
    for row in git(root, 'ls-tree', '-r', 'HEAD').splitlines():
        artifacts.require(row.split()[0] in ('100644', '100755'), 'platform source contains links or submodules')
    return {'repository': 'kuasar-sandbox/kuasar-sandbox', 'sha': expected_sha,
            'tree': git(root, 'rev-parse', 'HEAD^{tree}')}


def owner_cases(root, owner):
    e2e = root / ('test/e2e/platform' if owner == 'platform' else 'test/e2e')
    artifacts.require(not (e2e / 'run_all.sh').exists(), 'superseded owner runner: ' + owner)
    directory = e2e / 'cases'
    artifacts.require(directory.is_dir() and not directory.is_symlink(), 'case source is not a directory: ' + owner)
    entries = list(directory.iterdir())
    artifacts.require(entries, 'selected source has no case files: ' + owner)
    artifacts.require(all(entry.is_file() and not entry.is_symlink() for entry in entries),
                      'selected E2E cases must be flat files: ' + owner)
    return sorted(artifacts.case_name(entry.name) for entry in entries)


def cases(roots):
    result, seen = {}, set()
    for owner in artifacts.OWNERS:
        names = owner_cases(roots[owner], owner)
        for name in names:
            artifacts.require(name not in seen, 'duplicate E2E case ID: ' + name)
            seen.add(name)
        result[owner] = names
    return result


def verify_package(archive, roots, case_files, records):
    """Match delivered case/library bytes against repository-local inputs."""
    spec = importlib.util.spec_from_file_location('input_package_validator', ROOT / 'release/validate-e2e-package.py')
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    validator.validate(archive, expected_sources=records)
    with tempfile.TemporaryDirectory(prefix='source-package-') as directory:
        output = Path(directory)
        artifacts.unpack(archive, output, 'platform', {})
        artifacts.normalize_e2e_cases(output, case_files, from_owners=False)
        for owner, names in case_files.items():
            e2e = roots[owner] / ('test/e2e/platform' if owner == 'platform' else 'test/e2e')
            for name in names:
                artifacts.require(artifacts.digest(e2e / 'cases' / name) ==
                                  artifacts.digest(output / 'test/e2e/cases' / name),
                                  'packaged case differs from planned owner input: ' + owner + '/' + name)
            paths = package_inputs.library_inputs(e2e)
            expected = {str(path.relative_to('lib')): artifacts.digest(e2e / path) for path in paths}
            actual = artifacts.tree_files(output / 'test/e2e/lib' / owner)
            artifacts.require(actual == expected, 'packaged library differs from planned owner input: ' + owner)


def staged_inputs(stage, version, sha, output):
    transport.extract(stage / 'source-inputs.tar', output)
    platform = output / 'platform'
    platform_record = inspect_platform(platform, sha)
    units = tag_sources.selection.resolve_new_selection(platform, version)
    records = tag_sources.inspect_sources(output / 'sources', units)
    artifacts.require(records == json.loads((stage / 'source-records.json').read_text()),
                      'staged source facts differ from selected sources')
    tag_sources.require_owner_guides(output / 'sources')
    roots = {owner: output / 'sources' / unit for owner, unit in tag_sources.OWNER_UNITS.items()}
    roots['platform'] = platform
    selected_cases = cases(roots)
    verify_package(stage / 'assets' / f'platform-{version}.tar.gz', roots, selected_cases, records)
    return platform, units, records, platform_record, selected_cases


def inspect_checkout(root, repository, sha):
    git = tag_sources.git
    artifacts.require(root.is_dir() and not root.is_symlink() and (root / '.git').is_dir()
                      and not (root / '.git').is_symlink(), 'missing fixed run source')
    artifacts.require(Path(git(root, 'rev-parse', '--show-toplevel')).resolve() == root.resolve(), 'invalid run source root')
    artifacts.require(git(root, 'config', '--get', 'remote.origin.url') == f'https://github.com/{repository}.git',
                      'run source repository changed')
    artifacts.require(git(root, 'rev-parse', 'HEAD') == sha, 'run source identity changed')
    artifacts.require(not git(root, 'diff', '--no-ext-diff', '--no-textconv', '--name-only', 'HEAD', '--')
                      and not git(root, 'ls-files', '--others'), 'run source has modified inputs')
    for row in git(root, 'ls-tree', '-r', 'HEAD').splitlines():
        artifacts.require(row.split()[0] in ('100644', '100755'), 'run source contains links or submodules')
    return {'repository': repository, 'sha': sha, 'tree': git(root, 'rev-parse', 'HEAD^{tree}')}


def fetch_named(root, repository, ref, expected_sha):
    import re
    import subprocess
    artifacts.require(repository in {'kuasar-sandbox/' + ('kuasar-sandbox' if owner == 'platform' else owner)
                                      for owner in artifacts.OWNERS}, 'unadmitted repository')
    artifacts.require(re.fullmatch(r'refs/(heads/(main|release/v[0-9]+\.[0-9]+\.x)|pull/[1-9][0-9]*/merge|tags/release-v[0-9A-Za-z.+-]+)', ref),
                      'source retrieval requires an admitted branch, PR ref or aggregate tag')
    artifacts.require(not root.exists(), 'run source destination must be fresh')
    subprocess.run(['git', 'init', '--quiet', '--template=', str(root)], check=True)
    git = tag_sources.git
    git(root, 'config', 'core.hooksPath', '/dev/null')
    git(root, 'remote', 'add', 'origin', f'https://github.com/{repository}.git')
    git(root, 'fetch', '--quiet', '--depth=1', '--no-tags', 'origin', ref)
    git(root, 'checkout', '--quiet', '--detach', 'FETCH_HEAD')
    facts = inspect_checkout(root, repository, expected_sha)
    facts['ref'] = ref
    return facts


INPUT_ARCHIVE = Path('plan/source-inputs.tar')


def copy_run_inputs(plan, layout, output):
    """Consumers use only fixed transport; never retrieve by factual SHA."""
    import shutil
    with tempfile.TemporaryDirectory(prefix='fixed-inputs-') as directory:
        root = Path(directory) / 'inputs'
        transport.extract(INPUT_ARCHIVE, root)
        records = plan.get('run_inputs')
        artifacts.require(isinstance(records, dict) and records, 'plan lacks admitted run inputs')
        for relative, record in records.items():
            artifacts.relative(relative)
            facts = inspect_checkout(root / relative, record['repository'], record['sha'])
            artifacts.require(facts['tree'] == record['tree'], 'fixed run input tree changed')
            if 'tag' in record:
                unit = ('runtime' if record['tag'].startswith('runtime-') else
                        'vmlinux' if record['tag'].startswith('vmlinux-') else record['repository'].split('/')[1])
                observed = tag_sources.inspect_unit(root / relative, unit, record['tag'])
                artifacts.require(observed == {key: record[key] for key in observed}, 'fixed tag source facts changed')
        for relative, record in layout.items():
            artifacts.relative(relative)
            matches = [name for name, row in records.items()
                       if (row['repository'], row['sha']) == (record['repository'], record['sha'])
                       and ('tag' not in record or row.get('tag') == record['tag'])]
            artifacts.require(matches, 'planned source absent from admitted run inputs')
            # Same repository/commit has one tree even when two units select it.
            artifacts.require(len({records[name]['tree'] for name in matches}) == 1, 'ambiguous run source identity')
            shutil.copytree(root / sorted(matches)[0], output / relative)
