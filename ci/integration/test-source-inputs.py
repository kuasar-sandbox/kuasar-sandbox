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
from test_fixtures import CASES, architecture_result, helper_contract
sys.path.insert(0, str(subject.ROOT / 'release'))
from test_tag_sources import fixture

spec = importlib.util.spec_from_file_location('evidence_resolver', Path(__file__).with_name('resolve-artifacts.py'))
resolver = importlib.util.module_from_spec(spec); spec.loader.exec_module(resolver)


def declare_fixture(root, owner=None):
    path = root / 'release/helper-inputs.json'
    path.parent.mkdir(exist_ok=True)
    value = (json.loads(helper_contract('a' * 40, 'b' * 40)['owners'][owner]['declaration']) if owner else
             {'schema': 1, 'recipe': None, 'dependencies': [], 'helpers': []})
    path.write_text(json.dumps(value))
    if value['recipe']:
        recipe = root / value['recipe']; recipe.parent.mkdir(parents=True, exist_ok=True)
        recipe.write_text('fixture recipe')



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

    def producer(self):
        spec = importlib.util.spec_from_file_location('tested_producer', subject.ROOT / 'release/producer-inputs.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        subject.tag_sources.git(self.remote, 'branch', '-M', 'main')
        return module

    def admit_producer(self, producer, sha=None, source_ref='main', version='v1.2.3', name='admission.tar'):
        archive = self.root / name
        with patch.dict(os.environ, self.env):
            producer.admit('connector', source_ref, sha or self.sha, version,
                           'kuasar-sandbox/connector', archive)
        return archive

    def test_producer_historical_commit_survives_branch_advance_and_remote_removal(self):
        producer = self.producer(); git = subject.tag_sources.git
        before = git(self.remote, 'ls-tree', '-r', self.sha)
        (self.remote / 'newer-helper.py').write_text('must not fill an old contract\n')
        (self.remote / 'README.md').write_text('new docs must not replace selected docs\n')
        git(self.remote, 'add', '.'); git(self.remote, 'commit', '-qm', 'new main')
        admitted_tip = git(self.remote, 'rev-parse', 'HEAD')
        admission = self.admit_producer(producer)
        git(self.remote, 'commit', '--allow-empty', '-qm', 'advance after admission')
        shutil.rmtree(self.remote)
        capsule = self.root / 'producer.tar'
        with patch.object(subject, 'fetch_named', side_effect=AssertionError('must not refetch')):
            producer.freeze('connector', 'main', self.sha, [], capsule, admission, 'v1.2.3')
        transport = self.root / 'inspect'; subject.transport.extract(capsule, transport)
        receipt = json.loads((transport / 'producer-records.json').read_text())
        self.assertEqual(receipt['admission']['source']['admitted_tip'], admitted_tip)
        self.assertEqual(receipt['admission']['source']['mode'], 'first-parent')
        self.assertEqual(receipt['admission']['control'], producer.producer_sources.control_identity())
        for arch in artifacts.ARCHES:
            target = self.root / arch
            producer.restore(capsule, 'connector', self.sha, target)
            self.assertEqual(git(target / 'connector', 'ls-tree', '-r', 'HEAD'), before)
            self.assertFalse((target / 'connector/newer-helper.py').exists())
            self.assertEqual(git(target / 'connector', 'branch', '--show-current'), '')

    def test_producer_accepts_head_and_matching_maintenance_history(self):
        producer = self.producer(); git = subject.tag_sources.git
        git(self.remote, 'branch', 'release/v1.2.x')
        for ref in ('main', 'release/v1.2.x'):
            with self.subTest(ref=ref):
                archive = self.admit_producer(producer, source_ref=ref, name=ref.replace('/', '-') + '.tar')
                output = self.root / ref.replace('/', '-')
                subject.transport.extract(archive, output)
                record = json.loads((output / 'admission.json').read_text())['source']
                self.assertEqual(record['source_ref'], ref)
                self.assertEqual(record['admitted_tip'], self.sha)

    def test_producer_cli_admits_all_six_unit_names(self):
        producer = self.producer()
        for unit in artifacts.UNITS:
            with self.subTest(unit=unit):
                repository = subject.tag_sources.repository(unit)
                version = (unit + '-' if unit in ('runtime', 'vmlinux') else '') + 'v1.2.3'
                archive = self.root / (unit + '.tar')
                environment = {**os.environ, **self.env,
                               'GIT_CONFIG_VALUE_0': 'https://github.com/' + repository + '.git'}
                subprocess.run([sys.executable, '-B', producer.__file__, 'admit', unit, 'main',
                                self.sha, version, repository, str(archive)], env=environment, check=True)
                target = self.root / unit
                subject.transport.extract(archive, target)
                record = json.loads((target / 'admission.json').read_text())['source']
                self.assertEqual(record['unit'], unit)
                self.assertEqual(record['repository'], repository)
                self.assertEqual(record['version'], version)

    def test_historical_producer_keeps_exact_dependency_tags(self):
        producer = self.producer(); admission = self.admit_producer(producer)
        dependency = self.root / 'dependency'
        sha = fixture(dependency, 'accelerator', 'v1.2.3')
        subject.tag_sources.git(dependency, 'tag', 'v1.2.4')
        env = {**self.env, 'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_1': f'url.{dependency}.insteadOf',
               'GIT_CONFIG_VALUE_1': 'https://github.com/kuasar-sandbox/accelerator.git'}
        capsule = self.root / 'producer.tar'
        with patch.dict(os.environ, env):
            producer.freeze('connector', 'main', self.sha, [('accelerator', 'v1.2.3', sha)],
                            capsule, admission, 'v1.2.3')
        shutil.rmtree(self.remote); shutil.rmtree(dependency)
        producer.restore(capsule, 'connector', self.sha, self.root / 'exact', [('accelerator', 'v1.2.3', sha)])
        for dependencies in ([], [('accelerator', 'v1.2.4', sha)], [('accelerator', 'v1.2.3', 'f' * 40)]):
            with self.subTest(dependencies=dependencies), self.assertRaisesRegex(ValueError, 'differs from preflight'):
                producer.restore(capsule, 'connector', self.sha, self.root / 'bad', dependencies)

    def test_producer_rejects_side_parent_even_when_reachable_from_merge(self):
        producer = self.producer(); git = subject.tag_sources.git
        git(self.remote, 'checkout', '-qb', 'side')
        (self.remote / 'side').write_text('side')
        git(self.remote, 'add', '.'); git(self.remote, 'commit', '-qm', 'side')
        side = git(self.remote, 'rev-parse', 'HEAD')
        git(self.remote, 'checkout', '-q', 'main')
        git(self.remote, 'merge', '--no-ff', '-qm', 'merge side', 'side')
        with self.assertRaisesRegex(ValueError, 'first-parent'):
            self.admit_producer(producer, sha=side)
        self.assertFalse((self.root / 'admission.tar').exists())

    def test_producer_rejects_unrelated_or_missing_commit(self):
        producer = self.producer(); git = subject.tag_sources.git
        git(self.remote, 'checkout', '--orphan', 'unrelated')
        git(self.remote, 'commit', '-qm', 'unrelated root')
        unrelated = git(self.remote, 'rev-parse', 'HEAD')
        for sha in (unrelated, 'f' * 40):
            with self.subTest(sha=sha), self.assertRaisesRegex(ValueError, 'first-parent'):
                self.admit_producer(producer, sha=sha)

    def test_producer_request_validation_all_units_and_negative_cases(self):
        producer = self.producer(); validate = producer.producer_sources.validate_request
        for unit in artifacts.UNITS:
            prefix = unit + '-' if unit in ('runtime', 'vmlinux') else ''
            repo = subject.tag_sources.repository(unit)
            for suffix in ('', '-preview.20261026', '-preview.20261026.2'):
                validate(unit, 'main', self.sha, prefix + 'v1.2.3' + suffix, repo)
                validate(unit, 'release/v1.2.x', self.sha, prefix + 'v1.2.3' + suffix, repo)
        invalid = [
            ('connector', 'main', self.sha, 'v1.2.3', 'kuasar-sandbox/sandboxer'),
            ('platform', 'main', self.sha, 'v1.2.3', 'kuasar-sandbox/kuasar-sandbox'),
            ('connector', 'refs/heads/main', self.sha, 'v1.2.3', 'kuasar-sandbox/connector'),
            ('connector', 'refs/pull/1/merge', self.sha, 'v1.2.3', 'kuasar-sandbox/connector'),
            ('connector', 'release/v01.2.x', self.sha, 'v1.2.3', 'kuasar-sandbox/connector'),
            ('connector', 'release/v1.2.x', self.sha, 'v1.3.0', 'kuasar-sandbox/connector'),
            ('connector', 'main', 'HEAD', 'v1.2.3', 'kuasar-sandbox/connector'),
            ('connector', 'main', 'A' * 40, 'v1.2.3', 'kuasar-sandbox/connector'),
            ('connector', 'main', self.sha, 'runtime-v1.2.3', 'kuasar-sandbox/connector'),
        ]
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError): validate(*request)

    def test_producer_missing_branch_does_not_fetch_raw_sha(self):
        producer = self.producer()
        with self.assertRaisesRegex(ValueError, 'git fetch failed'):
            self.admit_producer(producer, source_ref='release/v1.2.x')
        self.assertFalse((self.root / 'admission.tar').exists())

    def test_producer_archive_rejects_conflicting_request_and_control(self):
        producer = self.producer(); admission = self.admit_producer(producer)
        for ref, sha, version in [('main', 'f' * 40, 'v1.2.3'), ('release/v1.2.x', self.sha, 'v1.2.3'),
                                  ('main', self.sha, 'v1.2.4'), ('main', self.sha, None)]:
            with self.subTest(ref=ref, sha=sha, version=version), self.assertRaisesRegex(ValueError, 'differs from release request'):
                producer.freeze('connector', ref, sha, [], self.root / 'bad.tar', admission, version)
        with patch.object(producer.producer_sources, 'control_identity', return_value={'framework_sha': 'f' * 40}), \
             self.assertRaisesRegex(ValueError, 'control identity changed'):
            producer.freeze('connector', 'main', self.sha, [], self.root / 'bad.tar', admission, 'v1.2.3')

    def test_producer_archive_detects_source_or_evidence_tampering(self):
        producer = self.producer(); admission = self.admit_producer(producer)
        for field, value in [('tree', 'f' * 40), ('admitted_tip', 'f' * 40), ('mode', 'arbitrary-sha')]:
            with self.subTest(field=field):
                root = self.root / field
                subject.transport.extract(admission, root)
                receipt = json.loads((root / 'admission.json').read_text())
                receipt['source'][field] = value
                (root / 'admission.json').write_text(json.dumps(receipt))
                bad = self.root / (field + '.tar')
                with tarfile.open(bad, 'w') as archive:
                    for path in root.iterdir(): archive.add(path, arcname=path.name)
                with self.assertRaises(ValueError):
                    producer.freeze('connector', 'main', self.sha, [], self.root / 'bad.tar', bad, 'v1.2.3')
        root = self.root / 'dirty'; subject.transport.extract(admission, root)
        (root / 'source/README.md').write_text('tampered')
        with tarfile.open(self.root / 'dirty.tar', 'w') as archive:
            for path in root.iterdir(): archive.add(path, arcname=path.name)
        with self.assertRaisesRegex(ValueError, 'modified inputs'):
            producer.freeze('connector', 'main', self.sha, [], self.root / 'bad.tar', self.root / 'dirty.tar', 'v1.2.3')

    def test_historical_producer_does_not_relax_named_ref_admission(self):
        self.producer(); git = subject.tag_sources.git
        git(self.remote, 'commit', '--allow-empty', '-qm', 'advance')
        for ref in ('refs/heads/main', 'refs/pull/23/merge'):
            git(self.remote, 'update-ref', ref, git(self.remote, 'rev-parse', 'HEAD'))
            with patch.dict(os.environ, self.env), self.assertRaisesRegex(ValueError, 'identity changed'):
                subject.fetch_named(self.root / ref.replace('/', '-'), 'kuasar-sandbox/connector', ref, self.sha)

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
        for suffix, modern in [(suffix, modern) for suffix in ('', '.git') for modern in (False, True)]:
            with self.subTest(suffix=suffix, modern=modern):
                root = self.root / (('canonical' if not suffix else 'suffixed') + ('-modern' if modern else '-legacy'))
                inputs = root / 'inputs'
                platform = inputs / 'platform'
                fixture(platform, 'connector', 'v0.0.1')
                origin = 'https://github.com/' + resolver.PLATFORM + suffix
                git(platform, 'remote', 'set-url', 'origin', origin)
                shutil.rmtree(platform / 'test/e2e/cases')
                cases = platform / 'test/e2e/platform/cases'; cases.mkdir(parents=True)
                for name in CASES['platform']: (cases / name).write_text('echo platform\n')
                if modern:
                    marker = cases.parent / 'lib/requirements.json'; marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text('{"schema":1}\n')
                    for case in cases.iterdir():
                        case.write_text('# e2e-requires: ["kvm"]\n' + case.read_text())
                records, units = {}, {}
                for unit in resolver.release.selection.UNITS:
                    owner = 'guest-runtime' if unit in ('runtime', 'vmlinux') else unit
                    tag = (unit + '-' if unit in ('runtime', 'vmlinux') else '') + 'v1.2.3'
                    source = inputs / 'sources' / unit
                    fixture(source, unit, 'v0.0.1')
                    shutil.rmtree(source / 'test/e2e/cases')
                    (source / 'test/e2e/cases').mkdir()
                    for name in CASES[owner]: (source / 'test/e2e/cases' / name).write_text('echo owner\n')
                    if modern:
                        marker = source / 'test/e2e/lib/requirements.json'; marker.parent.mkdir(parents=True, exist_ok=True)
                        marker.write_text('{"schema":1}\n')
                        for case in (source / 'test/e2e/cases').iterdir():
                            case.write_text('# e2e-requires: []\n' + case.read_text())
                    declare_fixture(source, owner)
                    git(source, 'add', '.'); git(source, 'commit', '-qm', 'owner inputs')
                    git(source, 'tag', tag)
                    records[unit] = subject.tag_sources.inspect_unit(source, unit, tag)
                    units[unit] = tag
                manifest = 'version: release-v1.2.3\ndelivery: workbench-v1\ncomponents:\n' + ''.join(
                    f'  {unit}: {tag}\n' for unit, tag in units.items())
                (platform / 'releases').mkdir()
                (platform / 'releases/release.yaml').write_text(manifest)
                (platform / 'releases/daily-preview.yaml').write_text(manifest + 'preview_version: preview.20260101\n')
                declare_fixture(platform, "platform")
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
                self.assertEqual(plan['schema'], 4 if modern else 3)
                if modern:
                    contract = plan['case_requirements']['cases']
                    self.assertEqual(set(contract), {name for names in CASES.values() for name in names})
                    self.assertEqual(contract['basic.demo.sh']['requires'], ['kvm'])
                    self.assertEqual(plan['lanes']['aarch64']['groups']['supplemental-arm--basic']['cases'], ['basic.demo.sh'])
                    self.assertEqual(plan['lanes']['aarch64']['selection']['cases'],
                                     artifacts.suite_selection(['platform'], 'aarch64', CASES, native_full=True)['cases'])
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
                declare_fixture(repo, owner)
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
                 patch.object(resolver, 'maintained_units', return_value={unit: row['version'] for unit, row in selected['units'].items()}), \
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



