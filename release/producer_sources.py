"""Release-only admission of an exact commit on a trusted branch's first parent.

PR refs and aggregate branch HEADs deliberately keep source_inputs.fetch_named.
The captured branch history is evidence, never a second source selector.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ci/integration'))
import artifacts
import source_inputs

git = source_inputs.tag_sources.git
REF = re.compile(r'(main|release/v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.x)')
SHA = re.compile(r'[0-9a-f]{40}')
ADMITTED_REF = 'refs/producer/admitted'


def validate_request(unit, source_ref, sha, version, repository):
    expected = source_inputs.tag_sources.repository(unit)
    artifacts.require(repository == expected, 'producer repository does not own release unit')
    match = REF.fullmatch(source_ref)
    artifacts.require(match is not None, 'source-ref must be main or release/vMAJOR.MINOR.x')
    artifacts.require(SHA.fullmatch(sha), 'source-sha must be a full lowercase SHA')
    source_inputs.tag_sources.validate_tag(unit, version)
    if source_ref != 'main':
        core = version.removeprefix('runtime-').removeprefix('vmlinux-').removeprefix('v')
        artifacts.require(core.split('.')[:2] == [match[2], match[3]],
                          'release tag does not belong to source branch version line')


def validate_checkout(root, record):
    validate_request(record['unit'], record['source_ref'], record['sha'],
                     record['version'], record['repository'])
    artifacts.require(SHA.fullmatch(record['admitted_tip']), 'invalid admitted branch tip')
    artifacts.require(record['mode'] == 'first-parent', 'invalid producer admission mode')
    artifacts.require(git(root, 'rev-parse', '--is-shallow-repository') == 'false',
                      'producer admission requires complete branch history')
    artifacts.require(git(root, 'rev-parse', ADMITTED_REF) == record['admitted_tip'],
                      'admitted branch tip changed')
    artifacts.require(record['sha'] in git(root, 'rev-list', '--first-parent',
                                          record['admitted_tip']).splitlines(),
                      'source commit is not on admitted branch first-parent history')
    facts = source_inputs.inspect_checkout(root, record['repository'], record['sha'])
    artifacts.require(facts['tree'] == record['tree'], 'admitted producer tree changed')
    return {**facts, 'ref': 'refs/heads/' + record['source_ref']}


def fetch(root, unit, source_ref, sha, version, repository):
    validate_request(unit, source_ref, sha, version, repository)
    artifacts.require(not root.exists() and not root.is_symlink(), 'producer destination must be fresh')
    subprocess.run(['git', 'init', '--quiet', '--template=', str(root)], check=True)
    git(root, 'config', 'core.hooksPath', '/dev/null')
    git(root, 'remote', 'add', 'origin', f'https://github.com/{repository}.git')
    # Fetch only the allowed named branch, not an arbitrary SHA or pull ref.
    git(root, 'fetch', '--quiet', '--no-tags', 'origin',
        'refs/heads/' + source_ref + ':' + ADMITTED_REF)
    tip = git(root, 'rev-parse', ADMITTED_REF)
    artifacts.require(sha in git(root, 'rev-list', '--first-parent', tip).splitlines(),
                      'source commit is not on admitted branch first-parent history')
    git(root, 'checkout', '--quiet', '--detach', sha)
    facts = source_inputs.inspect_checkout(root, repository, sha)
    record = {**facts, 'unit': unit, 'version': version, 'source_ref': source_ref,
              'admitted_tip': tip, 'mode': 'first-parent'}
    validate_checkout(root, record)
    return record


def control_identity():
    framework_sha = git(Path(__file__).resolve().parents[1], 'rev-parse', 'HEAD')
    workflow_sha = os.environ.get('GITHUB_SHA')
    artifacts.require(SHA.fullmatch(framework_sha), 'invalid trusted framework identity')
    artifacts.require(workflow_sha is None or SHA.fullmatch(workflow_sha), 'invalid workflow identity')
    return {'framework_sha': framework_sha, 'workflow_sha': workflow_sha}
