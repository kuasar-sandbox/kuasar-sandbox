#!/usr/bin/env python3
"""Exercise the CI boundary without Docker, credentials, or external requests."""
import argparse
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(os.environ.get('MAIN_WORKBENCH_ROOT', Path(__file__).resolve().parents[2])).resolve()
spec = importlib.util.spec_from_file_location('ci_workbench_security_subject', ROOT / 'ci/hosted/workbench.py')
ci = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ci
spec.loader.exec_module(ci)
FRAMEWORK, SOURCE = '1' * 40, '2' * 40


def selection():
    return {
        'framework_sha': FRAMEWORK, 'source_revision': SOURCE,
        'aggregate_version': 'release-v0.1.6-preview.20261009',
        'registry_index': 'sha256:' + '3' * 64,
        'architectures': {
            arch: {'reference': ci.REGISTRY + '@sha256:' + digit * 64,
                   'image_id': 'sha256:' + digit * 64, 'archive_sha256': digit * 64,
                   'archive_size': 1, 'qualification_scope': 'native-full'}
            for arch, digit in [('x86_64', '4'), ('aarch64', '5')]
        },
    }


def image(arch='x86_64'):
    selected = selection()
    return {'Id': selected['architectures'][arch]['image_id'], 'Os': 'linux',
            'Architecture': ci.PLATFORMS[arch], 'Size': 100,
            'Config': {'Labels': {
                'org.opencontainers.image.version': selected['aggregate_version'],
                'org.opencontainers.image.revision': SOURCE,
                'org.opencontainers.image.source': 'https://github.com/kuasar-sandbox/kuasar-sandbox',
            }}}


