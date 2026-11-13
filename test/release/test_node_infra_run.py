#!/usr/bin/env python3
"""Exercise the Quick Start client's process, cancellation and ownership boundaries."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest import mock


REAL_POPEN = subprocess.Popen
SCRIPT = Path(__file__).resolve().parents[2] / 'node-infra/runtime/node-infra-run'
loader = importlib.machinery.SourceFileLoader('node_infra_run', str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


class QuickStartRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = Path(self.temp.name)
        self.patch('STATE', state)
        (state / 'api-key').write_text('test-key')
        (state / 'template-id').write_text('local-template')
        self.patch('initialized', lambda: True)
        self.patch('has_capacity', lambda: True)
        self.native_health = cli.proxy_ready
        self.patch('proxy_ready', lambda: True)
        self.calls = []
        self.interrupt_create = False
        self.fail_session = False
        self.lose_reply = False
        self.owned = False
        self.sid = ''
        self.marker = ''
        self.patch('api', self.api)
        for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))

    def patch(self, name, value):
        patcher = mock.patch.object(cli, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def api(self, key, method, path, expected, body=None):
        self.assertEqual(key, 'test-key')
        self.calls.append((method, path))
        if (method, path) == ('POST', '/sandboxes'):
            self.assertEqual(body['templateID'], 'local-template')
            self.sid = json.loads(body['metadata']['kuasar-sandbox.identity'])['id']
            self.marker = body['metadata']['node-infra-run']
            if self.lose_reply:
                raise cli.RunError('control connection lost')
            if self.interrupt_create:
                os.kill(os.getpid(), signal.SIGTERM)
            return {'sandboxID': self.sid}
        if path.endswith('/exec-sessions'):
            if self.fail_session:
                raise cli.RunError('session unavailable')
            return {'execAccessToken': 'test-session'}
        return {'state': 'running', 'metadata': {'node-infra-run': self.marker if self.owned else 'foreign'}}

    def test_interrupt_during_create_still_cleans_up_accepted_guest(self):
        self.interrupt_create = True
        with self.assertRaises(SystemExit) as result:
            cli.run(['true'])
        self.assertEqual(result.exception.code, 128 + signal.SIGTERM)
        self.assertEqual(self.calls, [('POST', '/sandboxes'), ('DELETE', '/sandboxes/' + self.sid)])

    def test_exec_session_failure_cleans_up_without_retrying(self):
        self.fail_session = True
        with self.assertRaisesRegex(cli.RunError, 'session unavailable'):
            cli.run(['true'])
        self.assertEqual(self.calls, [('POST', '/sandboxes'), ('GET', '/sandboxes/' + self.sid),
                                      ('POST', '/sandboxes/' + self.sid + '/exec-sessions'),
                                      ('DELETE', '/sandboxes/' + self.sid)])

    def test_delayed_native_proxy_readiness_precedes_a_single_create(self):
        self.patch('proxy_ready', self.native_health)
        probe = ['/usr/libexec/node-infra/node-infra-runtime', 'health']
        statuses = iter((1, 0))

        def health(command, **kwargs):
            self.assertEqual(command, probe)
            self.assertEqual(self.calls, [])
            return subprocess.CompletedProcess(command, next(statuses))

        with mock.patch.object(subprocess, 'run', side_effect=health) as check, \
                mock.patch.object(subprocess, 'Popen', side_effect=self.exec_client):
            self.assertEqual(cli.run(['/bin/true']), 0)
        self.assertEqual(check.call_count, 2)
        self.assertEqual(self.calls.count(('POST', '/sandboxes')), 1)

    def test_proxy_readiness_timeout_leaves_no_guest(self):
        self.patch('proxy_ready', self.native_health)
        self.patch('START_TIMEOUT', 0)
        with mock.patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('health', 10)):
            with self.assertRaisesRegex(cli.RunError, 'timed out waiting for Proxy readiness'):
                cli.run(['true'])
        self.assertEqual(self.calls, [])

    def test_lost_create_reply_reconciles_only_owned_guest(self):
        self.lose_reply = True
        for self.owned in (True, False):
            with self.subTest(owned=self.owned):
                self.calls.clear()
                with self.assertRaisesRegex(cli.RunError, 'could not confirm creation'):
                    cli.run(['true'])
                path = '/sandboxes/' + self.sid
                self.assertEqual(self.calls, [('POST', '/sandboxes'), ('GET', path)]
                                 + ([('DELETE', path)] if self.owned else []))

    def test_native_client_receives_literal_argv_and_returns_guest_status(self):
        argv = ['/bin/sh', '-c', 'exit 7', 'one two', '*', '$HOME']
        with mock.patch.object(subprocess, 'Popen', wraps=self.exec_client) as client:
            self.assertEqual(cli.run(argv), 7)
        command = client.call_args.args[0]
        self.assertEqual(command[command.index('--') + 1:], argv)
        self.assertIn('X-Access-Token: test-session', command)
        self.assertEqual(self.calls[-1], ('DELETE', '/sandboxes/' + self.sid))

    @staticmethod
    def exec_client(command):
        return REAL_POPEN(command[command.index('--') + 1:])


if __name__ == '__main__':
    unittest.main()
