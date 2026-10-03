"""Source harness regressions using an explicit public-runner fixture, no KVM."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / 'test/perf/sandbox-perf.sh'
FAKE = '''import argparse, json, os, pathlib, sys
p=argparse.ArgumentParser();p.add_argument('command')
for key in ('workdir','include','run-root','out-root','result'): p.add_argument('--'+key)
a=p.parse_args(); mode=os.environ.get('MOCK_PERF_MODE','')
if mode=='fail': print('explicit fixture failure');sys.exit(9)
out=pathlib.Path(a.out_root)/a.include;out.mkdir(parents=True)
name='lifecycle-stats.json' if a.include=='sandbox.lifecycle.sh' else 'stats.json'
if mode!='missing-stats': (out/name).write_text(json.dumps({'wallclock':{'duration_ms':2},'backends':[],'runtime':{},'uffd':{}}))
pathlib.Path(a.result).write_text(json.dumps({'conclusion':'failure' if mode=='bad-result' else 'success','cases':[a.include],'timings':[{'case':a.include,'wall_seconds':0.25,'exit_code':0}]}))
'''


class PerfEntryTests(unittest.TestCase):
    def run_fixture(self, mode='', scenarios='cold cold-manifest'):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);prepared=root/'prepared';runner=prepared/'test/e2e/e2e'
            runner.parent.mkdir(parents=True);runner.write_text(FAKE)
            (prepared/'provenance.json').write_text('{}')
            report=root/'report.txt'
            env=os.environ | {'E2E_WORKDIR':str(prepared),'PERF_OUT':str(report),'PERF_ITERS':'1',
                              'MOCK_PERF_MODE':mode,'PERF_SCENARIOS':scenarios,'TMPDIR':str(root)}
            result=subprocess.run(['bash',str(HARNESS)],env=env,text=True,capture_output=True)
            logs=list(root.glob('report.txt.samples.*/**/run.log'))
            return result,report.read_text() if report.exists() else '',len(logs)

    def test_current_cases_and_truthful_timing(self):
        result,report,logs=self.run_fixture()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('[file://] N=1',report)
        self.assertIn('[manifest://] N=1',report)
        self.assertIn('public case envelope',report)
        self.assertIn('case envelope:',report)
        self.assertEqual(logs,2)

    def test_failures_are_not_skipped_or_deleted(self):
        for mode in ('fail','missing-stats','bad-result'):
            with self.subTest(mode=mode):
                result,_,logs=self.run_fixture(mode)
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(logs,1)
                self.assertNotIn('failed; skipping',result.stdout)

    def test_invalid_selection_is_rejected(self):
        for scenarios in ('unknown','cold cold',' '):
            with self.subTest(scenarios=scenarios):
                result,_,logs=self.run_fixture(scenarios=scenarios)
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(logs,0)

    def test_make_uses_source_path_without_assembly_or_build(self):
        result=subprocess.run(['make','-n','perf-sandbox','E2E_WORKDIR=/prepared'],cwd=ROOT,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('bash test/perf/sandbox-perf.sh',result.stdout)
        self.assertNotIn('assemble.sh',result.stdout)
        self.assertNotIn('build/guest-native',result.stdout)

    def test_demo_missing_interpreter_fails_before_build(self):
        result=subprocess.run(['make','demo','PYTHON_BIN='],cwd=ROOT,text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('set PYTHON_BIN=',result.stdout+result.stderr)
        self.assertNotIn('build/guest-native',result.stdout)


if __name__=='__main__':unittest.main()