class MaintainedSelection(unittest.TestCase):
    def release_state(self, unit, tag, sha):
        return {'tag_name': tag, 'draft': False, 'prerelease': '-preview.' in tag,
                'target_commitish': sha, 'assets': [
                    {'name': name, 'state': 'uploaded', 'size': 1, 'digest': 'sha256:' + 'e' * 64}
                    for name in ['SHA256SUMS', *(artifacts.archive_name(unit, tag, arch) for arch in artifacts.ARCHES)]]}

    def test_missing_aggregate_uses_only_explicit_validated_predecessor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'releases').mkdir()
            manifest = 'version: release-v1.2.3\npreview_version: preview.20261019.1\nprevious_preview_version: preview.20261015.1\ncomponents:\n'
            manifest += ''.join(f"  {unit}: {unit + '-' if unit in ('runtime', 'vmlinux') else ''}v1.2.4\n" for unit in artifacts.UNITS)
            (root / 'releases/daily-preview.yaml').write_text(manifest)
            original = {'historical': 'unchanged'}
            with patch.object(resolver, 'aggregate', side_effect=[None, original]) as lookup:
                self.assertIs(resolver.baseline(root, 'main'), original)
                self.assertEqual([call.args[0] for call in lookup.call_args_list],
                                 ['release-v1.2.3-preview.20261019.1', 'release-v1.2.3-preview.20261015.1'])
            with patch.object(resolver, 'aggregate', return_value=None), self.assertRaisesRegex(ValueError, 'published validated baseline'):
                resolver.baseline(root, 'main')

    def test_incomplete_or_unverified_selected_publications_fail_closed(self):
        good = self.release_state('connector', 'v1.2.4', 'a' * 40)
        bad = [None, dict(good, draft=True), dict(good, target_commitish='b' * 40),
               dict(good, tag_name='v1.2.5'), dict(good, prerelease=True),
               dict(good, assets=good['assets'][:-1]), dict(good, assets=good['assets'] + good['assets'][:1])]
        for field, value in [('state', 'new'), ('size', 0), ('digest', None)]:
            row = copy.deepcopy(good); row['assets'][0][field] = value; bad.append(row)
        with patch.object(resolver, 'public'), patch.object(resolver.release, 'tag_sha', return_value='a' * 40):
            for state in bad:
                with self.subTest(state=state), patch.object(resolver.release, 'api_optional', return_value=state), self.assertRaises(ValueError):
                    resolver.published_unit('connector', 'v1.2.4')
            with patch.object(resolver.release, 'api_optional', return_value=good):
                self.assertEqual(resolver.published_unit('connector', 'v1.2.4'), 'a' * 40)

    def test_cutover_product_closure_and_independent_kernel(self):
        products = resolver.maintained_products({'connector': {}})
        self.assertTrue({'connector-ctl', 'sandbox-ctl', 'sandbox-init', 'sandbox-runtime.bundle',
                         'node-ctl', 'cluster-ctl', 'node-stub-ctl', 'e2b-key-ctl'} <= products)
        self.assertNotIn('vmlinux', products)
        self.assertEqual(resolver.maintained_products({'vmlinux': {}}), {'vmlinux'})
        self.assertNotIn('mkfs.erofs', resolver.maintained_products(dict.fromkeys(artifacts.UNITS)))

    def test_modern_manifest_tags_overlay_legacy_aggregate_and_admitted_companion(self):
        self.run_cutover()

    def test_partial_requirement_migration_still_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'mixed E2E requirement generations'):
            self.run_cutover(legacy_owner='connector')

    def test_runtime_tag_cannot_select_kernel_from_its_makefile(self):
        self.run_cutover(kernel_delta=False)

    def test_release_line_uses_maintained_component_minor(self):
        self.run_cutover(base_ref='release/v1.2.x')

    def test_complete_modern_baseline_keeps_non_candidate_owners(self):
        self.run_cutover(complete_baseline=True)

    def test_complete_modern_baseline_keeps_maintenance_admission(self):
        self.run_cutover(complete_baseline=True, base_ref='release/v1.2.x')

    def test_complete_modern_baseline_preserves_each_candidate_and_companion(self):
        for owner in artifacts.OWNERS:
            with self.subTest(owner=owner):
                self.run_cutover(complete_baseline=True, candidate_owner=owner)

    def test_complete_modern_baseline_rejects_candidate_requirement_downgrade(self):
        with self.assertRaisesRegex(ValueError, 'mixed E2E requirement generations'):
            self.run_cutover(complete_baseline=True, candidate_owner='connector', downgrade_candidate=True)

    def run_cutover(self, legacy_owner=None, kernel_delta=True, base_ref='main', complete_baseline=False,
                    candidate_owner='platform', downgrade_candidate=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = subject.tag_sources.git
            env = {'GIT_CONFIG_COUNT': '6'}
            baseline = {'repository': resolver.PLATFORM, 'version': 'release-v1.2.3', 'sha': 'b' * 40,
                        'units': {}, 'assets': [], 'case_files': copy.deepcopy(CASES),
                        'test_revisions': artifacts.release_test_revisions(
                            {owner: 'd' * 40 for owner in artifacts.OWNERS if owner != 'platform'}, 'b' * 40)}
            repos, maintained, shas = {}, {}, {}
            for index, owner in enumerate(artifacts.OWNERS):
                repo = root / owner; repos[owner] = repo
                fixture(repo, 'connector', 'v0.0.1')
                repository = resolver.REPOSITORIES[owner]
                git(repo, 'remote', 'set-url', 'origin', 'https://github.com/' + repository + '.git')
                git(repo, 'branch', '-M', 'main')
                shutil.rmtree(repo / 'test/e2e/cases')
                e2e = repo / ('test/e2e/platform' if owner == 'platform' else 'test/e2e')
                (e2e / 'cases').mkdir(parents=True)
                for name in CASES[owner]: (e2e / 'cases' / name).write_text('echo historical\n')
                declare_fixture(repo, owner)
                if complete_baseline:
                    (e2e / 'lib').mkdir(exist_ok=True)
                    (e2e / 'lib/requirements.json').write_text('{"schema":1}\n')
                    for name in CASES[owner]: (e2e / 'cases' / name).write_text('# e2e-requires: []\necho baseline\n')
                if owner == 'guest-runtime':
                    (repo / 'native-deps').mkdir(exist_ok=True)
                    (repo / 'native-deps/Makefile').write_text('vmlinux: old-kernel-input\n')
                git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'historical cases')
                old_sha = git(repo, 'rev-parse', 'HEAD')
                units = () if owner == 'platform' else ('runtime', 'vmlinux') if owner == 'guest-runtime' else (owner,)
                for unit in units:
                    prefix = unit + '-' if unit in ('runtime', 'vmlinux') else ''
                    old_tag, tag = prefix + 'v1.1.3', prefix + 'v1.2.4'
                    git(repo, 'tag', old_tag)
                    baseline['units'][unit] = {'repository': repository, 'version': old_tag, 'sha': old_sha}
                    maintained[unit] = tag
                if owner != legacy_owner:
                    (e2e / 'lib').mkdir(exist_ok=True)
                    (e2e / 'lib/requirements.json').write_text('{"schema": 1}')
                    for name in CASES[owner]: (e2e / 'cases' / name).write_text('# e2e-requires: []\necho modern\n')
                (repo / 'source-change.txt').write_text('explicit tag source delta')
                if owner == 'guest-runtime':
                    (repo / 'native-deps').mkdir(exist_ok=True)
                    (repo / 'native-deps/Makefile').write_text('vmlinux: new-kernel-input\n')
                git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'modern tag')
                sha = git(repo, 'rev-parse', 'HEAD')
                for unit in units:
                    if unit == 'vmlinux':
                        (repo / 'kernel-source.txt').write_text('independent kernel revision')
                        git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'kernel tag')
                        sha = git(repo, 'rev-parse', 'HEAD')
                    git(repo, 'tag', maintained[unit]); shas[unit] = sha
                env['GIT_CONFIG_KEY_' + str(index)] = f'url.{repo}.insteadOf'
                env['GIT_CONFIG_VALUE_' + str(index)] = 'https://github.com/' + repository + '.git'
            if not kernel_delta:
                maintained['vmlinux'] = baseline['units']['vmlinux']['version']
                shas['vmlinux'] = baseline['units']['vmlinux']['sha']
            platform = repos['platform']
            (platform / 'releases').mkdir()
            stable = 'version: release-v1.2.3\ncomponents:\n' + ''.join(f'  {unit}: {tag}\n' for unit, tag in maintained.items())
            (platform / 'releases/release.yaml').write_text(stable)
            (platform / 'releases/daily-preview.yaml').write_text(stable + 'preview_version: preview.20261019.1\n')
            git(platform, 'add', '.'); git(platform, 'commit', '-qm', 'trusted manifest')
            platform_base = git(platform, 'rev-parse', 'HEAD')
            # A valid but untrusted candidate manifest must not choose sources.
            (platform / 'releases/daily-preview.yaml').write_text((stable + 'preview_version: preview.20261019.1\n').replace('v1.2.4', 'v1.2.5'))
            git(platform, 'commit', '-qam', 'candidate manifest cannot redirect')
            candidate = git(platform, 'rev-parse', 'HEAD')
            git(platform, 'update-ref', 'refs/pull/23/merge', candidate)
            git(platform, 'update-ref', 'refs/heads/' + base_ref, platform_base)
            candidate_base = platform_base
            if candidate_owner != 'platform':
                primary = repos[candidate_owner]
                candidate_base = git(primary, 'rev-parse', 'HEAD')
                e2e = primary / 'test/e2e'
                (e2e / 'cases' / CASES[candidate_owner][0]).write_text('# e2e-requires: []\necho candidate\n')
                if downgrade_candidate:
                    (e2e / 'lib/requirements.json').unlink()
                    for name in CASES[candidate_owner]:
                        (e2e / 'cases' / name).write_text('echo legacy candidate\n')
                git(primary, 'add', '.'); git(primary, 'commit', '-qm', 'candidate test')
                candidate = git(primary, 'rev-parse', 'HEAD')
                git(primary, 'update-ref', 'refs/pull/23/merge', candidate)
            companion_owner = 'sandboxer' if candidate_owner == 'orchestrator' else 'orchestrator'
            companion = repos[companion_owner]
            companion_base = git(companion, 'rev-parse', 'HEAD')
            (companion / 'test/e2e/cases' / CASES[companion_owner][0]).write_text('# e2e-requires: []\necho companion\n')
            git(companion, 'commit', '-qam', 'companion test')
            companion_sha = git(companion, 'rev-parse', 'HEAD')
            git(companion, 'update-ref', 'refs/pull/24/merge', companion_sha)
            env.update(CANDIDATE_REPOSITORY=resolver.REPOSITORIES[candidate_owner], CANDIDATE_PR='23', CANDIDATE_SHA=candidate,
                       CANDIDATE_BASE_SHA=candidate_base, CANDIDATE_HEAD_SHA=candidate, CANDIDATE_BASE_REF=base_ref,
                       COMPANION_CANDIDATES=json.dumps([{'repository': resolver.REPOSITORIES[companion_owner],
                           'pull_request_number': 24, 'base_ref': base_ref, 'base_sha': companion_base,
                           'head_sha': companion_sha, 'candidate_sha': companion_sha}]))
            original = copy.deepcopy(baseline)
            if complete_baseline:
                baseline['validation_plan'] = {'schema': 4}
                original = copy.deepcopy(baseline)
            def state(path):
                unit = next(unit for unit, tag in maintained.items() if path.endswith('/' + tag)
                            and path.split('/')[2] == resolver.REPOSITORIES[resolver.unit_owner(unit)].split('/')[1])
                return self.release_state(unit, maintained[unit], shas[unit])
            with patch.dict(os.environ, env), patch.object(resolver, 'public'), \
                 patch.object(resolver, 'baseline', return_value=baseline), \
                 patch.object(resolver.release, 'branch_sha', return_value=platform_base), \
                 patch.object(resolver.release, 'api_optional', side_effect=state), \
                 patch.object(resolver.release, 'tag_sha', side_effect=lambda repo, tag: shas[next(unit for unit in maintained if maintained[unit] == tag and resolver.REPOSITORIES[resolver.unit_owner(unit)] == repo)]):
                plan = resolver.source_plan('a' * 40, root / 'admitted')
            self.assertEqual(baseline, original)
            self.assertEqual(plan['schema'], 4)
            self.assertEqual(plan['baseline'], original)
            if complete_baseline:
                self.assertEqual(plan['maintained_deltas'], {})
                if candidate_owner != 'guest-runtime':
                    self.assertEqual(plan['kernel_sha'], baseline['units']['vmlinux']['sha'])
                admitted = {candidate_owner: (candidate, 'candidate'), companion_owner: (companion_sha, 'companion')}
                for owner in artifacts.OWNERS:
                    if owner in admitted:
                        sha, role = admitted[owner]
                        self.assertEqual(plan['sources'][owner]['sha'], sha)
                        self.assertEqual(plan['sources'][owner]['role'], role)
                        self.assertEqual(plan['test_revisions'][owner], plan['sources'][owner])
                        self.assertEqual(plan['run_inputs']['candidates/' + owner]['sha'], sha)
                        continue
                    unit = 'runtime' if owner == 'guest-runtime' else owner
                    expected = platform_base if owner == 'platform' else baseline['units'][unit]['sha']
                    self.assertEqual(plan['sources'][owner]['sha'], expected)
                    self.assertEqual(plan['test_revisions'][owner]['sha'], expected)
                self.assertEqual(set(plan['owners']), set(admitted))
                for product, dependencies in plan['product_sources'].items():
                    for repository, sha in dependencies.items():
                        owner = next(owner for owner in artifacts.OWNERS if resolver.REPOSITORIES[owner] == repository)
                        self.assertEqual(sha, plan['kernel_sha'] if product == 'vmlinux' else plan['sources'][owner]['sha'])
                for unit, row in plan['source_records'].items():
                    self.assertEqual(row['tag'], baseline['units'][unit]['version'])
                    self.assertEqual(row['sha'], baseline['units'][unit]['sha'])
                artifacts.check_plan(plan)
                return
            self.assertEqual(plan['kernel_sha'], shas['vmlinux'])
            self.assertNotEqual(plan['kernel_sha'], plan['sources']['guest-runtime']['sha'])
            self.assertEqual(plan['test_revisions']['orchestrator']['sha'], companion_sha)
            # Joint qualification must retain the admitted consumer test source
            # and the selected dependency tuple; a rebuilt consumer is not an
            # authorization to substitute another owner main or test revision.
            self.assertEqual(plan['sources']['orchestrator']['sha'], companion_sha)
            for owner, unit in (('accelerator', 'accelerator'), ('sandboxer', 'sandboxer'),
                                ('guest-runtime', 'runtime')):
                self.assertEqual(plan['sources'][owner]['sha'], shas[unit])
                self.assertEqual(plan['test_revisions'][owner]['sha'], shas[unit])
                self.assertEqual(plan['product_sources']['sandbox-runtime.bundle'][resolver.REPOSITORIES[owner]], shas[unit])
            for owner in ('accelerator', 'sandboxer'):
                self.assertEqual(plan['product_sources']['node-ctl'][resolver.REPOSITORIES[owner]], shas[owner])
            self.assertIn('source-change.txt', plan['changes']['orchestrator'])
            self.assertEqual(set(plan['maintained_deltas']), set(artifacts.UNITS) - (set() if kernel_delta else {'vmlinux'}))
            for unit, row in plan['source_records'].items():
                self.assertEqual((row['tag'], row['sha']), (maintained[unit], shas[unit]))
                self.assertEqual(row, {key: plan['run_inputs'][('maintained/' if unit in plan['maintained_deltas'] else 'released/') + unit][key] for key in row})
            for arch in artifacts.ARCHES:
                self.assertEqual(set(plan['lanes'][arch]['products']), set(artifacts.PRODUCTS) - {'mkfs.erofs'} - (set() if kernel_delta else {'vmlinux'}))
                self.assertEqual(plan['lanes'][arch]['embedded_products'], ['envd'])
            if kernel_delta:
                self.assertEqual(plan['product_sources']['vmlinux'], {'kuasar-sandbox/guest-runtime': shas['vmlinux']})
            else:
                self.assertNotIn('vmlinux', plan['product_sources'])
            self.assertEqual(plan['product_sources']['node-ctl']['kuasar-sandbox/orchestrator'], companion_sha)
            self.assertEqual(plan['product_sources']['node-ctl']['kuasar-sandbox/connector'], shas['connector'])
            artifacts.check_plan(plan)
            spec = importlib.util.spec_from_file_location('cutover_build', Path(__file__).with_name('build-artifacts.py'))
            build = importlib.util.module_from_spec(spec); spec.loader.exec_module(build)
            capsule = root / 'inputs.tar'
            with tarfile.open(capsule, 'w') as archive:
                for entry in sorted((root / 'admitted').iterdir()): archive.add(entry, arcname=entry.name)
            for arch in artifacts.ARCHES:
                output = root / arch
                layout = build.source_layout(plan, arch)
                if kernel_delta:
                    self.assertEqual(layout['kernel-unit/guest-runtime']['sha'], shas['vmlinux'])
                with patch.object(subject, 'INPUT_ARCHIVE', capsule):
                    subject.copy_run_inputs(plan, layout, output)
                self.assertEqual(git(output / 'orchestrator', 'rev-parse', 'HEAD'), companion_sha)
                self.assertEqual(git(output / 'guest-runtime', 'rev-parse', 'HEAD'), shas['runtime'])
                if kernel_delta:
                    self.assertEqual(git(output / 'kernel-unit/guest-runtime', 'rev-parse', 'HEAD'), shas['vmlinux'])

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

    def test_retired_native_prerequisite_is_the_only_accepted_selection_difference(self):
        for arch in artifacts.ARCHES:
            current = artifacts.suite_selection(['platform'], arch, CASES)
            legacy = dict(current, required_products=sorted(current['required_products'] + ['mkfs.erofs']))
            for selection in (current, legacy):
                self.assertTrue(artifacts.compatible_suite_selection(selection, ['platform'], arch, CASES))
                for name in ('flatten-ctl', 'store-ctl'):
                    changed = copy.deepcopy(selection)
                    changed['required_products'].remove(name)
                    self.assertFalse(artifacts.compatible_suite_selection(changed, ['platform'], arch, CASES))
                changed = copy.deepcopy(selection)
                changed['cases'] = changed['cases'][1:]
                self.assertFalse(artifacts.compatible_suite_selection(changed, ['platform'], arch, CASES))

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
