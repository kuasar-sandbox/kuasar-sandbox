#!/usr/bin/env python3
"""Check exact source handoff, native helpers and reproducible Go helper builds."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import struct
import shutil
import go_workspace
import tempfile
import unittest
from unittest.mock import patch

import artifacts
from test_fixtures import restore_using, helper_contract
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "release"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'test/release'))
from test_tag_sources import fixture

ROOT = Path(__file__).resolve().parents[2]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("workbench_delta", "ci/integration/build-artifacts.py")
release_helpers = load("workbench_release_helpers", "release/build-e2e-helpers.py")


def declare(root, owner):
    recipe = 'release/helper.sh' if owner == 'orchestrator' else None
    declaration = {'schema': 1, 'recipe': recipe, 'dependencies': [], 'helpers':
                   [{'name': 'custom-proxy', 'env': 'CUSTOM_PROXY_BIN', 'cases': ['orchestrator.*.sh']}] if recipe else []}
    (root / 'release/helper-inputs.json').write_text(json.dumps(declaration))
    if recipe: (root / recipe).write_text('go build -o "$2/custom-proxy" ./examples/custom-proxy\n')


def platform_fixture(root):
    fixture(root, 'connector', 'v0.0.1')
    git = release_helpers.tag_sources.git
    git(root, 'remote', 'set-url', 'origin', 'https://github.com/kuasar-sandbox/kuasar-sandbox')
    declare(root, 'platform')
    git(root, 'add', '.'); git(root, 'commit', '-qm', 'platform helper declaration')



class WorkbenchBuild(unittest.TestCase):
    def test_release_helper_private_copy_paths_do_not_change_go_binary_bytes(self):
        selected = {unit: ('runtime-v1.2.3' if unit == 'runtime' else 'vmlinux-v1.2.3' if unit == 'vmlinux' else 'v1.2.3')
                    for unit in release_helpers.selection.UNITS}
        arch = os.uname().machine
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = root / "sources"
            for unit, tag in selected.items():
                module = release_helpers.tag_sources.source_path(sources, unit, owner_layout=True)
                fixture(module, unit, tag)
                (module / "go.mod").write_text("module example.test/" + unit + "\ngo 1.22\n")
            proxy = sources / "orchestrator/examples/custom-proxy"
            proxy.mkdir(parents=True)
            (proxy / "main.go").write_text('package main\nimport ("fmt"; "runtime")\n'
                                         'func main() { _, file, _, _ := runtime.Caller(0); fmt.Println(file) }\n')
            for unit, tag in selected.items():
                module = release_helpers.tag_sources.source_path(sources, unit, owner_layout=True)
                declare(module, unit)
                release_helpers.tag_sources.git(module, 'add', '.')
                release_helpers.tag_sources.git(module, 'commit', '-qm', 'helper input')
                release_helpers.tag_sources.git(module, 'tag', '-f', tag)
            platform_fixture(root / "platform")
            copied = []
            def helpers(source, target, destination, selected, environment, contract):
                copied.append(source)
                self.assertIn("-p=2", environment["GOFLAGS"].split())
                destination.mkdir(parents=True)
                # Same owner command shape which omitted -trimpath in the
                # pinned custom-proxy recipe; the producer supplies the flag.
                subprocess.run(["go", "-C", str(source / "orchestrator"), "build",
                                "-o", str(destination / "custom-proxy"), "./examples/custom-proxy"],
                               env={**environment, "GOWORK": "off", "CGO_ENABLED": "0"}, check=True)
                for name in set(selected) - {"custom-proxy"}:
                    (destination / name).write_bytes(b"unrelated helper fixture")
            with patch.dict(os.environ, {"GOFLAGS": "-p=2", "GOPROXY": "off", "GOSUMDB": "off"}), \
                    patch.object(release_helpers.build_demo_wheels, "build"), \
                    patch.object(release_helpers.build_helpers, "build", side_effect=helpers):
                for phase in ("cold", "warm"):
                    release_helpers.build(sources, selected, root / phase, root / (phase + "-wheels"), root / "platform", arch)
            self.assertNotEqual(copied[0], copied[1])
            self.assertTrue(all(not source.exists() for source in copied))
            self.assertEqual(artifacts.digest(root / "cold" / arch / "custom-proxy"),
                             artifacts.digest(root / "warm" / arch / "custom-proxy"))
            self.assertEqual((root / "cold" / arch / "helpers.json").read_bytes(),
                             (root / "warm" / arch / "helpers.json").read_bytes())

    def plan(self):
        revisions = artifacts.release_test_revisions(
            {owner: "a" * 40 for owner in artifacts.OWNERS if owner != "platform"}, "b" * 40)
        tests = copy.deepcopy(revisions)
        tests["orchestrator"]["sha"] = "c" * 40
        contract = helper_contract(sha='a' * 40, framework='b' * 40)
        contract['owners']['orchestrator']['source']['sha'] = 'c' * 40
        return {"mode": "source", "helper_contract": contract, "sources": revisions, "test_revisions": tests,
                "test_overlays": list(artifacts.OWNERS),
                "product_sources": {"node-ctl": {"kuasar-sandbox/orchestrator": "a" * 40}},
                "lanes": {"x86_64": {"products": [], "selection": {"cases": []}}}}

    def test_retired_mkfs_build_fails_before_sources_or_compilers_run(self):
        plan = self.plan()
        plan["framework_sha"] = "a" * 40
        plan["lanes"]["x86_64"]["products"] = ["mkfs.erofs"]
        with tempfile.TemporaryDirectory() as temporary, patch.object(builder.artifacts, "check_plan"), \
             patch.object(builder.platform, "machine", return_value="x86_64"), \
             patch.object(builder.subprocess, "check_output", return_value="a" * 40 + "\n"), \
             patch.object(builder, "materialize") as materialize, \
             patch.object(builder, "run") as run, \
             patch.dict(os.environ, {name: "" for name in ("GH_TOKEN", "GITHUB_TOKEN", "CALLER_TOKEN", "KUASAR_CI_APP_PRIVATE_KEY")}):
            with self.assertRaisesRegex(ValueError, "native mkfs builds are retired"):
                builder.build(plan, "x86_64", Path("assets"),
                              Path("sources"), Path(temporary) / "output")
            materialize.assert_not_called()
            run.assert_not_called()

    def test_host_fetches_exact_product_test_and_helper_sources_without_go(self):
        plan = self.plan()
        with tempfile.TemporaryDirectory() as temporary:
            sources = Path(temporary) / "sources"
            fetched = {}
            def checkout(repository, sha, destination):
                destination.mkdir()
                (destination / "go.mod").write_text("module fixture\n\ngo 1.26.1\n")
                fetched[str(destination.relative_to(sources))] = sha
            with patch.object(builder.artifacts, "planned_helpers", return_value={"custom-proxy": "orchestrator"}), \
                 patch.object(builder.source_inputs, "copy_run_inputs", side_effect=restore_using(checkout)), patch.object(builder, "run") as run:
                builder.materialize(plan, "x86_64", sources)
                run.assert_not_called()
                self.assertEqual(fetched["orchestrator"], "a" * 40)
                self.assertEqual(fetched["test-overlays/orchestrator"], "c" * 40)
                self.assertEqual(fetched["test-helpers/orchestrator"], "c" * 40)
                self.assertEqual(builder.helper_sources(plan, "x86_64", sources), sources / "test-helpers")
                self.assertEqual(builder.test_source(plan, "x86_64", sources, "orchestrator"), sources / "test-overlays/orchestrator")
                self.assertFalse((sources / ".ci/go-workspaces").exists())

    def test_changed_materialized_revision_is_rejected_before_compilation(self):
        plan = self.plan()
        with patch.object(builder.artifacts, "planned_helpers", return_value={}), \
             patch.object(builder.subprocess, "check_output", return_value="f" * 40 + "\n"), \
             patch.object(builder, "run") as run:
            with self.assertRaisesRegex(ValueError, "materialized source identity changed"):
                builder.verify_materialized(plan, "x86_64", Path("/private/sources"))
            run.assert_not_called()

    def test_materialized_checkout_rejects_dirty_and_untracked_compiler_inputs(self):
        for mutation in ("clean", "modified", "staged", "untracked"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "connector"
                subprocess.run(["git", "init", "--quiet", "--template=", str(source)], check=True)
                for key, value in (("user.name", "Chen Xiaohui"), ("user.email", "graych@gmail.com")):
                    subprocess.run(["git", "-C", source, "config", "--local", key, value], check=True)
                (source / "input.go").write_text("package fixture\n")
                subprocess.run(["git", "-C", source, "add", "input.go"], check=True)
                subprocess.run(["git", "-C", source, "commit", "--quiet", "-m", "fixture"], check=True)
                revision = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True).strip()
                if mutation in ("modified", "staged"):
                    (source / "input.go").write_text("package changed\n")
                    if mutation == "staged":
                        subprocess.run(["git", "-C", source, "add", "input.go"], check=True)
                elif mutation == "untracked":
                    (source / "extra.go").write_text("package fixture\n")
                with patch.object(builder, "source_layout", return_value={"connector": {"sha": revision}}):
                    if mutation == "clean":
                        builder.verify_materialized({}, "x86_64", root)
                    else:
                        with self.assertRaisesRegex(ValueError, "materialized source has"):
                            builder.verify_materialized({}, "x86_64", root)

    def test_cross_architecture_is_rejected_before_checkout_or_outputs(self):
        with patch.object(builder.artifacts, "check_plan"), patch.object(builder.platform, "machine", return_value="x86_64"), \
             patch.object(builder, "materialize") as materialize:
            with self.assertRaisesRegex(ValueError, "selected native architecture"):
                builder.build({}, "aarch64", Path("assets"), Path("sources"), Path("output"))
            materialize.assert_not_called()

    def test_framework_mismatch_is_rejected_before_checkout_or_toolchain_selection(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(builder.artifacts, "check_plan"), \
             patch.object(builder.platform, "machine", return_value="x86_64"), \
             patch.object(builder.subprocess, "check_output", return_value="f" * 40 + "\n"), \
             patch.object(builder, "materialize") as materialize, patch.object(builder, "configure_workspace") as configure, \
             patch.dict(os.environ, {name: "" for name in ("GH_TOKEN", "GITHUB_TOKEN", "CALLER_TOKEN", "KUASAR_CI_APP_PRIVATE_KEY")}):
            with self.assertRaisesRegex(ValueError, "framework checkout differs"):
                builder.build({"framework_sha": "a" * 40}, "x86_64", Path("assets"), Path("sources"), Path(temporary) / "output")
            materialize.assert_not_called()
            configure.assert_not_called()

    def test_release_helper_job_builds_only_native_helpers_from_selected_tags(self):
        from test_tag_sources import fixture
        selected = {unit: ('runtime-' if unit == 'runtime' else 'vmlinux-' if unit == 'vmlinux' else '') + 'v1.2.3'
                    for unit in release_helpers.selection.UNITS}
        framework = subprocess.check_output(['git', '-C', ROOT, 'rev-parse', 'HEAD'], text=True).strip()
        for arch in artifacts.ARCHES:
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                sources = work / "sources"
                for unit, tag in selected.items():
                    source = release_helpers.tag_sources.source_path(sources, unit, owner_layout=True)
                    fixture(source, unit, tag)
                    declare(source, unit)
                    git = release_helpers.tag_sources.git
                    git(source, 'add', '.'); git(source, 'commit', '-qm', 'declared helpers'); git(source, 'tag', '-f', tag)
                platform_fixture(work / 'platform')
                facts = release_helpers.tag_sources.inspect_sources(sources, selected, owner_layout=True)
                pins = release_helpers.tag_sources.owner_revisions(facts)
                calls, built_names = [], set()
                def helpers(source, target, destination, chosen, environment, contract):
                    calls.append(target)
                    built_names.update(chosen)
                    self.assertEqual(set(p.name for p in source.iterdir()), set(pins) | {'platform'})
                    for owner, unit in release_helpers.tag_sources.OWNER_UNITS.items():
                        expected = release_helpers.tag_sources.source_path(sources, unit, owner_layout=True)
                        self.assertEqual((source / owner / 'README.md').read_bytes(), (expected / 'README.md').read_bytes())
                    destination.mkdir(parents=True)
                    for name in chosen:
                        data = bytearray(64); data[:7] = b'\x7fELF\x02\x01\x01'
                        struct.pack_into('<H', data, 18, 62 if arch == 'x86_64' else 183)
                        (destination / name).write_bytes(data); (destination / name).chmod(0o755)
                real_run = subprocess.run
                def run(command, **kwargs):
                    if command[:3] == ['go', 'work', 'init']:
                        return subprocess.CompletedProcess(command, 0)
                    return real_run(command, **kwargs)
                with patch.object(release_helpers.platform, "machine", return_value=arch), \
                     patch.object(release_helpers.subprocess, "run", side_effect=run), \
                     patch.object(release_helpers.build_demo_wheels, "build") as wheels, \
                     patch.object(release_helpers.build_helpers, "build", side_effect=helpers):
                    release_helpers.build(sources, selected, work / "helpers", work / "wheels", work / "platform", arch)
                self.assertEqual(calls, [arch])
                self.assertEqual(wheels.call_args.args[1], arch)
                self.assertEqual({path.name for path in (work / "helpers").iterdir()}, {arch})
                record = json.loads((work / "helpers" / arch / "helpers.json").read_text())
                self.assertEqual(set(record), {'schema', 'arch', 'helpers'})
                self.assertEqual(record['schema'], 2)
                self.assertEqual(set(record['helpers']), built_names)
                self.assertTrue(all(set(row) == {'owner', 'env', 'cases', 'size', 'sha256', 'mode'}
                                    for row in record['helpers'].values()))



@unittest.skipUnless(shutil.which('go'), 'Go compiler required')
class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='selected-go-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env = dict(os.environ, GOWORK='off', GOPROXY='off', GOSUMDB='off')
        for owner in go_workspace.DEPENDENCIES:
            module = self.root / owner; module.mkdir()
            (module / 'go.mod').write_text('module github.com/kuasar-sandbox/' + owner + '\n\ngo 1.26.1\n')
        (self.root / 'accelerator/selected.go').write_text('package accelerator\nconst Selected = "fixed-local-source"\n')
        (self.root / 'sandboxer/go.mod').write_text('module github.com/kuasar-sandbox/sandboxer\ngo 1.26.1\nrequire github.com/kuasar-sandbox/accelerator v0.1.10\n')
        (self.root / 'sandboxer/main.go').write_text('package main\nimport ("fmt"; "github.com/kuasar-sandbox/accelerator")\nfunc main() { fmt.Println(accelerator.Selected) }\n')
        self.before = {p: p.read_bytes() for p in self.root.glob('*/go.mod')}

    def tearDown(self):
        # Workspace generation and builds must not rewrite maintained manifests.
        if getattr(self, 'check_manifests', True):
            self.assertEqual({p: p.read_bytes() for p in self.before}, self.before)
        self.assertEqual(list(self.root.glob('*/go.sum')), [])

    def test_explicit_closures_do_not_include_uncompiled_modules(self):
        for owner, names in [('sandboxer', {'sandboxer','accelerator','connector'}),
                             ('orchestrator', {'orchestrator','sandboxer','accelerator','connector'}),
                             ('guest-runtime', {'guest-runtime','accelerator'})]:
            env = go_workspace.configure(self.root, [owner], self.env)
            doc = json.loads(subprocess.check_output(['go','work','edit','-json',env['GOWORK']], env=env))
            self.assertEqual({Path(x['DiskPath']).name for x in doc['Use']}, names)
            self.assertFalse(doc.get('Replace'))
            self.assertEqual(go_workspace.configure(self.root, [owner], self.env)['GOWORK'], env['GOWORK'])

    def test_build_uses_selected_local_dependency_despite_inherited_off(self):
        env = go_workspace.configure(self.root, ['sandboxer'], self.env)
        executable = self.root / 'probe'
        subprocess.run(['go', 'build', '-o', str(executable), '.'], cwd=self.root/'sandboxer', env=env, check=True)
        self.assertEqual(subprocess.check_output([executable], text=True).strip(), 'fixed-local-source')
        info = subprocess.check_output(['go','version','-m',executable], text=True)
        self.assertIn('github.com/kuasar-sandbox/accelerator\t(devel)', info)

    def test_legacy_sandboxer_make_override_keeps_recipe_and_selected_modules(self):
        recipe = self.root / 'sandboxer/Makefile'
        recipe.write_text('GO := go\nall:\n\tGOWORK=off $(GO) build -o ../probe .\n')
        before = recipe.read_bytes()
        env = go_workspace.helper_environment(self.root, 'sandboxer', self.env)
        subprocess.run(['make'], cwd=recipe.parent, env=env, check=True)
        self.assertEqual(subprocess.check_output([self.root/'probe'], text=True).strip(), 'fixed-local-source')
        self.assertEqual(recipe.read_bytes(), before)

    def test_legacy_explicit_go_entry_overrides_off_but_rejects_external_modules(self):
        env = go_workspace.helper_environment(self.root, 'orchestrator', self.env)
        env['GOWORK'] = 'off'
        subprocess.run([env['KUASAR_E2E_GO'], '-C', str(self.root/'sandboxer'), 'build', '-o', str(self.root/'probe'), '.'], env=env, check=True)
        external = self.root / 'external'; external.mkdir(); (external/'go.mod').write_text('module example.invalid/external\ngo 1.26.1\n')
        result = subprocess.run([env['KUASAR_E2E_GO'],'-C='+str(external),'env','GOWORK'], env=env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'outside its selected', result.stderr)
        self.assertEqual(self.env['GOWORK'], 'off')

    def test_source_raw_go_uses_selected_modules_and_preserves_external_isolation(self):
        env = go_workspace.helper_environment(self.root, 'orchestrator', self.env, source_checks=True)
        env['GOWORK'] = 'off'
        subprocess.run(['go','build','-o',str(self.root/'raw-probe'),'.'], cwd=self.root/'sandboxer', env=env, check=True)
        self.assertEqual(subprocess.check_output([self.root/'raw-probe'], text=True).strip(), 'fixed-local-source')
        external = self.root/'independent'; external.mkdir()
        (external/'go.mod').write_text('module example.invalid/independent\ngo 1.26.1\n')
        self.assertEqual(subprocess.check_output(['go','env','GOWORK'], cwd=external, env=env, text=True).strip(), 'off')
        tool = self.root/'orchestrator/validator.go'
        tool.write_text('package main\nimport "fmt"\nfunc main() { fmt.Println("isolated-validator") }\n')
        isolated = dict(env, GO111MODULE='off')
        self.assertEqual(subprocess.check_output(['go','run',str(tool)], cwd=tool.parent,
                        env=isolated, text=True).strip(), 'isolated-validator')
        env['GOWORK'] = str(self.root/'unexpected.work')
        result = subprocess.run(['go','env','GOWORK'], cwd=external, env=env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def release_repos(self):
        for name in go_workspace.DEPENDENCIES:
            path = self.root / name
            for args in (['init', '-q'], ['add', '.'], ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'],
                         ['tag', 'v0.1.10'], ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'tag', '-am', 'annotated', 'v0.1.11-preview.20261104.1']):
                subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True)

    def test_release_selection_accepts_exact_lightweight_and_annotated_tags(self):
        self.release_repos()
        for owner in ('sandboxer', 'orchestrator', 'guest-runtime'):
            for tag in ('v0.1.10', 'v0.1.11-preview.20261104.1'):
                selections = [name+'='+tag for name in go_workspace.closure([owner]) if name != owner]
                env = go_workspace.configure_release(self.root, [owner], selections, self.env)
                self.assertTrue(Path(env['GOWORK']).is_file())
        self.assertEqual(list((self.root/'.ci').rglob('*.*')), list((self.root/'.ci').rglob('go.work')))

    def test_release_selection_rejects_incomplete_duplicate_unknown_and_bad_tags(self):
        for selections in ([], ['accelerator=v0.1.10'], ['accelerator=v0.1.10']*2,
                           ['vmlinux=v0.1.10'], ['accelerator=main'], ['accelerator=v0.1.0-preview.20261104.0']):
            with self.subTest(selections=selections), self.assertRaises(ValueError):
                go_workspace.configure_release(self.root, ['sandboxer'], selections, self.env)
        with self.assertRaises(ValueError):
            go_workspace.configure_release(self.root, ['sandboxer','orchestrator'], [], self.env)

    def test_release_selection_rejects_missing_mismatched_tags_and_dirty_sources(self):
        self.release_repos()
        selections = ['accelerator=v0.1.10', 'connector=v0.1.10']
        accelerator = self.root/'accelerator'
        subprocess.run(['git','-C',str(accelerator),'tag','-d','v0.1.10'], check=True, capture_output=True)
        with self.assertRaises(subprocess.CalledProcessError):
            go_workspace.configure_release(self.root, ['sandboxer'], selections, self.env)
        subprocess.run(['git','-C',str(accelerator),'tag','v0.1.10'], check=True)
        (accelerator/'extra.go').write_text('package accelerator\n')
        with self.assertRaisesRegex(ValueError, 'uncommitted'):
            go_workspace.configure_release(self.root, ['sandboxer'], selections, self.env)
        subprocess.run(['git','-C',str(accelerator),'add','.'], check=True)
        subprocess.run(['git','-C',str(accelerator),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','changed'],check=True)
        with self.assertRaisesRegex(ValueError, 'selected tag'):
            go_workspace.configure_release(self.root, ['sandboxer'], selections, self.env)
        self.assertFalse((self.root/'.ci').exists())

    def test_release_selection_rejects_hidden_index_changes(self):
        self.release_repos()
        path = self.root/'accelerator'
        for flag in ('assume-unchanged', 'skip-worktree'):
            subprocess.run(['git','-C',str(path),'update-index','--'+flag,'selected.go'],check=True)
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, 'assume-unchanged or skip-worktree'):
                go_workspace.configure_release(self.root, ['guest-runtime'], ['accelerator=v0.1.10'], self.env)
            subprocess.run(['git','-C',str(path),'update-index','--no-'+flag,'selected.go'],check=True)
        self.assertFalse((self.root/'.ci').exists())

    def test_release_selection_cannot_hide_dirty_submodule_with_git_config(self):
        self.release_repos()
        path = self.root/'accelerator'
        def git(*args):
            subprocess.run(['git','-C',str(path),*args],check=True,capture_output=True)
        git('-c','protocol.file.allow=always','submodule','add','../connector','nested')
        git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qam','submodule')
        git('tag','-f','v0.1.10')
        git('config','submodule.nested.ignore','all')
        (path/'nested/untracked.txt').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'uncommitted'):
            go_workspace.configure_release(self.root, ['guest-runtime'], ['accelerator=v0.1.10'], self.env)
        self.assertFalse((self.root/'.ci').exists())

    def test_release_selection_rejects_different_license_source_overrides(self):
        self.release_repos()
        selections = ['accelerator=v0.1.10', 'connector=v0.1.10']
        for key in ('RELEASE_SOURCE_ROOT', 'RELEASE_ACCELERATOR_SOURCE_DIR', 'RELEASE_CONNECTOR_SOURCE_DIR'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'source directory'):
                go_workspace.configure_release(self.root, ['sandboxer'], selections, dict(self.env, **{key:str(self.root/'elsewhere')}))
        env = dict(self.env, RELEASE_SOURCE_ROOT=str(self.root/'sandboxer'),
                   RELEASE_ACCELERATOR_SOURCE_DIR=str(self.root/'accelerator'))
        go_workspace.configure_release(self.root, ['sandboxer'], selections, env)


    def test_missing_module_and_wrong_module_path_fail(self):
        self.check_manifests = False
        module = self.root/'connector/go.mod'; module.unlink()
        with self.assertRaisesRegex(ValueError, 'missing local'):
            go_workspace.configure(self.root, ['sandboxer'], self.env)
        module.write_text('module example.invalid/wrong\ngo 1.26.1\n')
        with self.assertRaisesRegex(ValueError, 'module path'):
            go_workspace.configure(self.root, ['sandboxer'], self.env)

    def test_existing_workspace_cannot_add_unselected_module_or_replace(self):
        env = go_workspace.configure(self.root, ['sandboxer'], self.env)
        path = Path(env['GOWORK']); original = path.read_text()
        for extra in ('\nuse '+str(self.root/'guest-runtime')+'\n', '\nreplace example.invalid/module => ../elsewhere\n'):
            path.write_text(original+extra)
            with self.assertRaisesRegex(ValueError, 'selected module closure'):
                go_workspace.configure(self.root, ['sandboxer'], self.env)
        path.write_text(original)

    def test_symlink_module_and_unknown_owner_fail(self):
        self.check_manifests = False
        shutil.rmtree(self.root/'connector')
        (self.root/'connector').symlink_to(self.root/'accelerator', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'missing local'):
            go_workspace.configure(self.root, ['sandboxer'], self.env)
        for owners in (['vmlinux'], ['go-erofs'], []):
            with self.assertRaises(ValueError): go_workspace.configure(self.root, owners, self.env)



if __name__ == "__main__":
    unittest.main()