class TemporaryFiles(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ci-workbench-security-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def receipt(self):
        root = self.root / 'state'
        root.mkdir()
        sources = self.root / 'sources'
        sources.mkdir()
        for name in ('home', 'build', 'output'):
            (root / 'instances/ci' / name).mkdir(parents=True)
        value = {'owner_uid': os.getuid(), 'framework_sha': FRAMEWORK,
                 'sources': str(sources), 'started_ns': time.time_ns(), 'commands': [],
                 'arch': 'x86_64', 'image': selection()['architectures']['x86_64'],
                 'conclusion': 'running'}
        ci.write(root / 'receipt.json', value)
        return root


class SelectionTests(unittest.TestCase):
    def test_selection_binds_the_checked_out_framework(self):
        ci.check_selection(selection(), FRAMEWORK)
        with self.assertRaisesRegex(ValueError, 'different trusted framework'):
            ci.check_selection(selection(), '9' * 40)

    def test_selection_requires_both_immutable_native_images(self):
        for arch in ('x86_64', 'aarch64'):
            for field, value in [('reference', ci.REGISTRY + ':latest'),
                                 ('image_id', 'sha256:short'), ('archive_size', 0),
                                 ('archive_sha256', ''), ('qualification_scope', 'unchecked')]:
                with self.subTest(arch=arch, field=field):
                    changed = selection()
                    changed['architectures'][arch][field] = value
                    with self.assertRaises(ValueError):
                        ci.check_selection(changed)
        changed = selection()
        del changed['architectures']['aarch64']
        with self.assertRaisesRegex(ValueError, 'both native architectures'):
            ci.check_selection(changed)
        changed = selection()
        changed['architectures']['x86_64']['qualification_scope'] = 'artifact-only'
        with self.assertRaises(ValueError):
            ci.check_selection(changed)

    def test_config_architecture_and_release_are_all_binding(self):
        for arch in ('x86_64', 'aarch64'):
            ci.image_check(selection(), arch, image(arch))
        for field, value in [('Id', 'sha256:' + '8' * 64), ('Os', 'windows'),
                             ('Architecture', 'arm64')]:
            with self.subTest(field=field):
                changed = image()
                changed[field] = value
                with self.assertRaisesRegex(ValueError, 'image/config/architecture mismatch'):
                    ci.image_check(selection(), 'x86_64', changed)
        for field in ('version', 'revision', 'source'):
            with self.subTest(label=field):
                changed = image()
                changed['Config']['Labels']['org.opencontainers.image.' + field] = 'foreign'
                with self.assertRaisesRegex(ValueError, 'release/source mismatch'):
                    ci.image_check(selection(), 'x86_64', changed)

    def test_wrong_native_runner_is_rejected_before_pull(self):
        with patch.object(ci.platform, 'machine', return_value='x86_64'), patch.object(
                ci.launcher, 'inspect') as inspect, patch.object(ci.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'native runner'):
                ci.acquire(selection(), 'aarch64')
            inspect.assert_not_called()
            run.assert_not_called()

    def test_cold_pull_uses_the_frozen_digest_and_verifies_the_config(self):
        with patch.object(ci.platform, 'machine', return_value='x86_64'), patch.object(
                ci.launcher, 'inspect', side_effect=[None, image()]), patch.object(
                ci.subprocess, 'run') as run:
            result = ci.acquire(selection(), 'x86_64')
        run.assert_called_once_with(['docker', 'pull', selection()['architectures']['x86_64']['reference']],
                                    check=True, timeout=1800)
        self.assertFalse(result['image_present_before'])

    def test_warm_exact_image_does_not_resolve_or_pull_latest(self):
        with patch.object(ci.platform, 'machine', return_value='x86_64'), patch.object(
                ci.launcher, 'inspect', return_value=image()), patch.object(ci.subprocess, 'run') as run:
            self.assertTrue(ci.acquire(selection(), 'x86_64')['image_present_before'])
            run.assert_not_called()


class CacheScopeTests(TemporaryFiles):
    def setUp(self):
        super().setUp()
        self.sources = self.root / 'sources'
        self.sources.mkdir()
        self.args = argparse.Namespace(sources=self.sources, receipt=self.root / 'scope.json')
        self.env = {'GITHUB_REPOSITORY': 'kuasar-sandbox/kuasar-sandbox',
                    'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                    'GITHUB_RUN_ID': '10', 'GH_TOKEN': 'offline-fixture-not-a-credential',
                    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}
        self.environment = patch.dict(os.environ, self.env)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def repository(self):
        source = self.sources / 'sandboxer'
        subprocess.run(['git', 'init', '-q', source], check=True)
        for key, value in [('user.name', 'Chen Xiaohui'), ('user.email', 'graych@gmail.com'),
                           ('commit.gpgsign', 'false')]:
            subprocess.run(['git', '-C', source, 'config', '--local', key, value], check=True)
        subprocess.run(['git', '-C', source, 'remote', 'add', 'origin',
                        'https://github.com/kuasar-sandbox/sandboxer.git'], check=True)
        (source / 'source.txt').write_text('admitted source\n')
        subprocess.run(['git', '-C', source, 'add', 'source.txt'], check=True)
        subprocess.run(['git', '-C', source, 'commit', '-q', '-m', 'fixture'], check=True)
        return source

    def scope(self, admitted=False):
        stream = io.StringIO()
        with patch.object(ci.cache_policy, 'public_main', return_value=admitted), redirect_stdout(stream):
            ci.cache_scope(self.args)
        return stream.getvalue().strip()

    def test_candidate_dispatch_from_main_is_not_trusted(self):
        self.repository()
        self.assertRegex(self.scope(), r'^candidate-[0-9a-f]{64}$')
        self.assertEqual(json.loads(self.args.receipt.read_text())['scope'], 'candidate')

    def test_clean_main_source_has_a_usable_trusted_save_scope(self):
        self.repository()
        self.assertEqual(self.scope(admitted=True), 'trusted')

    def test_dirty_and_unversioned_sources_never_acquire_main_trust(self):
        source = self.repository()
        (source / 'source.txt').write_text('candidate change\n')
        self.assertRegex(self.scope(admitted=True), r'^candidate-[0-9a-f]{64}$')
        self.args.receipt.unlink()
        (source / 'source.txt').write_text('admitted source\n')
        (self.sources / 'unversioned').mkdir()
        self.assertRegex(self.scope(admitted=True), r'^candidate-[0-9a-f]{64}$')

    def test_candidate_receipt_prevents_host_git_execution_after_build(self):
        source = self.repository()
        self.assertRegex(self.scope(), r'^candidate-[0-9a-f]{64}$')
        marker = self.root / 'hook-ran'
        hook = self.root / 'fsmonitor'
        hook.write_text('#!/bin/sh\nprintf fixture > "$WORKBENCH_HOOK_MARKER"\n')
        hook.chmod(0o755)
        subprocess.run(['git', '-C', source, 'config', '--local', 'core.fsmonitor', str(hook)], check=True)
        with patch.dict(os.environ, {'WORKBENCH_HOOK_MARKER': str(marker)}):
            # Confirm this harmless fixture would execute if Git were invoked.
            subprocess.run(['git', '-C', source, 'status', '--porcelain'], check=True,
                            text=True, capture_output=True)
            self.assertTrue(marker.exists())
            marker.unlink()
            with patch.object(ci.cache_policy, 'git_read', side_effect=AssertionError('host Git used after receipt')), patch.object(ci.cache_policy, 'public_main', side_effect=AssertionError('GitHub used after receipt')):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    ci.cache_scope(self.args)
        self.assertRegex(stream.getvalue().strip(), r'^candidate-[0-9a-f]{64}$')
        self.assertFalse(marker.exists())

    def test_scope_receipt_from_another_run_or_source_is_rejected(self):
        self.repository()
        self.scope()
        for field in ('run_id', 'repository', 'ref', 'event', 'source_root'):
            with self.subTest(field=field):
                value = json.loads(self.args.receipt.read_text())
                changed = copy.deepcopy(value)
                changed[field] = 'foreign'
                self.args.receipt.write_text(json.dumps(changed))
                with patch.object(ci.cache_policy, 'git_read', side_effect=AssertionError('host command before rejection')):
                    with self.assertRaisesRegex(ValueError, 'foreign cache scope receipt'):
                        ci.cache_scope(self.args)
                self.args.receipt.write_text(json.dumps(value))


class CacheKeyTests(TemporaryFiles):
    def key(self, sources):
        def output(command, **kwargs):
            if 'native-cache.sh' in str(command[0]):
                environment = kwargs.get('env') or os.environ
                workspace = Path(environment['KUASAR_WORKSPACE_ROOT'])
                return ci.hashlib.sha256((workspace / 'guest-runtime/Makefile').read_bytes()).hexdigest()
            return 'fixed compiler version'

        stream = io.StringIO()
        with patch.dict(os.environ, {'KUASAR_WORKSPACE_ROOT': str(sources)}), patch.object(
                ci, 'output', side_effect=output), redirect_stdout(stream):
            ci.cache_key(argparse.Namespace(sources=sources))
        return stream.getvalue().strip()

    def test_exact_helper_dependency_changes_miss_the_actions_cache(self):
        for container in ('test-helpers', 'test-overlays'):
            with self.subTest(container=container):
                sources = self.root / container
                helper = sources / container / 'sandboxer'
                helper.mkdir(parents=True)
                lock = helper / 'go.sum'
                lock.write_text('first exact helper dependency\n')
                before = self.key(sources)
                lock.write_text('second exact helper dependency\n')
                self.assertNotEqual(before, self.key(sources))

    def test_separate_kernel_unit_recipe_changes_miss_the_actions_cache(self):
        sources = self.root / 'sources'
        for parent in (sources, sources / 'kernel-unit'):
            owner = parent / 'guest-runtime'
            owner.mkdir(parents=True)
            (owner / 'Makefile').write_text('first native recipe\n')
        before = self.key(sources)
        (sources / 'kernel-unit/guest-runtime/Makefile').write_text('changed exact kernel recipe\n')
        self.assertNotEqual(before, self.key(sources))

    def test_task_directory_and_transport_metadata_do_not_discard_reusable_inputs(self):
        keys = []
        for name in ('first-job', 'new-job'):
            sources = self.root / name
            owner = sources / 'test-helpers/sandboxer'
            owner.mkdir(parents=True)
            (owner / 'go.sum').write_text('same exact helper dependency\n')
            (sources / '.ci').mkdir()
            (sources / '.ci/plan.json').write_text(json.dumps({'run': name}))
            keys.append(self.key(sources))
        self.assertEqual(keys[0], keys[1])


class OutputBoundaryTests(TemporaryFiles):
    def test_declared_outputs_do_not_rewrite_unexported_build_source_links(self):
        root = self.receipt()
        sources = self.root / 'sources'
        (sources / 'sandboxer/release-bundle').mkdir(parents=True)
        (sources / 'sandboxer/release-bundle/package.tar').write_text('fixture package')
        (sources / 'sandboxer/build-tool-link').symlink_to(self.root / 'image-tool-fixture')
        with patch.object(ci, 'cleanup', return_value=0):
            ci.check_outputs(argparse.Namespace(root=root, outputs='sandboxer/release-bundle'))
        self.assertTrue((sources / 'sandboxer/build-tool-link').is_symlink())

    def test_an_exported_symlink_directory_cannot_hide_escaping_children(self):
        root = self.receipt()
        sources = self.root / 'sources'
        (sources / 'sandboxer').mkdir()
        (sources / 'actual-bundle').mkdir()
        (sources / 'sandboxer/release-bundle').symlink_to('../actual-bundle', target_is_directory=True)
        outside = self.root / 'outside-fixture'
        outside.write_text('not an exported file')
        (sources / 'actual-bundle/escape').symlink_to(outside)
        with patch.object(ci, 'cleanup', return_value=0):
            with self.assertRaises(ValueError):
                ci.check_outputs(argparse.Namespace(root=root, outputs='sandboxer/release-bundle'))

    def test_cache_parent_symlink_cannot_select_a_host_directory(self):
        root = self.receipt()
        (self.root / 'sources/result').write_text('fixture result')
        outside = self.root / 'outside-fixture/git'
        outside.mkdir(parents=True)
        (outside / 'fake-host-file').write_text('not a compiler cache')
        (root / 'instances/ci/home/.cargo').symlink_to(outside.parent, target_is_directory=True)
        with patch.object(ci, 'cleanup', return_value=0):
            with self.assertRaises(ValueError):
                ci.check_outputs(argparse.Namespace(root=root, outputs='result'))

    def test_explicit_output_contract_cannot_be_empty(self):
        root = self.receipt()
        with patch.object(ci, 'cleanup', return_value=0):
            with self.assertRaises(ValueError):
                ci.check_outputs(argparse.Namespace(root=root, outputs=''))

    def test_source_files_and_internal_links_are_allowed(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'file').write_text('ordinary build output')
        (source / 'link').symlink_to('file')
        ci.check_tree(source, source)

    def test_source_or_cache_link_cannot_target_a_host_file(self):
        source = self.root / 'source'
        source.mkdir()
        outside = self.root / 'outside-fixture'
        outside.write_text('fixture data must not become an upload')
        (source / 'link').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'escapes task'):
            ci.check_tree(source, source)

    def test_output_special_files_are_rejected_without_reading_them(self):
        source = self.root / 'source'
        source.mkdir()
        os.mkfifo(source / 'pipe')
        with self.assertRaisesRegex(ValueError, 'special file'):
            ci.check_tree(source, source)

    def test_diagnostics_copy_never_follows_links_or_special_files(self):
        outside = self.root / 'outside-fixture'
        outside.write_text('not a diagnostic')
        for kind in ('link', 'pipe'):
            with self.subTest(kind=kind):
                source = self.root / kind
                if kind == 'link':
                    source.symlink_to(outside)
                else:
                    os.mkfifo(source)
                destination = self.root / ('copy-' + kind)
                with self.assertRaisesRegex(ValueError, 'unsafe diagnostic file'):
                    ci.copy_evidence(source, destination)
                self.assertFalse(destination.exists())

    def test_diagnostic_regular_files_are_copied(self):
        source = self.root / 'source'
        (source / 'nested').mkdir(parents=True)
        (source / 'nested/log').write_bytes(b'bounded fixture output\n')
        destination = self.root / 'diagnostics'
        ci.copy_evidence(source, destination)
        self.assertEqual((destination / 'nested/log').read_bytes(), b'bounded fixture output\n')

    def test_output_checks_stop_execution_before_examining_candidate_files(self):
        root = self.receipt()
        args = argparse.Namespace(root=root)
        with patch.object(ci, 'cleanup', return_value=23), patch.object(ci, 'check_tree') as scan:
            with self.assertRaisesRegex(ValueError, 'failed to stop'):
                ci.check_outputs(args)
            scan.assert_not_called()
        with patch.object(ci, 'cleanup', return_value=0), patch.object(
                ci, 'check_tree', side_effect=ValueError('fixture unsafe output')):
            with self.assertRaisesRegex(ValueError, 'unsafe output'):
                ci.check_outputs(args)
        self.assertEqual(json.loads((root / 'receipt.json').read_text())['conclusion'], 'failure')


