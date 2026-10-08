#!/usr/bin/env python3
import importlib.util, os, pathlib, shutil, subprocess, tempfile, unittest
ROOT=pathlib.Path(__file__).resolve().parents[2]
INTEGRATION=ROOT/"ci/integration"
HELPER=INTEGRATION/"source-owner.sh"
import artifacts
import build_helpers
from unittest.mock import patch
from test_fixtures import CASES, selection

def run(cmd,owner): return subprocess.run(["bash",str(HELPER),cmd,owner],text=True,capture_output=True)

class SourceOwnerClosureTest(unittest.TestCase):
    def words(self,c,o):
        r=run(c,o); self.assertEqual(r.returncode,0,r.stderr); return r.stdout.split()
    def test_bins(self):
        self.assertEqual(self.words("required-bins","connector"),["connector-ctl"])
        self.assertEqual(set(self.words("required-bins","guest-runtime")),{"mkfs.erofs","store-ctl","flatten-ctl"})
        self.assertEqual(set(self.words("required-bins","accelerator")),{"mkfs.erofs","manifest-ctl","store-ctl","cache-ctl","flatten-ctl"})
        self.assertTrue({"vmlinux","sandbox-ctl","cloud-hypervisor","sandbox-runtime.bundle","cache-ctl","connector-ctl"} <= set(self.words("required-bins","sandboxer")))
        full=self.words("required-bins","orchestrator"); self.assertEqual(full,self.words("required-bins","kuasar-sandbox")); self.assertIn("connector-ctl",full); self.assertIn("node-ctl",full)
    def test_sandboxer_builds_and_assembles_connector(self):
        with tempfile.TemporaryDirectory(prefix="source-owner-") as tmp:
            org=pathlib.Path(tmp); platform=org/"platform"
            (platform/"ci/integration").mkdir(parents=True)
            (platform/"release").mkdir()
            shutil.copy2(HELPER,platform/"ci/integration/source-owner.sh")
            manifest=ROOT/"release/bin-inputs.manifest"
            shutil.copy2(manifest,platform/"release/bin-inputs.manifest")
            required=set(self.words("required-bins","sandboxer"))
            for line in manifest.read_text().splitlines():
                if not line.strip() or line.lstrip().startswith("#"): continue
                repo,name,*_=line.split()
                if name in required and name!="connector-ctl":
                    path=org/repo/"bin/x86_64"/name
                    path.parent.mkdir(parents=True,exist_ok=True); path.write_text(name)
            tools=org/"tools";tools.mkdir();log=org/"make.log"
            make=tools/"make"
            make.write_text("#!/usr/bin/env python3\nimport os,pathlib,sys\na=sys.argv[1:]\nrepo=pathlib.Path(a[a.index('-C')+1])\nwith open(os.environ['MAKE_LOG'],'a') as f:f.write(repo.name+' '+a[-1]+'\\n')\nif repo.name=='connector' and a[-1]=='build':\n p=repo/'bin/x86_64/connector-ctl'\n p.parent.mkdir(parents=True,exist_ok=True)\n p.write_text('built connector')\n")
            make.chmod(0o755)
            env=dict(os.environ,PATH=str(tools)+os.pathsep+os.environ["PATH"],MAKE_LOG=str(log),TARGET_ARCH="x86_64")
            result=subprocess.run(["bash",str(platform/"ci/integration/source-owner.sh"),"build","sandboxer"],env=env,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn("connector build",log.read_text().splitlines())
            self.assertEqual((platform/"bin/x86_64/connector-ctl").read_text(),"built connector")
            self.assertNotIn("orchestrator build",log.read_text().splitlines())
    def test_native(self):
        self.assertEqual(self.words("native-components","connector"),[])
        self.assertEqual(self.words("native-components","guest-runtime"),["erofs"])
        self.assertEqual(set(self.words("native-components","accelerator")),{"erofs","rocksdb"})
        self.assertEqual(set(self.words("native-components","sandboxer")),{"vmlinux","erofs","envd","rocksdb","cloud-hypervisor"})
    def test_images(self):
        self.assertEqual(self.words("images","connector"),[]); self.assertEqual(self.words("images","guest-runtime"),[])
        self.assertEqual(set(self.words("images","accelerator")),{"python:3.12-slim","python:3.12-alpine"})
        self.assertEqual(set(self.words("images","sandboxer")),{"python:3.12-slim","busybox:latest"})
        self.assertEqual(self.words("images","orchestrator"),["python:3.12-slim"])
    def test_heavy_gates(self):
        for o in ("sandboxer","kuasar-sandbox"): self.assertEqual(run("needs-uffd",o).returncode,0); self.assertEqual(run("needs-working-set",o).returncode,0)
        for o in ("accelerator","connector","guest-runtime","orchestrator"): self.assertNotEqual(run("needs-uffd",o).returncode,0); self.assertNotEqual(run("needs-working-set",o).returncode,0)
    def test_unknown(self): self.assertNotEqual(run("required-bins","unknown").returncode,0)

class PreparedHelperSelectionTest(unittest.TestCase):
    @staticmethod
    def load_build_artifacts():
        spec=importlib.util.spec_from_file_location("build_artifacts",INTEGRATION/"build-artifacts.py")
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
    def test_runner_test_helper_uses_orchestrator_test_identity(self):
        for case in ('orchestrator.lifecycle.sh', 'telemetry.guest.sh'):
            selected = artifacts.planned_helpers({'cases': [case]})
            self.assertEqual(selected['node-ctl-runner-test'], 'orchestrator')
        self.assertNotIn('node-ctl-runner-test', artifacts.planned_helpers({'cases': []}))
        self.assertNotIn('node-ctl-runner-test', artifacts.planned_helpers({'cases': ['storage.cache.sh']}))

    def test_runner_helper_cross_build_retains_the_exact_workspace(self):
        # The required local module is intentionally unavailable from a proxy.
        # Losing the pinned workspace must fail instead of resolving elsewhere.
        # This minimal fixture uses no new language features; do not require an
        # offline compiler test to download the product module's Go toolchain.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            sources = root / 'sources'
            owner, sdk = sources / 'orchestrator', sources / 'sdk'
            (owner / 'cmd/node-ctl').mkdir(parents=True)
            sdk.mkdir()
            (owner / 'go.mod').write_text('module example.test/orchestrator\ngo 1.22\nrequire example.test/sdk v0.0.0\n')
            (sdk / 'go.mod').write_text('module example.test/sdk\ngo 1.22\n')
            (sdk / 'identity.go').write_text('package sdk\nconst Identity = "exact-test-pin"\n')
            (sources / 'go.work').write_text('go 1.22\nuse (\n ./orchestrator\n ./sdk\n)\n')
            (owner / 'cmd/node-ctl/main.go').write_text('package main\nfunc main() {}\n')
            (owner / 'cmd/node-ctl/main_test.go').write_text(
                'package main\nimport ("testing"; "example.test/sdk")\n'
                'func TestPinnedRunner(t *testing.T) { if sdk.Identity != "exact-test-pin" { t.Fatal(sdk.Identity) } }\n')
            environment = {key: value for key, value in os.environ.items()
                           if key not in ('GH_TOKEN', 'GITHUB_TOKEN', 'CALLER_TOKEN', 'KUASAR_CI_APP_PRIVATE_KEY')}
            environment.update(GOWORK=str(sources / 'go.work'), GOPROXY='off', GOSUMDB='off')
            for arch in ('x86_64', 'aarch64'):
                output = root / arch
                build_helpers.build(sources, arch, output, {'node-ctl-runner-test': 'orchestrator'}, environment)
                binary = output / 'node-ctl-runner-test'
                artifacts.check_architecture(binary, arch)
                self.assertEqual(set(artifacts.tree_files(output)), {'node-ctl-runner-test'})
                if arch == os.uname().machine:
                    result = subprocess.run([str(binary), '-test.run=^TestPinnedRunner$', '-test.v'],
                                            text=True, capture_output=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('--- PASS: TestPinnedRunner', result.stdout)

    def test_runner_helper_compile_failure_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with patch.object(build_helpers.subprocess, 'run', side_effect=subprocess.CalledProcessError(23, ['go'])):
                with self.assertRaises(subprocess.CalledProcessError) as failure:
                    build_helpers.build(root, 'x86_64', root / 'output', {'node-ctl-runner-test': 'orchestrator'}, {})
            self.assertEqual(failure.exception.returncode, 23)

    def test_cgroup_probe_is_selected_only_for_its_native_case(self):
        cgroup=selection(["sandboxer"],"x86_64",{"sandboxer":["sandbox.cgroup.sh"]})
        self.assertEqual(artifacts.planned_helpers(cgroup)["cgroup-fork-probe"],"sandboxer")
        lifecycle=selection(["sandboxer"],"x86_64",{"sandboxer":["sandbox.lifecycle.sh"]})
        self.assertNotIn("cgroup-fork-probe",artifacts.planned_helpers(lifecycle))
        arm=selection(["sandboxer"],"aarch64",{"sandboxer":["sandbox.cgroup.sh"]})
        self.assertNotIn("cgroup-fork-probe",artifacts.planned_helpers(arm))
    def test_sparse_executor_import_does_not_require_preparation_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / 'ci/integration'
            target.mkdir(parents=True)
            shutil.copy2(INTEGRATION / 'artifacts.py', target / 'artifacts.py')
            result = subprocess.run(['python3', '-B', '-c',
                'import artifacts; assert artifacts.case_name("sandbox.cgroup.sh") == "sandbox.cgroup.sh"'],
                cwd=target, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_arm_full_selection_requires_the_same_probe(self):
        native = artifacts.suite_selection(['sandboxer'], 'aarch64',
                    CASES | {'sandboxer': ['sandbox.cgroup.sh']}, native_full=True)
        self.assertEqual(artifacts.planned_helpers(native)['cgroup-fork-probe'], 'sandboxer')

    def test_cgroup_probe_uses_only_exact_sandboxer_test_source(self):
        profile=selection(["sandboxer"],"x86_64",{"sandboxer":["sandbox.cgroup.sh"]})
        plan={"lanes":{"x86_64":{"selection":profile,"embedded_products":[]}},"test_overlays":["sandboxer"],"product_sources":{}}
        self.assertEqual(self.load_build_artifacts().source_owners(plan,"x86_64"),{"sandboxer"})
    def test_builder_and_runner_keep_probe_in_prepared_helper_pipeline(self):
        builder=(INTEGRATION/"build_helpers.py").read_text()
        runner=(ROOT/"test/e2e/lib/workspace.py").read_text()
        self.assertIn("('cgroup-fork-probe', 'e2e-cgroup-fork-probe')",builder)
        self.assertIn("if name in helpers:",builder)
        self.assertIn('"cgroup-fork-probe": "CGROUP_FORK_PROBE_BIN"',runner)
        self.assertNotIn('e2e-cgroup-fork-probe',runner)

if __name__=="__main__": unittest.main()
