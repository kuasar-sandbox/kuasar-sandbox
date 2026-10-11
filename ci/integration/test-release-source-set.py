"""Execute frozen owner Makefiles and native recipes, with pre-command rejection."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
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

    def cli_framework(self):
        # Execute the real CLI from its own clean, self-contained admitted root.
        # Only fixture declarations/Makefiles differ; imports cannot fall back to
        # the test driver's checkout or PYTHONPATH.
        framework = Path(__file__).resolve().parents[2]
        root = self.fixture.roots['platform']
        for directory in ('ci/integration', 'release', 'test/e2e'):
            for source in (framework / directory).rglob('*.py'):
                target = root / source.relative_to(framework)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        git(root, 'add', '.'); git(root, 'commit', '-qm', 'Frozen CLI framework')
        self.plan(['platform'])

    def exact_assets(self, preview=False):
        root = self.fixture.roots['platform']
        (root / 'releases').mkdir(exist_ok=True)
        units = {unit: row['tag'] for unit, row in self.document['source_records'].items()}
        version='release-v1.2.3'+('-preview.20990101' if preview else '')
        relative='releases/daily-preview.yaml' if preview else 'releases/release.yaml'
        (root / relative).write_text(
            'version: release-v1.2.3\ndelivery: workbench-v1\n' +
            ('preview_version: preview.20990101\n' if preview else '') + 'components:\n' +
            ''.join('  '+unit+': '+tag+'\n' for unit, tag in units.items()))
        git(root, 'add', '.'); git(root, 'commit', '-qm', 'Staged selection')
        self.plan(['platform'])
        plan = self.document
        platform_source = {key: plan['run_inputs']['platform'][key] for key in ('repository','sha','tree')}
        plan.update(schema=3, mode='exact-assets', platform_source=platform_source,
                    run_inputs={'platform': copy.deepcopy(platform_source), **{
                        'sources/'+unit: copy.deepcopy(row) for unit,row in plan['source_records'].items()}},
                    candidate_records=[], test_overlays=[], kernel_sha=plan['source_records']['vmlinux']['sha'])
        for row in plan['test_revisions'].values(): row['role']='release'
        plan['sources']=copy.deepcopy(plan['test_revisions'])
        plan['baseline']=dict(repository=platform_source['repository'], sha=platform_source['sha'],
            version=version, staged=True, delivery='workbench-v1', units={
                unit:dict(repository=row['repository'],sha=row['sha'],version=row['tag'])
                for unit,row in plan['source_records'].items()})
        names={'SHA256SUMS','platform-'+version+'.tar.gz'}
        names.update(artifacts.archive_name(unit,tag,arch) for unit,tag in units.items() for arch in artifacts.ARCHES)
        names.update('workbench-'+arch+'-'+version.removeprefix('release-')+'.tar.gz' for arch in artifacts.ARCHES)
        plan['baseline']['assets']=[dict(name=name,size=1,digest='sha256:'+'a'*64) for name in sorted(names)]
        for lane in plan['lanes'].values(): lane['performance']=[]
        facts={owner:platform_source if owner=='platform' else plan['source_records'][
            'runtime' if owner=='guest-runtime' else owner] for owner in artifacts.OWNERS}
        plan['helper_contract']=gate.helpers.discover(self.fixture.roots,facts)
        self.save()
        artifacts.check_plan(plan)

    def cli(self, auto=True, context=True):
        env = dict(self.env)
        for key in ('PYTHONPATH','GITHUB_ACTIONS','REQUIRE_WORKING_SET_PRIVILEGED','KUASAR_WORKSPACE_ROOT',
                    'KUASAR_WORKBENCH_IMAGE_ID','KUASAR_WORKBENCH_FRAMEWORK_SHA','KUASAR_CI_DIR'):
            env.pop(key,None)
        if context:
            env.update(REQUIRE_WORKING_SET_PRIVILEGED='1', KUASAR_WORKSPACE_ROOT=str(self.sources),
                       KUASAR_WORKBENCH_IMAGE_ID='fixture-image', KUASAR_WORKBENCH_FRAMEWORK_SHA='a'*40,
                       KUASAR_CI_DIR=str(self.root/'metrics'))
        command=[sys.executable,'-B',str(self.sources/'platform/ci/integration/release-source-set.py'),
                 '--sources',str(self.sources)]
        command += ['--auto'] if auto else ['--plan',str(self.plan_path),'--output',str(self.output)]
        completed=subprocess.run(command,env=env,cwd=self.sources/'platform',text=True,
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60)
        paths=sorted((self.root/'metrics').glob('release-gates-*/result.json')) if auto else [self.output/'result.json']
        result=json.loads(paths[-1].read_text()) if paths else None
        return completed,result

    def test_exact_assets_auto_and_explicit_cli_execute_native_recipe(self):
        self.cli_framework(); self.exact_assets()
        before=self.plan_path.read_bytes()
        for auto in (True,False):
            with self.subTest(auto=auto):
                completed,result=self.cli(auto)
                self.assertEqual(completed.returncode,0,(completed.stdout,completed.stderr,result))
                self.assertEqual(result['plan_id'],artifacts.identity(self.document))
                self.assertEqual(set(result['helpers']),set(artifacts.OWNERS))
                self.assertEqual(result['helpers']['connector']['status'],'validated-native-recipe')
                self.assertGreater(result['helpers']['connector']['outputs']['component-probe']['size'],64)
                self.assertEqual(len([row for row in result['commands'] if row['command']==['make','test-release']]),5)
                self.assertTrue(all(row['exit_code']==0 for row in result['commands']))
                self.assertEqual(self.plan_path.read_bytes(),before)
                self.assertNotIn('tag',self.document['platform_source'])
                self.assertNotEqual(self.document['platform_source']['sha'],self.document['framework_sha'])
        self.assertEqual(len((self.root/'executed').read_text().splitlines()),10)

    def test_exact_assets_preview_auto_cli(self):
        self.cli_framework();self.exact_assets(preview=True)
        completed,result=self.cli()
        self.assertEqual(completed.returncode,0,(completed.stdout,completed.stderr,result))
        self.assertEqual(result['plan_id'],artifacts.identity(self.document))
        self.assertEqual(result['helpers']['connector']['status'],'validated-native-recipe')

    def test_source_auto_cli_and_missing_context_documents(self):
        self.cli_framework()
        completed,result=self.cli()
        self.assertEqual(completed.returncode,0,(completed.stdout,completed.stderr,result))
        for missing in (['.source-plan.json'],['.source-checks.json'],['.source-plan.json','.source-checks.json']):
            with self.subTest(missing=missing):
                self.save()
                for name in missing: (self.sources/name).unlink()
                before=(self.root/'executed').read_bytes()
                completed,result=self.cli()
                self.assertNotEqual(completed.returncode,0)
                self.assertEqual(result['commands'],[])
                self.assertIn('missing regular frozen input',result['error'])
                self.assertEqual((self.root/'executed').read_bytes(),before)
        count=len(list((self.root/'metrics').glob('*')))
        completed,result=self.cli(context=False)
        self.assertEqual(completed.returncode,0,completed.stderr)
        self.assertIn('was not requested',completed.stdout)
        self.assertEqual(len(list((self.root/'metrics').glob('*'))),count)

    def test_exact_assets_platform_evidence_mismatch_before_commands(self):
        self.exact_assets(); original=copy.deepcopy(self.document)
        changes=[('platform_source','sha','b'*40),('platform_source','tree','b'*40),
                 ('platform_source','repository','kuasar-sandbox/other'),
                 ('baseline','sha','b'*40),('baseline','repository','kuasar-sandbox/other'),
                 ('baseline','version','release-v1.2.4'),('baseline','staged',False)]
        for group,key,value in changes:
            with self.subTest(group=group,key=key):
                self.document=copy.deepcopy(original);self.document[group][key]=value;self.save()
                self.output=self.root/(group+'-'+key)
                status,result=self.run_gate()
                self.assertNotEqual(status,0);self.assertEqual(result['commands'],[])
        for key,value in [('sha','b'*40),('tree','b'*40),('repository','kuasar-sandbox/other'),('ref','refs/heads/main')]:
            with self.subTest(run_input=key):
                self.document=copy.deepcopy(original);self.document['run_inputs']['platform'][key]=value;self.save()
                self.output=self.root/('run-input-'+key)
                status,result=self.run_gate()
                self.assertNotEqual(status,0);self.assertEqual(result['commands'],[])
        self.document=copy.deepcopy(original);self.document['test_revisions']['platform']['role']='baseline';self.save()
        self.output=self.root/'wrong-role';status,result=self.run_gate()
        self.assertNotEqual(status,0);self.assertEqual(result['commands'],[])
        self.document=original;self.save()
        receipt=json.loads((self.sources/'.source-checks.json').read_text())
        receipt['sources']['platform']['sha']='b'*40
        (self.sources/'.source-checks.json').write_text(json.dumps(receipt))
        self.output=self.root/'wrong-receipt';status,result=self.run_gate()
        self.assertNotEqual(status,0);self.assertEqual(result['commands'],[])
        self.assertFalse((self.root/'executed').exists())

    def test_exact_assets_consistent_false_tree_and_helper_source_rejected(self):
        self.exact_assets();original=copy.deepcopy(self.document)
        self.document['platform_source']['tree']='b'*40
        self.document['run_inputs']['platform']['tree']='b'*40
        self.document['helper_contract']['owners']['platform']['source']['tree']='b'*40
        self.save();status,result=self.run_gate()
        self.assertNotEqual(status,0);self.assertEqual(result['commands'],[])
        self.assertIn('tree changed',result['error'])
        self.document=original
        self.document['helper_contract']['owners']['platform']['source']['ref']='refs/heads/main'
        self.save();self.output=self.root/'helper-source'
        status,result=self.run_gate()
        self.assertNotEqual(status,0);self.assertEqual(result['commands'],[])

    def test_exact_assets_wrong_component_tag_at_same_commit(self):
        self.exact_assets()
        git(self.fixture.roots['connector'],'tag','v1.2.4')
        self.document['run_inputs']['sources/connector']['tag']='v1.2.4'
        self.document['helper_contract']['owners']['connector']['source']['tag']='v1.2.4'
        self.save();status,result=self.run_gate()
        self.assertNotEqual(status,0);self.assertEqual(result['commands'],[])
        self.assertFalse((self.root/'executed').exists())

    def test_exact_assets_manifest_binds_version_and_selected_tags(self):
        self.exact_assets();original=copy.deepcopy(self.document)
        self.document['baseline']['version']='release-v1.2.4'
        for row in self.document['baseline']['assets']:
            if row['name'].startswith(('platform-', 'workbench-')):
                row['name']=row['name'].replace('v1.2.3','v1.2.4')
        self.save();status,result=self.run_gate()
        self.assertNotEqual(status,0);self.assertEqual(result['commands'],[])
        self.assertIn('frozen platform manifest',result['error'])
        self.document=original
        git(self.fixture.roots['connector'],'tag','v1.2.4')
        self.document['baseline']['units']['connector']['version']='v1.2.4'
        for row in self.document['baseline']['assets']:
            if row['name'].startswith('connector-'):row['name']=row['name'].replace('v1.2.3','v1.2.4')
        for row in (self.document['source_records']['connector'],self.document['run_inputs']['sources/connector'],
                    self.document['helper_contract']['owners']['connector']['source']):row['tag']='v1.2.4'
        self.save();self.output=self.root/'wrong-manifest-tag'
        status,result=self.run_gate()
        self.assertNotEqual(status,0);self.assertEqual(result['commands'],[])
        self.assertIn('frozen platform manifest',result['error'])

    def test_unknown_mode_and_incomplete_exact_assets_fail_closed(self):
        self.exact_assets();original=copy.deepcopy(self.document)
        self.document['mode']='other';self.save()
        status,result=self.run_gate()
        self.assertNotEqual(status,0);self.assertEqual(result['commands'],[])
        for key in ('platform_source','run_inputs','helper_contract','source_records'):
            with self.subTest(key=key):
                self.document=copy.deepcopy(original);del self.document[key];self.save()
                self.output=self.root/('missing-'+key)
                status,result=self.run_gate()
                self.assertNotEqual(status,0);self.assertEqual(result['commands'],[])

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

    def test_failed_command_keeps_exit_when_it_also_dirties_sources(self):
        root=self.sources/'connector'
        (root/'Makefile').write_text('test-release:\n\t@touch unexpected-source; exit 23\n')
        git(root,'add','.');git(root,'commit','-qm','Fail and mutate input')
        self.plan(['connector'])
        status,result=self.run_gate()
        self.assertEqual(status,2,result)
        self.assertEqual(result['commands'][0]['exit_code'],2)
        self.assertIn('modified inputs',result['error'])
        self.assertTrue((root/'unexpected-source').exists())

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

    def test_explicit_python_syntax_check_keeps_source_clean(self):
        root=self.sources/'connector'
        (root/'syntax.py').write_text('value = 1\n')
        (root/'Makefile').write_text('test-release:\n\t@python3 -m py_compile syntax.py\n')
        git(root,'add','.');git(root,'commit','-qm','Original syntax gate')
        self.plan(['connector'])
        status,result=self.run_gate()
        self.assertEqual(status,0,result)
        self.assertFalse((root/'__pycache__').exists())
        self.assertEqual(git(root,'ls-files','--others'),'')

    def test_required_case_capability_cannot_be_omitted(self):
        self.document['case_files']['orchestrator']=['orchestrator.exec.sh']
        for arch,lane in self.document['lanes'].items():
            lane['selection']=artifacts.suite_selection(self.document['owners'],arch,self.document['case_files'])
        self.save()
        status,result=self.run_gate()
        self.assertNotEqual(status,0);self.assertIn('required helper',result['error'])
        self.assertEqual(result['commands'],[])

    def test_six_candidate_schema2_is_not_rewritten(self):
        self.plan(list(artifacts.OWNERS))
        before=self.plan_path.read_bytes()
        status,result=self.run_gate()
        self.assertEqual(status,0,result)
        self.assertEqual(self.plan_path.read_bytes(),before)
        self.assertNotIn('helper_contract',json.loads(before))
        self.assertEqual(result['plan_id'],artifacts.identity(json.loads(before)))

    def test_schema3_contract_and_incomplete_schemas(self):
        self.document['schema']=3
        facts=gate.identities(self.document,self.document['test_revisions'])
        self.document['helper_contract']=gate.helpers.discover(self.fixture.roots,facts)
        self.save()
        gate.validate(self.plan_path,self.sources,self.env)
        del self.document['helper_contract'];self.save()
        with self.assertRaisesRegex(ValueError,'helper contract'):gate.validate(self.plan_path,self.sources,self.env)
        self.document['schema']=99;self.save()
        with self.assertRaisesRegex(ValueError,'unsupported'):gate.validate(self.plan_path,self.sources,self.env)

    def test_linked_receipt_and_changed_closure_rejected(self):
        marker=self.sources/'.source-checks.json'
        original=marker.read_bytes();other=self.root/'receipt-copy';other.write_bytes(original)
        marker.unlink();marker.symlink_to(other)
        with self.assertRaisesRegex(ValueError,'regular frozen input'):gate.validate(self.plan_path,self.sources,self.env)
        marker.unlink();row=json.loads(original);del row['sources']['connector'];marker.write_text(json.dumps(row))
        with self.assertRaisesRegex(ValueError,'receipt'):gate.validate(self.plan_path,self.sources,self.env)

    def test_existing_evidence_is_not_overwritten(self):
        self.output.mkdir(); marker=self.output/'source-result.json';marker.write_text('original')
        with self.assertRaisesRegex(ValueError,'already exists'):self.run_gate()
        self.assertEqual(marker.read_text(),'original')

    def test_framework_is_separate_from_candidate_and_must_match(self):
        self.env['KUASAR_WORKBENCH_FRAMEWORK_SHA']='a'*40
        gate.validate(self.plan_path,self.sources,self.env)
        self.env['KUASAR_WORKBENCH_FRAMEWORK_SHA']=self.document['test_revisions']['platform']['sha']
        with self.assertRaisesRegex(ValueError,'framework'):gate.validate(self.plan_path,self.sources,self.env)


if __name__ == '__main__': unittest.main()
