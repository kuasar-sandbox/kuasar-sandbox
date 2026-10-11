"""Execute frozen owner Makefiles and native recipes, with pre-command rejection."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import unittest

import artifacts
from test_fixtures import CASES, select_plan

spec = importlib.util.spec_from_file_location('release_source_set', Path(__file__).with_name('release-source-set.py'))
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'release'))
import test_helper_discovery as discovery
git = discovery.git


class FrozenReleaseGates(unittest.TestCase):
    def setUp(self):
        self.fixture = discovery.OwnerDiscovery()
        # Reuse only its clean real Git/ELF fixture setup, not a mocked producer.
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.sources = self.root / 'sources'
        self.output = self.root / 'evidence'
        self.plan_path = self.sources / '.source-plan.json'
        self.env = {k: v for k,v in os.environ.items() if k not in (
            'GH_TOKEN','GITHUB_TOKEN','CALLER_TOKEN','KUASAR_CI_APP_PRIVATE_KEY',
            'KUASAR_WORKBENCH_FRAMEWORK_SHA','KUASAR_WORKSPACE_ROOT','GOWORK')}
        self.env.update(TARGET_ARCH=platform.machine(), EXECUTED=str(self.root / 'executed'))
        for owner, root in self.fixture.roots.items():
            (root / 'Makefile').write_text('test-release:\n\t@echo '+owner+' >> "$${EXECUTED}"\n')
            git(root, 'add', '.'); git(root, 'commit', '-qm', 'Owner release entry')
            git(root, 'tag', ('runtime-' if owner == 'guest-runtime' else '') + 'v1.2.3')
        self.plan(['platform'])

    def plan(self, owners):
        facts, pins, candidates, released = {}, {}, [], {}
        for owner, root in self.fixture.roots.items():
            repository = 'kuasar-sandbox/' + ('kuasar-sandbox' if owner == 'platform' else owner)
            row = dict(repository=repository, sha=git(root,'rev-parse','HEAD'), tree=git(root,'rev-parse','HEAD^{tree}'))
            role = 'candidate' if owner in owners else 'baseline'
            if owner in owners:
                candidates.append(dict(repository=repository, candidate_sha=row['sha'], pull_request_number=1))
                row['ref'] = 'refs/pull/1/merge'
            elif owner == 'platform': row['ref'] = 'refs/heads/main'
            else: row['tag'] = ('runtime-' if owner == 'guest-runtime' else '') + 'v1.2.3'
            facts[owner] = row
            pins[owner] = dict(repository=repository, sha=row['sha'], role=role)
            if owner != 'platform':
                released['runtime' if owner == 'guest-runtime' else owner] = dict(row, tag=('runtime-' if owner == 'guest-runtime' else '')+'v1.2.3')
                released['runtime' if owner == 'guest-runtime' else owner].pop('ref',None)
        released['vmlinux'] = dict(released['runtime'], tag='vmlinux-v1.2.3')
        self.document = dict(schema=2, mode='source', framework_sha='a'*40, baseline={}, case_files={owner: ['image.fixture-'+owner+'.sh'] for owner in artifacts.OWNERS},
            test_overlays=list(artifacts.OWNERS), test_revisions=pins, run_inputs=facts,
            candidate_records=candidates, source_records=released,
            lanes={arch:dict(products=[]) for arch in artifacts.ARCHES})
        select_plan(self.document, owners)
        for arch, lane in self.document['lanes'].items():
            lane['selection']=artifacts.suite_selection(owners,arch,self.document['case_files'])
        self.save()

    def save(self):
        self.plan_path.write_bytes(artifacts.canonical(self.document))
        _, records = gate.source_checks.source_records(self.document)
        (self.sources / '.source-checks.json').write_bytes(artifacts.canonical(
            dict(plan_id=artifacts.identity(self.document), sources=records)))

    def run_gate(self):
        status = gate.execute(self.plan_path, self.sources, self.output, self.env)
        result = json.loads((self.output / 'result.json').read_text())
        return status, result

    def test_all_owners_original_entries_and_real_native_recipe(self):
        before = self.plan_path.read_bytes()
        status, result = self.run_gate()
        self.assertEqual(status, 0, result)
        self.assertEqual(self.plan_path.read_bytes(), before)
        self.assertEqual(set((self.root/'executed').read_text().splitlines()), set(artifacts.OWNERS)-{'platform'})
        self.assertGreater(result['helpers']['connector']['outputs']['component-probe']['size'], 64)
        self.assertEqual(result['helpers']['connector']['status'], 'validated-native-recipe')
        self.assertTrue(all(row['exit_code']==0 for row in result['commands']))

    def test_selected_owner_only_and_existing_context_detection(self):
        self.plan(['connector'])
        status, result = self.run_gate()
        self.assertEqual(status, 0, result)
        self.assertEqual((self.root/'executed').read_text(), 'connector\n')
        self.assertEqual(set(result['helpers']), {'connector'})
        self.assertFalse(gate.requested(self.root/'absent', {}))
        for key in ['REQUIRE_WORKING_SET_PRIVILEGED','KUASAR_WORKSPACE_ROOT','KUASAR_WORKBENCH_IMAGE_ID','KUASAR_CI_DIR']:
            self.assertTrue(gate.requested(self.root/'absent', {key:'present'}))
        self.assertTrue(gate.requested(self.sources, {}))

    def test_missing_receipt_rejected_before_any_command(self):
        (self.sources/'.source-checks.json').unlink()
        status, result = self.run_gate()
        self.assertNotEqual(status,0); self.assertEqual(result['commands'],[])
        self.assertFalse((self.root/'executed').exists())

    def test_plan_receipt_mismatch_rejected_before_any_command(self):
        self.document['framework_sha']='b'*40
        self.plan_path.write_bytes(artifacts.canonical(self.document))
        status,result=self.run_gate()
        self.assertNotEqual(status,0); self.assertIn('receipt',result['error'])
        self.assertFalse((self.root/'executed').exists())

    def test_dirty_source_rejected_before_any_command(self):
        (self.sources/'orchestrator/untracked').write_text('dirty')
        status,result=self.run_gate()
        self.assertNotEqual(status,0); self.assertEqual(result['commands'],[])

    def test_source_tree_and_ref_mismatch_rejected(self):
        for field,value in [('tree','0'*40), ('ref','refs/pull/99/merge')]:
            with self.subTest(field=field):
                self.document['run_inputs']['platform'][field]=value; self.save()
                with self.assertRaises(ValueError): gate.validate(self.plan_path,self.sources,self.env)
                self.plan(['platform'])

    def test_same_commit_wrong_selected_tag_rejected(self):
        self.plan(['connector'])
        self.document['run_inputs']['accelerator']['tag']='v9.9.9'
        self.plan(['platform'])
        self.document['run_inputs']['accelerator']['tag']='v9.9.9'; self.save()
        status,result=self.run_gate()
        self.assertNotEqual(status,0); self.assertEqual(result['commands'],[])

    def test_original_make_exit_stops_later_owners(self):
        root=self.sources/'accelerator'
        (root/'Makefile').write_text('test-release:\n\t@exit 23\n')
        git(root,'add','.');git(root,'commit','-qm','Fail original gate')
        self.plan(['platform','accelerator'])
        status,result=self.run_gate()
        self.assertEqual(status,2,result)  # GNU make preserves its own recipe-failure status.
        self.assertEqual(len(result['commands']),1)
        self.assertIn('Error 23',(self.output/'00.log').read_text())
        self.assertFalse((self.root/'executed').exists())

    def test_missing_current_declaration_is_not_history(self):
        root=self.sources/'connector'; (root/'release/helper-inputs.json').unlink()
        git(root,'add','-A');git(root,'commit','-qm','Remove declaration')
        self.plan(['platform','connector'])
        status,result=self.run_gate()
        self.assertNotEqual(status,0);self.assertEqual(result['commands'],[])

    def test_extra_recipe_output_and_source_mutation_fail(self):
        for extra in ['touch "$2/undeclared"\n', 'touch unexpected-source\n']:
            with self.subTest(extra=extra):
                root=self.sources/'connector'
                recipe=root/'release/build-helpers.sh'
                recipe.write_text('set -eu\ncc -o "$2/component-probe" release/helper.c\n'+extra)
                git(root,'add','.');git(root,'commit','-qm','Invalid recipe')
                self.plan(['connector']); self.output=self.root/('evidence'+str(len(extra)))
                status,result=self.run_gate()
                self.assertNotEqual(status,0,result)

    def test_missing_both_documents_in_ci_does_not_skip(self):
        self.plan_path.unlink(); (self.sources/'.source-checks.json').unlink()
        self.env['REQUIRE_WORKING_SET_PRIVILEGED']='1'
        self.assertTrue(gate.requested(self.sources,self.env))
        status,result=self.run_gate()
        self.assertNotEqual(status,0);self.assertEqual(result['commands'],[])

    def test_missing_original_target_and_dependency_fail_before_make(self):
        root=self.sources/'connector'
        for missing in ('target','dependency'):
            with self.subTest(missing=missing):
                if missing=='target': (root/'Makefile').write_text('unrelated:\n\t@true\n')
                else:
                    (root/'Makefile').write_text('test-release:\n\t@true\n')
                    path=root/'release/helper-inputs.json'; row=json.loads(path.read_text())
                    row['dependencies']=['guest-runtime']; path.write_text(json.dumps(row))
                git(root,'add','.');git(root,'commit','-qm','Missing required release input')
                self.plan(['connector']);self.output=self.root/missing
                status,result=self.run_gate()
                self.assertNotEqual(status,0);self.assertEqual(result['commands'],[])

    def test_workspace_and_receipt_mutation_are_rejected_after_command(self):
        root=self.sources/'connector'
        for target in ('go.work','.source-checks.json'):
            with self.subTest(target=target):
                (root/'Makefile').write_text('test-release:\n\t@echo changed > ../'+target+'\n')
                git(root,'add','.');git(root,'commit','-qm','Mutate fixed evidence')
                self.plan(['connector']);self.output=self.root/('changed-'+target)
                status,result=self.run_gate()
                self.assertNotEqual(status,0);self.assertEqual(len(result['commands']),1)
                self.assertEqual(result['commands'][0]['exit_code'],0)
                self.assertEqual((self.sources/target).read_text(),'changed\n')
                if target=='go.work': (self.sources/target).unlink()

    def test_required_case_capability_cannot_be_omitted(self):
        self.document['case_files']['orchestrator']=['orchestrator.exec.sh']
        for arch,lane in self.document['lanes'].items():
            lane['selection']=artifacts.suite_selection(self.document['owners'],arch,self.document['case_files'])
        self.save()
        status,result=self.run_gate()
        self.assertNotEqual(status,0);self.assertIn('required helper',result['error'])
        self.assertEqual(result['commands'],[])

    def test_framework_is_separate_from_candidate_and_must_match(self):
        self.env['KUASAR_WORKBENCH_FRAMEWORK_SHA']='a'*40
        gate.validate(self.plan_path,self.sources,self.env)
        self.env['KUASAR_WORKBENCH_FRAMEWORK_SHA']=self.document['test_revisions']['platform']['sha']
        with self.assertRaisesRegex(ValueError,'framework'):gate.validate(self.plan_path,self.sources,self.env)


if __name__ == '__main__': unittest.main()
