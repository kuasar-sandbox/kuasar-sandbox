#!/usr/bin/env python3
"""Real named-ref admission, artifact restoration and historical evidence tests."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import artifacts
import source_inputs as subject
from test_fixtures import CASES, architecture_result
sys.path.insert(0, str(subject.ROOT / 'release'))
from test_tag_sources import fixture

spec = importlib.util.spec_from_file_location('evidence_resolver', Path(__file__).with_name('resolve-artifacts.py'))
resolver = importlib.util.module_from_spec(spec); spec.loader.exec_module(resolver)


class FixedInputs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.remote = self.root / 'remote'
        self.sha = fixture(self.remote, 'connector', 'v1.2.3')
        subject.tag_sources.git(self.remote, 'update-ref', 'refs/pull/23/merge', self.sha)
        self.env = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': f'url.{self.remote}.insteadOf',
                    'GIT_CONFIG_VALUE_0': 'https://github.com/kuasar-sandbox/connector.git'}

    def test_platform_checkout_origin_suffix_and_wrong_repository(self):
        root = self.root / 'platform'
        root.mkdir()
        git = subject.tag_sources.git
        git(root, 'init', '-q')
        git(root, 'config', 'user.email', 'test@example.invalid')
        git(root, 'config', 'user.name', 'Fixture')
        (root / 'README.md').write_text('platform fixture\n')
        git(root, 'add', 'README.md')
        git(root, 'commit', '-qm', 'fixture')
        sha = git(root, 'rev-parse', 'HEAD')
        for origin in ('https://github.com/kuasar-sandbox/kuasar-sandbox',
                       'https://github.com/kuasar-sandbox/kuasar-sandbox.git'):
            with self.subTest(origin=origin):
                git(root, 'remote', 'remove', 'origin') if git(root, 'remote') else None
                git(root, 'remote', 'add', 'origin', origin)
                self.assertEqual(subject.inspect_platform(root, sha)['sha'], sha)
        git(root, 'remote', 'set-url', 'origin', 'https://github.com/other/kuasar-sandbox')
        with self.assertRaisesRegex(ValueError, 'wrong platform source repository'):
            subject.inspect_platform(root, sha)
        git(root, 'remote', 'set-url', 'origin', 'https://github.com/kuasar-sandbox/kuasar-sandbox')
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            subject.inspect_platform(root, 'f' * 40)

    def test_staged_platform_origin_survives_both_fixed_input_consumers(self):
        spec = importlib.util.spec_from_file_location('fixed_source_checks',
                                                     Path(__file__).with_name('run-source-checks.py'))
        checks = importlib.util.module_from_spec(spec); spec.loader.exec_module(checks)
        git = subject.tag_sources.git
        for suffix in ('', '.git'):
            with self.subTest(suffix=suffix):
                root = self.root / ('canonical' if not suffix else 'suffixed')
                inputs = root / 'inputs'
                platform = inputs / 'platform'
                fixture(platform, 'connector', 'v0.0.1')
                origin = 'https://github.com/' + resolver.PLATFORM + suffix
                git(platform, 'remote', 'set-url', 'origin', origin)
                shutil.rmtree(platform / 'test/e2e/cases')
                cases = platform / 'test/e2e/platform/cases'; cases.mkdir(parents=True)
                for name in CASES['platform']: (cases / name).write_text('echo platform\n')
                records, units = {}, {}
                for unit in resolver.release.selection.UNITS:
                    owner = 'guest-runtime' if unit in ('runtime', 'vmlinux') else unit
                    tag = (unit + '-' if unit in ('runtime', 'vmlinux') else '') + 'v1.2.3'
                    source = inputs / 'sources' / unit
                    fixture(source, unit, 'v0.0.1')
                    shutil.rmtree(source / 'test/e2e/cases')
                    (source / 'test/e2e/cases').mkdir()
                    for name in CASES[owner]: (source / 'test/e2e/cases' / name).write_text('echo owner\n')
                    git(source, 'add', '.'); git(source, 'commit', '-qm', 'owner inputs')
                    git(source, 'tag', tag)
                    records[unit] = subject.tag_sources.inspect_unit(source, unit, tag)
                    units[unit] = tag
                manifest = 'version: release-v1.2.3\ndelivery: workbench-v1\ncomponents:\n' + ''.join(
                    f'  {unit}: {tag}\n' for unit, tag in units.items())
                (platform / 'releases').mkdir()
                (platform / 'releases/release.yaml').write_text(manifest)
                (platform / 'releases/daily-preview.yaml').write_text(manifest + 'preview_version: preview.20260101\n')
                git(platform, 'add', '.'); git(platform, 'commit', '-qm', 'admitted selection')
                sha = git(platform, 'rev-parse', 'HEAD')
                stage = root / 'stage'; (stage / 'assets').mkdir(parents=True)
                capsule = stage / 'source-inputs.tar'
                with tarfile.open(capsule, 'w') as archive:
                    for entry in inputs.iterdir(): archive.add(entry, arcname=entry.name)
                (stage / 'source-records.json').write_text(json.dumps(records))
                (stage / 'selection.tsv').write_text(''.join(f'{unit}\t{tag}\n' for unit, tag in units.items()))
                names = {'SHA256SUMS', 'platform-release-v1.2.3.tar.gz'}
                for arch in artifacts.ARCHES:
                    names.add(resolver.release.selection.workbench_archive('release-v1.2.3', arch))
                    names.update(artifacts.archive_name(unit, tag, arch) for unit, tag in units.items())
                for name in names: (stage / 'assets' / name).write_bytes(b'package boundary fixture')
                # Package payload validation has its own complete suite. Keep real staged
                # source extraction/admission, plan validation and both consumers here.
                with patch.object(subject, 'verify_package') as package, patch.object(resolver, 'public'), \
                     patch.object(resolver.release, 'tag_sha', side_effect=lambda repo, tag:
                                  next(row['sha'] for row in records.values() if row['tag'] == tag and row['repository'] == repo)), \
                     patch.dict(os.environ, RELEASE_VERSION='release-v1.2.3', PLATFORM_SOURCE_SHA=sha):
                    plan = resolver.exact_assets_plan('a' * 40, stage)
                package.assert_called_once()
                self.assertEqual(plan['run_inputs']['platform'], subject.inspect_platform(platform, sha))
                shutil.rmtree(inputs)  # Consumers have only the frozen archive.
                with patch.object(subject, 'INPUT_ARCHIVE', capsule):
                    checks.materialize(plan, root / 'checked', root / 'result.json')
                    for arch in artifacts.ARCHES:
                        checks.build.materialize(plan, arch, root / arch)
                self.assertEqual(json.loads((root / 'result.json').read_text())['conclusion'], 'success')
                restored = root / 'checked/platform'
                self.assertEqual(git(restored, 'config', '--get', 'remote.origin.url'), origin)
                self.assertEqual(subject.inspect_checkout(restored, resolver.PLATFORM, sha), plan['platform_source'])
                self.assertEqual((restored / 'releases/release.yaml').read_text(), manifest)

    def test_fixed_tagged_unit_transport_preserves_both_origin_forms(self):
        git = subject.tag_sources.git
        for suffix in ('', '.git'):
            with self.subTest(suffix=suffix):
                origin = 'https://github.com/kuasar-sandbox/connector' + suffix
                git(self.remote, 'remote', 'set-url', 'origin', origin)
                row = subject.tag_sources.inspect_unit(self.remote, 'connector', 'v1.2.3')
                capsule = self.root / 'unit.tar'
                with tarfile.open(capsule, 'w') as archive: archive.add(self.remote, arcname='sources/connector')
                output = self.root / ('unit-canonical' if not suffix else 'unit-suffixed')
                with patch.object(subject, 'INPUT_ARCHIVE', capsule):
                    subject.copy_run_inputs({'run_inputs': {'sources/connector': row}}, {'connector': row}, output)
                self.assertEqual(git(output / 'connector', 'config', '--get', 'remote.origin.url'), origin)
                self.assertEqual(subject.tag_sources.inspect_unit(output / 'connector', 'connector', 'v1.2.3'), row)

    def test_fixed_platform_transport_rejects_origin_and_input_tampering(self):
        git = subject.tag_sources.git
        source = self.root / 'platform'
        fixture(source, 'connector', 'v1.2.3')
        origin = 'https://github.com/' + resolver.PLATFORM
        git(source, 'remote', 'set-url', 'origin', origin)
        sha = git(source, 'rev-parse', 'HEAD')
        row = subject.inspect_platform(source, sha)
        def consume(record=row):
            capsule = self.root / 'platform.tar'
            with tarfile.open(capsule, 'w') as archive: archive.add(source, arcname='platform')
            with patch.object(subject, 'INPUT_ARCHIVE', capsule):
                subject.copy_run_inputs({'run_inputs': {'platform': record}}, {'platform': row}, self.root / 'rejected')
        for wrong in (origin + '.git.git', origin + '-other', origin + '/extra',
                      origin.replace('github.com', 'github.com.evil.invalid'),
                      origin.replace('https:', 'http:'), origin.replace('kuasar-sandbox/kuasar-sandbox', 'other/kuasar-sandbox')):
            with self.subTest(origin=wrong):
                git(source, 'remote', 'set-url', 'origin', wrong)
                with self.assertRaisesRegex(ValueError, 'repository changed'): consume()
        git(source, 'remote', 'set-url', 'origin', origin)
        with self.assertRaisesRegex(ValueError, 'identity changed'): consume(dict(row, sha='f' * 40))
        with self.assertRaisesRegex(ValueError, 'tree changed'): consume(dict(row, tree='f' * 40))
        path = source / 'README.md'; original = path.read_bytes()
        path.write_text('modified\n')
        with self.assertRaisesRegex(ValueError, 'modified inputs'): consume()
        git(source, 'add', 'README.md')
        with self.assertRaisesRegex(ValueError, 'modified inputs'): consume()
        path.write_bytes(original); git(source, 'add', 'README.md')
        untracked = source / 'untracked'; untracked.write_text('extra')
        with self.assertRaisesRegex(ValueError, 'modified inputs'): consume()
        untracked.unlink()
        (source / '.git/info').mkdir(exist_ok=True)
        (source / '.git/info/exclude').write_text('ignored\n')
        ignored = source / 'ignored'; ignored.write_text('extra')
        with self.assertRaisesRegex(ValueError, 'modified inputs'): consume()
        ignored.unlink()
        path.unlink(); path.symlink_to('product.go')
        with self.assertRaisesRegex(ValueError, 'transport cannot contain links'): consume()
        git(source, 'add', 'README.md')
        git(source, 'commit', '-qm', 'tracked link')
        # Exercise the checkout link guard itself, without transport rejecting first.
        with self.assertRaisesRegex(ValueError, 'links or submodules'):
            subject.inspect_checkout(source, resolver.PLATFORM, git(source, 'rev-parse', 'HEAD'))
        path.unlink(); path.write_text('new committed inputs\n')
        git(source, 'add', 'README.md'); git(source, 'commit', '-qm', 'changed admitted commit')
        with self.assertRaisesRegex(ValueError, 'identity changed'): consume()

    def test_candidate_tests_survive_reuse_and_do_not_refetch_after_admission(self):
        (self.remote / 'test/e2e/cases/basic.fixture.sh').write_text('echo candidate tests\n')
        subject.tag_sources.git(self.remote, 'commit', '-qam', 'tests only')
        candidate = subject.tag_sources.git(self.remote, 'rev-parse', 'HEAD')
        subject.tag_sources.git(self.remote, 'update-ref', 'refs/pull/23/merge', candidate)
        inputs = self.root / 'inputs'; inputs.mkdir()
        with patch.dict(os.environ, self.env), patch.object(subject.tag_sources, 'git', wraps=subject.tag_sources.git) as git:
            row = subject.fetch_named(inputs / 'connector', 'kuasar-sandbox/connector', 'refs/pull/23/merge', candidate)
        self.assertEqual(row['sha'], candidate)
        fetch = [call.args for call in git.call_args_list if call.args[1] == 'fetch']
        self.assertEqual(fetch[0][-1], 'refs/pull/23/merge')
        baseline = self.root / 'baseline'
        with patch.dict(os.environ, self.env): subject.tag_sources.fetch_unit(baseline, 'connector', 'v1.2.3', self.sha)
        changes = resolver.tree_changes(baseline, inputs / 'connector')
        self.assertEqual(changes, ['test/e2e/cases/basic.fixture.sh'])
        self.assertEqual(artifacts.changed_products({'connector': changes}), [])
        capsule = self.root / 'source-inputs.tar'
        with tarfile.open(capsule, 'w') as archive: archive.add(inputs / 'connector', arcname='connector')
        plan = {'run_inputs': {'connector': row}}
        layout = {'connector': {'repository': row['repository'], 'sha': candidate}}
        output = self.root / 'output'; output.mkdir()
        shutil.rmtree(self.remote)
        with patch.object(subject, 'INPUT_ARCHIVE', capsule): subject.copy_run_inputs(plan, layout, output)
        self.assertEqual((output / 'connector/test/e2e/cases/basic.fixture.sh').read_text(), 'echo candidate tests\n')
        broken = copy.deepcopy(plan); broken['run_inputs']['connector']['tree'] = '0' * 40
        with patch.object(subject, 'INPUT_ARCHIVE', capsule), self.assertRaisesRegex(ValueError, 'tree changed'):
            subject.copy_run_inputs(broken, layout, self.root / 'rejected')
        absent = {'connector': dict(layout['connector'], sha=self.sha)}
        with patch.object(subject, 'INPUT_ARCHIVE', capsule), self.assertRaisesRegex(ValueError, 'absent from admitted'):
            subject.copy_run_inputs(plan, absent, self.root / 'absent')

    def test_two_tags_at_one_commit_restore_the_selected_tag_checkout(self):
        subject.tag_sources.git(self.remote, 'tag', 'v1.2.4')
        inputs = self.root / 'inputs'
        inputs.mkdir()
        records = {}
        with patch.dict(os.environ, self.env):
            for name, tag in (('a', 'v1.2.3'), ('b', 'v1.2.4')):
                records[name] = subject.tag_sources.fetch_unit(inputs / name, 'connector', tag, self.sha)
        capsule = self.root / 'source-inputs.tar'
        with tarfile.open(capsule, 'w') as archive:
            for path in inputs.iterdir(): archive.add(path, arcname=path.name)
        shutil.rmtree(self.remote)
        with patch.object(subject, 'INPUT_ARCHIVE', capsule):
            subject.copy_run_inputs({'run_inputs': records}, {'connector': records['b']}, self.root / 'output')
        observed = subject.tag_sources.inspect_unit(self.root / 'output/connector', 'connector', 'v1.2.4')
        self.assertEqual(observed, records['b'])

    def test_producer_branch_and_dependency_tag_are_frozen_for_both_architectures(self):
        spec = importlib.util.spec_from_file_location('producer_inputs', subject.ROOT / 'release/producer-inputs.py')
        producer = importlib.util.module_from_spec(spec); spec.loader.exec_module(producer)
        subject.tag_sources.git(self.remote, 'branch', '-M', 'main')
        dependency = self.root / 'dependency'
        dependency_sha = fixture(dependency, 'accelerator', 'v1.2.3')
        environment = dict(self.env, GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_1=f'url.{dependency}.insteadOf',
                           GIT_CONFIG_VALUE_1='https://github.com/kuasar-sandbox/accelerator.git')
        capsule = self.root / 'producer.tar'
        with patch.dict(os.environ, environment):
            producer.freeze('connector', 'main', self.sha, [('accelerator', 'v1.2.3', dependency_sha)], capsule)
        shutil.rmtree(self.remote); shutil.rmtree(dependency)
        for arch in artifacts.ARCHES:
            output = self.root / arch
            producer.restore(capsule, 'connector', self.sha, output, [('accelerator', 'v1.2.3', dependency_sha)])
            self.assertEqual(subject.tag_sources.git(output / 'connector', 'rev-parse', 'HEAD'), self.sha)
            self.assertEqual(subject.tag_sources.inspect_unit(output / 'accelerator', 'accelerator', 'v1.2.3')['sha'], dependency_sha)
        with self.assertRaisesRegex(ValueError, 'differs from admitted request'):
            producer.restore(capsule, 'connector', 'f' * 40, self.root / 'wrong')
        expected = [('accelerator', 'v1.2.3', dependency_sha)]
        for dependencies in ([], expected + [('sandboxer', 'v1.2.3', dependency_sha)],
                             [('sandboxer', 'v1.2.3', dependency_sha)],
                             [('accelerator', 'v1.2.4', dependency_sha)],
                             [('accelerator', 'v1.2.3', 'f' * 40)]):
            with self.subTest(dependencies=dependencies), self.assertRaisesRegex(ValueError, 'differs from preflight'):
                producer.restore(capsule, 'connector', self.sha, self.root / 'unexpected', dependencies)
        # The artifact cannot substitute its own expected tag, even at the same commit.
        extracted = self.root / 'tampered'
        subject.transport.extract(capsule, extracted)
        receipt = json.loads((extracted / 'producer-records.json').read_text())
        receipt['sources']['src/accelerator']['tag'] = 'v1.2.4'
        subject.tag_sources.git(extracted / 'src/accelerator', 'tag', 'v1.2.4')
        (extracted / 'producer-records.json').write_text(json.dumps(receipt))
        altered = self.root / 'altered.tar'
        with tarfile.open(altered, 'w') as archive:
            for entry in extracted.iterdir(): archive.add(entry, arcname=entry.name)
        with self.assertRaisesRegex(ValueError, 'differs from preflight'):
            producer.restore(altered, 'connector', self.sha, self.root / 'substituted', expected)

    def test_moving_candidate_ref_fails_automatic_identity_check(self):
        with patch.dict(os.environ, self.env), self.assertRaisesRegex(ValueError, 'identity changed'):
            subject.fetch_named(self.root / 'candidate', 'kuasar-sandbox/connector', 'refs/pull/23/merge', 'f' * 40)
        with self.assertRaisesRegex(ValueError, 'admitted branch'):
            subject.fetch_named(self.root / 'raw', 'kuasar-sandbox/connector', self.sha, self.sha)

    def test_local_case_discovery_rejects_duplicates_links_and_empty_owners(self):
        roots = {}
        for owner, names in CASES.items():
            roots[owner] = self.root / owner
            cases = roots[owner] / ('test/e2e/platform/cases' if owner == 'platform' else 'test/e2e/cases')
            cases.mkdir(parents=True)
            for name in names: (cases / name).write_text('exit 0\n')
        self.assertEqual(subject.cases(roots), CASES)
        duplicate = roots['connector'] / 'test/e2e/cases' / CASES['accelerator'][0]
        duplicate.write_text('exit 0\n')
        with self.assertRaisesRegex(ValueError, 'duplicate E2E'): subject.cases(roots)
        duplicate.unlink(); duplicate.symlink_to(CASES['connector'][0])
        with self.assertRaises(ValueError): subject.cases(roots)


class SourcePlan(unittest.TestCase):
    def test_aggregate_publish_uses_fixed_manifest_and_live_stable_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            fixture(source, 'connector', 'v1.2.3')
            subject.tag_sources.git(source, 'remote', 'set-url', 'origin', 'https://github.com/' + resolver.PLATFORM + '.git')
            (source / 'releases').mkdir()
            manifest = source / 'releases/daily-preview.yaml'
            manifest.write_text((Path(__file__).parent / 'fixtures/pre-cutover-preview.yaml').read_text().split('test_revisions:')[0])
            subject.tag_sources.git(source, 'add', '.')
            subject.tag_sources.git(source, 'commit', '-qm', 'admitted selection')
            sha = subject.tag_sources.git(source, 'rev-parse', 'HEAD')
            (root / 'bin').mkdir()
            gh = root / 'bin/gh'
            gh.write_text('#!/bin/sh\ncase "$2" in */releases/tags/release-v0.1.6)\n'
                          'if [ "${CLOSED:-0}" = 1 ]; then echo "{}"; exit 0; fi\n'
                          'echo "gh: Not Found (HTTP 404)" >&2; exit 1;;\n'
                          '*) echo "late branch or SHA retrieval forbidden" >&2; exit 97;; esac\n')
            gh.chmod(0o755)
            env = dict(os.environ, PATH=str(root / 'bin') + ':' + os.environ['PATH'], PLATFORM_SOURCE_ROOT=str(source))
            command = ['bash', str(subject.ROOT / 'release/validate-preview-line.sh'),
                       'release-v0.1.6-preview.20261010.1', sha]
            self.assertEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)
            closed = subprocess.run(command, env=dict(env, CLOSED='1'), capture_output=True, text=True)
            self.assertNotEqual(closed.returncode, 0); self.assertIn('is closed', closed.stderr)
            manifest.write_text(manifest.read_text() + '# tampered\n')
            self.assertNotEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)

    def test_real_pre_cutover_base_and_tag_only_candidate(self):
        legacy = (Path(__file__).parent / 'fixtures/pre-cutover-preview.yaml').read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'releases').mkdir()
            path = root / 'releases/daily-preview.yaml'
            path.write_text(legacy)
            with patch.object(resolver, 'aggregate', return_value={'original': True}) as read:
                self.assertEqual(resolver.baseline(root, 'main'), {'original': True})
                read.assert_called_once_with('release-v0.1.6-preview.20261010.1')
            with self.assertRaisesRegex(resolver.release.selection.ManifestError, 'test_revisions'):
                resolver.release.selection.parse_manifest(legacy, str(path), True)
            candidate = legacy.split('test_revisions:')[0]
            version, _, _ = resolver.release.selection.parse_manifest(candidate, str(path), True)
            self.assertEqual(version, 'release-v0.1.6-preview.20261010.1')

    def test_tag_migration_cannot_drop_unchanged_owner_cases(self):
        selected = copy.deepcopy(CASES)
        selected['connector'] = selected['connector'][1:]
        with self.assertRaisesRegex(ValueError, 'explicit new-tag cutover required: connector'):
            resolver.preserve_baseline_cases(CASES, selected, ['accelerator'])
        # A deliberate change in the admitted owner's PR remains testable.
        resolver.preserve_baseline_cases(CASES, selected, ['connector'])
        with self.assertRaisesRegex(ValueError, 'validated case ownership evidence'):
            resolver.preserve_baseline_cases(None, selected, [])

    def test_real_owner_tags_and_candidate_tests_reuse_every_product(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {'GIT_CONFIG_COUNT': '6'}
            selected = {'repository': resolver.PLATFORM, 'version': 'release-v1.2.3', 'sha': 'b' * 40,
                        'units': {}, 'assets': [], 'case_files': copy.deepcopy(CASES), 'test_revisions': artifacts.release_test_revisions(
                            {owner: 'd' * 40 for owner in artifacts.OWNERS if owner != 'platform'}, 'b' * 40)}
            roots = {}
            for index, owner in enumerate(artifacts.OWNERS):
                repo = root / owner; roots[owner] = repo
                unit = 'runtime' if owner == 'guest-runtime' else 'connector' if owner == 'platform' else owner
                fixture(repo, unit, ('runtime-' if unit == 'runtime' else '') + 'v0.0.1')
                repository = resolver.REPOSITORIES[owner]
                subject.tag_sources.git(repo, 'remote', 'set-url', 'origin', 'https://github.com/' + repository + '.git')
                subject.tag_sources.git(repo, 'branch', '-M', 'main')
                shutil.rmtree(repo / 'test/e2e/cases')
                cases = repo / ('test/e2e/platform/cases' if owner == 'platform' else 'test/e2e/cases')
                cases.mkdir(parents=True)
                for name in CASES[owner]: (cases / name).write_text('echo original\n')
                subject.tag_sources.git(repo, 'add', '.')
                subject.tag_sources.git(repo, 'commit', '-qm', 'owner cases')
                sha = subject.tag_sources.git(repo, 'rev-parse', 'HEAD')
                for release_unit in (() if owner == 'platform' else ('runtime', 'vmlinux') if owner == 'guest-runtime' else (owner,)):
                    tag = (release_unit + '-' if release_unit in ('runtime', 'vmlinux') else '') + 'v1.2.3'
                    subject.tag_sources.git(repo, 'tag', tag)
                    selected['units'][release_unit] = {'repository': repository, 'version': tag, 'sha': sha}
                environment['GIT_CONFIG_KEY_' + str(index)] = f'url.{repo}.insteadOf'
                environment['GIT_CONFIG_VALUE_' + str(index)] = 'https://github.com/' + repository + '.git'
            connector = roots['accelerator']
            base_sha = subject.tag_sources.git(connector, 'rev-parse', 'HEAD')
            (connector / 'test/e2e/cases/image.manifest.sh').write_text('echo candidate assertion\n')
            subject.tag_sources.git(connector, 'commit', '-qam', 'test-only candidate')
            candidate = subject.tag_sources.git(connector, 'rev-parse', 'HEAD')
            subject.tag_sources.git(connector, 'update-ref', 'refs/pull/23/merge', candidate)
            environment.update(CANDIDATE_REPOSITORY=resolver.REPOSITORIES['accelerator'], CANDIDATE_PR='23',
                CANDIDATE_SHA=candidate, CANDIDATE_BASE_SHA=base_sha, CANDIDATE_HEAD_SHA=candidate,
                CANDIDATE_BASE_REF='main', COMPANION_CANDIDATES='[]')
            before = copy.deepcopy(selected)
            platform_sha = subject.tag_sources.git(roots['platform'], 'rev-parse', 'HEAD')
            with patch.dict(os.environ, environment), patch.object(resolver, 'public'), \
                 patch.object(resolver, 'baseline', return_value=selected), \
                 patch.object(resolver.release, 'branch_sha', return_value=platform_sha), \
                 patch.object(resolver, 'source_text', side_effect=AssertionError('no SHA source lookup')):
                plan = resolver.source_plan('a' * 40, root / 'admitted')
            self.assertEqual(selected, before)
            self.assertEqual(plan['case_files'], CASES)
            self.assertEqual(plan['changes']['accelerator'], ['test/e2e/cases/image.manifest.sh'])
            for arch in artifacts.ARCHES:
                self.assertEqual(plan['lanes'][arch]['products'], [])
                self.assertIn('image.manifest.sh', plan['lanes'][arch]['selection']['cases'])
            self.assertEqual(plan['test_revisions']['accelerator']['sha'], candidate)
            self.assertEqual(plan['baseline']['test_revisions']['accelerator']['sha'], 'd' * 40)
            self.assertEqual(plan['test_revisions']['orchestrator']['sha'], selected['units']['orchestrator']['sha'])
            self.assertEqual((root / 'admitted/candidates/accelerator/test/e2e/cases/image.manifest.sh').read_text(),
                             'echo candidate assertion\n')


class HistoricalEvidence(unittest.TestCase):
    def setUp(self):
        tests = artifacts.release_test_revisions({owner: 'd' * 40 for owner in artifacts.OWNERS if owner != 'platform'}, 'b' * 40)
        self.plan = {'schema': 2, 'mode': 'exact-assets', 'framework_sha': 'a' * 40, 'owners': ['platform'],
            'baseline': {'version': 'release-v1.2.3', 'sha': 'b' * 40, 'assets': [{'name': 'payload', 'digest': 'sha256:' + 'e' * 64}]},
            'case_files': CASES, 'test_revisions': tests, 'test_overlays': [],
            'lanes': {arch: {'selection': artifacts.suite_selection(['platform'], arch, CASES), 'products': [], 'performance': []}
                      for arch in artifacts.ARCHES}}
        self.binding = {'aggregate_sha': 'b' * 40, 'framework_sha': 'a' * 40, 'plan_id': artifacts.identity(self.plan),
                        'test_revisions': tests, 'assets': {'payload': 'sha256:' + 'e' * 64},
                        'architectures': {arch: architecture_result(self.plan, arch) for arch in artifacts.ARCHES}}

    def read(self, binding=None):
        return resolver.bound_plan(binding or self.binding, 'release-v1.2.3', 'b' * 40, self.binding['assets'], [{'id': 41}])

    def test_original_plan_recovers_ownership_and_true_independent_test_facts(self):
        def download(*args):
            self.assertEqual(args[:3], ('run', 'download', '41'))
            (Path(args[-1]) / 'plan.json').write_text(json.dumps(self.plan))
        listing = {'total_count': 1, 'artifacts': [{'name': 'integration-plan-41', 'expired': False}]}
        with patch.object(resolver.release, 'api', return_value=listing), patch.object(resolver.release, 'gh', side_effect=download), \
             patch.object(resolver, 'source_text', side_effect=AssertionError('no old SHA retrieval')):
            observed = self.read()
        self.assertEqual(observed['case_files'], CASES)
        self.assertEqual(observed['test_revisions']['connector']['sha'], 'd' * 40)
        self.assertNotIn('source_records', observed)

    def test_missing_expired_ambiguous_or_mismatched_evidence_fails(self):
        for rows in ([], [{'name': 'integration-plan-41', 'expired': True}],
                     [{'name': 'integration-plan-41', 'expired': False}] * 2):
            def download(*args): (Path(args[-1]) / 'plan.json').write_text(json.dumps(self.plan))
            with patch.object(resolver.release, 'api', return_value={'total_count': len(rows), 'artifacts': rows}), \
                 patch.object(resolver.release, 'gh', side_effect=download), self.assertRaisesRegex(ValueError, 'missing or ambiguous'):
                self.read()
        embedded = dict(self.binding, validation_plan=copy.deepcopy(self.plan))
        embedded['validation_plan']['case_files']['connector'] = ['basic.guessed.sh']
        with self.assertRaisesRegex(ValueError, 'plan identity mismatch'): self.read(embedded)

    def test_self_contained_plan_needs_no_retained_run_artifact(self):
        with patch.object(resolver.release, 'gh', side_effect=AssertionError('no artifact retrieval')), \
             patch.object(resolver.release, 'api', side_effect=AssertionError('no artifact lookup')):
            self.assertEqual(self.read(dict(self.binding, validation_plan=self.plan)), self.plan)

    def test_replaced_checksum_file_fails_even_when_all_archives_match(self):
        self.plan['baseline']['assets'].append({'name': 'SHA256SUMS', 'digest': 'sha256:' + '1' * 64})
        binding = dict(self.binding, validation_plan=self.plan, plan_id=artifacts.identity(self.plan))
        for result in binding['architectures'].values(): result['plan_id'] = binding['plan_id']
        complete = dict(binding['assets'], SHA256SUMS='sha256:' + '1' * 64)
        resolver.bound_plan(binding, 'release-v1.2.3', 'b' * 40, complete, [])
        complete['SHA256SUMS'] = 'sha256:' + '2' * 64
        with self.assertRaisesRegex(ValueError, 'complete asset inventory'):
            resolver.bound_plan(binding, 'release-v1.2.3', 'b' * 40, complete, [])

    def test_actual_legacy_plan_preserves_independent_test_provenance_and_tag_identity(self):
        plan = json.loads((Path(__file__).parent / 'fixtures/pre-cutover-plan.json').read_text())
        self.assertEqual(artifacts.identity(plan), '35addc8b111a87289f5691f3928febc85e624cdcc1112d69889f8282e56f9560')
        baseline = plan['baseline']
        self.assertNotEqual(plan['test_revisions']['accelerator']['sha'], baseline['units']['accelerator']['sha'])
        assets = {row['name']: row['digest'] for row in baseline['assets']}
        binding = dict(aggregate_sha=baseline['sha'], framework_sha=plan['framework_sha'],
                       test_revisions=plan['test_revisions'], plan_id=artifacts.identity(plan),
                       assets={name: value for name, value in assets.items() if name != 'SHA256SUMS'},
                       architectures={arch: architecture_result(plan, arch) for arch in artifacts.ARCHES},
                       validation_plan=plan)
        resolver.bound_plan(binding, baseline['version'], baseline['sha'], assets, [])
        original = baseline['units']
        resolver.check_published_units(original, plan)
        for field, value in [('sha', 'e' * 40), ('version', 'v99.0.0'), ('repository', 'kuasar-sandbox/connector')]:
            moved = copy.deepcopy(original); moved['accelerator'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'original validated plan'):
                resolver.check_published_units(moved, plan)


if __name__ == '__main__': unittest.main()