class ExecutionTests(TemporaryFiles):
    def execute(self, effect):
        root = self.receipt()
        args = argparse.Namespace(root=root, timeout=5, arguments=['--', 'bash', '-c', 'exit 42'])
        with patch.object(ci, 'framework_sha', return_value=FRAMEWORK), patch.object(
                ci.subprocess, 'run', side_effect=effect) as run:
            result = ci.execute(args)
        return result, json.loads((root / 'receipt.json').read_text()), run.call_args

    def test_exit_code_signal_and_timeout_are_not_changed_to_success(self):
        for result, expected in [(0, 0), (42, 42), (-15, 143), ('timeout', 124)]:
            with self.subTest(result=result), tempfile.TemporaryDirectory(dir=self.root) as directory:
                previous = self.root
                self.root = Path(directory)
                effect = (subprocess.TimeoutExpired('fixture', 5) if result == 'timeout'
                          else lambda *args, code=result, **kwargs: subprocess.CompletedProcess(args[0], code))
                code, receipt, _ = self.execute(effect)
                self.root = previous
                self.assertEqual(code, expected)
                self.assertEqual(receipt['commands'][-1]['exit_code'], expected)
                self.assertEqual(receipt['conclusion'], 'success' if expected == 0 else 'failure')

    def test_cancellation_preserves_a_failure_receipt(self):
        root = self.receipt()
        args = argparse.Namespace(root=root, timeout=5, arguments=['false'])
        with patch.object(ci, 'framework_sha', return_value=FRAMEWORK), patch.object(
                ci.subprocess, 'run', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                ci.execute(args)
        receipt = json.loads((root / 'receipt.json').read_text())
        self.assertEqual(receipt['conclusion'], 'failure')
        self.assertEqual(receipt['commands'][-1]['exit_code'], 130)

    def test_host_credentials_are_not_added_to_container_arguments(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'fake-private-publisher-token',
                                     'ACTIONS_RUNTIME_TOKEN': 'fake-private-artifact-token'}):
            _, _, call = self.execute(lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0))
        arguments = call.args[0]
        self.assertIn('GOTOOLCHAIN=local', arguments)
        self.assertIn('KUASAR_NATIVE_CACHE_ROOT=/build/native-cache', arguments)
        self.assertNotIn('fake-private-publisher-token', ' '.join(arguments))
        self.assertNotIn('fake-private-artifact-token', ' '.join(arguments))

    def test_changed_framework_refuses_to_start_candidate_command(self):
        root = self.receipt()
        args = argparse.Namespace(root=root, timeout=5, arguments=['false'])
        with patch.object(ci, 'framework_sha', return_value='f' * 40), patch.object(ci.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'framework changed'):
                ci.execute(args)
            run.assert_not_called()

    def test_real_interrupt_signals_record_failure_and_stop_the_owned_instance(self):
        script = '''import argparse, importlib.util, pathlib, signal, subprocess, sys
spec = importlib.util.spec_from_file_location("signal_fixture_subject", sys.argv[1])
ci = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ci
spec.loader.exec_module(ci)
root, ready, stopped = map(pathlib.Path, sys.argv[2:5])
ci.framework_sha = lambda: sys.argv[5]
def run(command, **kwargs):
    if "exec" in command:
        ready.write_text("waiting for a real signal")
        signal.pause()
        raise AssertionError("signal did not interrupt the command")
    if "cleanup" in command:
        stopped.write_text(str(root))
        return subprocess.CompletedProcess(command, 0)
    raise AssertionError("unexpected fixture command")
ci.subprocess.run = run
raise SystemExit(ci.execute(argparse.Namespace(root=root, timeout=30, arguments=["fixture-command"])))
'''
        parent = self.root
        for signum, expected in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=signum), tempfile.TemporaryDirectory(dir=parent) as directory:
                self.root = Path(directory)
                root = self.receipt()
                (root / 'instances/ci/instance.json').write_text('{}')
                ready, stopped = self.root / 'ready', self.root / 'stopped'
                process = subprocess.Popen([sys.executable, '-B', '-c', script,
                                            str(ROOT / 'ci/hosted/workbench.py'), str(root),
                                            str(ready), str(stopped), FRAMEWORK],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists(), 'signal fixture did not reach candidate execution')
                    process.send_signal(signum)
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, expected, stdout + stderr)
                    self.assertEqual(stopped.read_text(), str(root))
                    receipt = json.loads((root / 'receipt.json').read_text())
                    self.assertEqual(receipt['commands'][-1]['exit_code'], expected)
                    self.assertEqual(receipt['conclusion'], 'failure')
                    self.assertEqual(receipt['cleanup_exit_code'], 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=10)
                    self.root = parent


class CleanupTests(TemporaryFiles):
    def test_unsafe_diagnostics_fail_but_still_remove_only_owned_state(self):
        parent = self.root
        for kind in ('symlink', 'fifo'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(dir=parent) as directory:
                self.root = Path(directory)
                try:
                    root = self.receipt()
                    receipt = json.loads((root / 'receipt.json').read_text())
                    receipt['conclusion'] = 'success'
                    ci.write(root / 'receipt.json', receipt)
                    state = root / 'instances/ci'
                    (state / 'instance.json').write_text('{}')
                    (state / 'build/native-cache').mkdir()
                    (state / 'build/native-cache/owned').write_text('owned cached input')
                    unrelated = self.root / 'unrelated-cache'
                    unrelated.mkdir()
                    external = unrelated / 'not-a-diagnostic'
                    external.write_text('outside fixture bytes must not enter the upload')
                    unsafe = state / 'output/unsafe'
                    if kind == 'symlink':
                        unsafe.symlink_to(external)
                    else:
                        os.mkfifo(unsafe)
                    evidence = self.root / 'evidence'
                    with patch.object(ci.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
                        self.assertEqual(ci.finish(argparse.Namespace(root=root, evidence=evidence)), 1)
                    self.assertEqual(run.call_count, 2)
                    self.assertEqual(run.call_args.args[0], ci.command(root / 'instances', 'cleanup', '--delete-output'))
                    self.assertFalse(root.exists(), 'failed diagnostics left this action\'s cache/state behind')
                    self.assertTrue((self.root / 'sources').is_dir())
                    self.assertEqual(external.read_text(), 'outside fixture bytes must not enter the upload')
                    retained = json.loads((evidence / 'receipt.json').read_text())
                    self.assertEqual(retained['conclusion'], 'failure')
                    self.assertEqual(retained['cleanup_exit_code'], 0)
                    self.assertIn('unsafe diagnostic file', retained['diagnostics_error'])
                    self.assertFalse((evidence / 'output/unsafe').exists())
                    self.assertFalse((evidence / 'output/unsafe').is_symlink())
                    for path in evidence.rglob('*'):
                        self.assertFalse(path.is_symlink())
                        if path.is_file():
                            self.assertNotIn(external.read_bytes(), path.read_bytes())
                finally:
                    self.root = parent

    def test_foreign_receipt_cannot_authorize_deletion(self):
        root = self.receipt()
        value = json.loads((root / 'receipt.json').read_text())
        value['owner_uid'] = os.getuid() + 1
        ci.write(root / 'receipt.json', value)
        with self.assertRaisesRegex(ValueError, 'foreign task receipt'):
            ci.finish(argparse.Namespace(root=root, evidence=self.root / 'evidence'))
        self.assertTrue(root.exists())

    def test_linked_task_root_cannot_authorize_deletion(self):
        root = self.receipt()
        alias = self.root / 'linked-root'
        alias.symlink_to(root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'foreign or linked task root'):
            ci.finish(argparse.Namespace(root=alias, evidence=self.root / 'evidence'))
        self.assertTrue(root.exists())

    def test_launcher_cleanup_failure_preserves_the_owned_state(self):
        root = self.receipt()
        (root / 'instances/ci/instance.json').write_text('{}')
        (root / 'instances/ci/output/active-writer').write_text('untrusted while running')
        evidence = self.root / 'evidence'
        with patch.object(ci.subprocess, 'run', return_value=subprocess.CompletedProcess([], 17)) as run, \
             patch.object(ci, 'copy_evidence') as copy:
            self.assertEqual(ci.finish(argparse.Namespace(root=root, evidence=evidence)), 17)
        copy.assert_not_called()
        self.assertEqual({path.name for path in evidence.iterdir()}, {'receipt.json'})
        self.assertTrue(root.exists())
        self.assertEqual(run.call_count, 1)
        self.assertNotIn('--delete-output', run.call_args.args[0])
        self.assertEqual(json.loads((evidence / 'receipt.json').read_text())['cleanup_exit_code'], 17)

    def test_successful_cleanup_retains_receipt_and_deletes_only_its_state(self):
        root = self.receipt()
        (root / 'instances/ci/instance.json').write_text('{}')
        (root / 'instances/ci/output/log').write_text('fixture output')
        evidence = self.root / 'evidence'
        sibling = self.root / 'unrelated'
        sibling.mkdir()
        with patch.object(ci.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(ci.finish(argparse.Namespace(root=root, evidence=evidence)), 0)
        self.assertEqual(run.call_count, 2)
        self.assertIn('--delete-output', run.call_args.args[0])
        self.assertFalse(root.exists())
        self.assertTrue(sibling.exists())
        self.assertEqual((evidence / 'output/log').read_text(), 'fixture output')
        self.assertEqual(json.loads((evidence / 'receipt.json').read_text())['cleanup_exit_code'], 0)


class ActionTests(TemporaryFiles):
    def setUp(self):
        super().setUp()
        self.steps = yaml.safe_load((ROOT / '.github/actions/workbench/action.yml').read_text())['runs']['steps']

    def test_state_and_cache_paths_are_stable_but_evidence_is_unique(self):
        step = next(step for step in self.steps if step.get('id') == 'task')
        outputs = self.root / 'outputs'
        sources = self.root / 'sources'
        env = dict(os.environ, RUNNER_TEMP=str(self.root), WB_SOURCES=str(sources),
                   GITHUB_ACTION_PATH=str(ROOT / '.github/actions/workbench'), GITHUB_OUTPUT=str(outputs))
        records = []
        for _ in range(2):
            outputs.write_text('')
            subprocess.run(['bash', '-euo', 'pipefail', '-c', step['run']], env=env, check=True)
            records.append(dict(line.split('=', 1) for line in outputs.read_text().splitlines()))
        self.assertEqual(records[0]['root'], records[1]['root'])
        self.assertEqual(records[0]['scope'], records[1]['scope'])
        self.assertNotEqual(records[0]['evidence'], records[1]['evidence'])
        self.assertNotEqual(records[0]['identity'], records[1]['identity'])
        self.assertFalse(Path(records[0]['evidence']).is_relative_to(Path(records[0]['root'])))
        restore = next(step for step in self.steps if 'actions/cache/restore@' in step.get('uses', ''))
        save = next(step for step in self.steps if 'actions/cache/save@' in step.get('uses', ''))
        self.assertEqual(restore['with']['path'], save['with']['path'])
        self.assertTrue(all('${{ steps.task.outputs.root }}/instances/ci/' in path
                            for path in save['with']['path'].splitlines()))
        self.assertIn("inputs.cache == 'true'", save['if'])

    def test_candidate_command_is_passed_directly_not_written_into_sources(self):
        step = next(step for step in self.steps if 'WB_SCRIPT' in step.get('env', {}))
        self.assertEqual(step['env']['WB_SCRIPT'], '${{ inputs.run }}')
        self.assertIn('bash -euo pipefail -c "$WB_SCRIPT"', step['run'])
        self.assertFalse(any('GH_TOKEN' in step.get('env', {}) or 'GITHUB_TOKEN' in step.get('env', {})
                             for step in self.steps if step.get('id') != 'trust'))

    def test_cache_save_follows_output_check_and_cleanup_always_runs(self):
        check = next(index for index, step in enumerate(self.steps) if ' check-outputs ' in step.get('run', ''))
        save = next(index for index, step in enumerate(self.steps) if 'actions/cache/save@' in step.get('uses', ''))
        finish = next(step for step in self.steps if ' finish ' in step.get('run', ''))
        self.assertLess(check, save)
        self.assertTrue(finish['if'].startswith('always()'))


if __name__ == '__main__':
    unittest.main()
