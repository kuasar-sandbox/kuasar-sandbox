#!/usr/bin/env python3
"""Contract regressions using real archives and EROFS, without running payloads."""
import copy
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import artifacts as subject
import transport
from test_fixtures import CASES, WORKBENCH_CASES, selection, select_plan, architecture_result, workbench_results, registry_binding


def test_revisions(sha="d" * 40):
    return subject.release_test_revisions({owner: sha for owner in subject.OWNERS if owner != "platform"}, sha)


def elf(arch, marker):
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<H", header, 18, {"x86_64": 62, "aarch64": 183}[arch])
    return bytes(header) + marker.encode()


def kernel(arch):
    if arch == "x86_64":
        return elf(arch, "kernel")
    header = bytearray(64)
    struct.pack_into("<Q", header, 16, 4096)
    header[56:60] = b"ARM\x64"
    return bytes(header) + bytes(4096 - 64)


def archive(path, files):
    with tarfile.open(path, "w:gz") as output:
        for name, content in files.items():
            entry = tarfile.TarInfo("./" + name)
            entry.size, entry.mode = len(content), 0o644 if name == "bin/vmlinux.sha256" else 0o755
            output.addfile(entry, io.BytesIO(content))
    return {"name": path.name, "size": path.stat().st_size, "digest": "sha256:" + subject.digest(path)}


def runtime(root, arch, files):
    tree = root / f"runtime-{arch}"
    for name, source in (("sbin/init", "sandbox-init"), ("opt/sandbox-runtime/bin/flatten-ctl", "flatten-ctl"),
                         ("opt/sandbox-runtime/bin/mkfs.erofs", "mkfs.erofs"), ("opt/sandbox-runtime/bin/envd", None)):
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files[source] if source else elf(arch, "envd"))
        path.chmod(0o755)
    image = root / f"{arch}.erofs"
    subprocess.run(["mkfs.erofs", "--all-root", "-T0", str(image), str(tree)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    def footer(digest):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as data:
            entry = zipfile.ZipInfo(".kuasar.digest." + digest, (1980, 1, 1, 0, 0, 0))
            entry.external_attr = 0o100444 << 16
            data.writestr(entry, b"")
        return output.getvalue()
    content = image.read_bytes()
    prefix = content + bytes((2 << 20) - len(footer("0" * 64)) - len(content))
    return prefix + footer(hashlib.sha256(prefix).hexdigest())


class SourceTestPinContracts(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("source_pin_resolver", Path(__file__).with_name("resolve-artifacts.py"))
        self.resolver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.resolver)
        self.pins = {owner: "b" * 40 for owner in subject.OWNERS if owner != "platform"}
        self.baseline = {"test_revisions": subject.release_test_revisions(self.pins, "a" * 40)}
        self.record = {"base_sha": "c" * 40, "candidate_sha": "d" * 40}

    def resolve(self, before, after, record=True):
        def manifest(pins):
            return "test_revisions:\n" + "".join(f"  {owner}: {sha}\n" for owner, sha in pins.items())
        with patch.object(self.resolver, "source_text", side_effect=[manifest(before), manifest(after)]) as source:
            result = self.resolver.source_test_revisions(self.baseline, self.record if record else None, "c" * 40, "main")
        return result, source.call_count

    def test_unpublished_inherited_pins_do_not_mix_with_predecessor_products(self):
        inherited = {owner: "e" * 40 for owner in self.pins}
        result, calls = self.resolve(inherited, inherited)
        self.assertEqual(calls, 2)
        for owner, sha in self.pins.items():
            self.assertEqual(result[owner]["sha"], sha)
        self.assertEqual(result["platform"]["sha"], self.record["candidate_sha"])

    def test_explicit_platform_pin_change_is_preserved(self):
        inherited = {owner: "e" * 40 for owner in self.pins}
        result, _ = self.resolve(inherited, inherited | {"sandboxer": "f" * 40})
        self.assertEqual(result["sandboxer"]["sha"], "f" * 40)
        self.assertEqual(result["orchestrator"]["sha"], self.pins["orchestrator"])

    def test_component_pr_uses_paired_baseline_before_its_owner_override(self):
        result, calls = self.resolve({}, {}, record=False)
        self.assertEqual(calls, 0)
        self.assertEqual(result["orchestrator"]["sha"], self.pins["orchestrator"])
        self.assertEqual(result["platform"]["sha"], "c" * 40)

    def test_complete_release_retains_independent_test_pins(self):
        result, _ = self.resolve(self.pins, self.pins)
        self.assertEqual(result["orchestrator"], self.baseline["test_revisions"]["orchestrator"])

    def test_missing_baseline_test_identity_is_not_guessed(self):
        del self.baseline["test_revisions"]["orchestrator"]
        with self.assertRaisesRegex(ValueError, "owner test pins"):
            self.resolve(self.pins, self.pins)

    def test_candidate_does_not_mutate_baseline_evidence(self):
        before = json.dumps(self.baseline, sort_keys=True)
        self.resolve(self.pins, self.pins | {"sandboxer": "f" * 40})
        self.assertEqual(json.dumps(self.baseline, sort_keys=True), before)


class ReleasedCaseLayout(unittest.TestCase):
    def test_platform_user_material_and_historical_docs_keep_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / 'platform.tar.gz'
            files = {'guide/connector/README.md': b'# User guide\n',
                     'guide/licenses/connector/LICENSE': b'Legal text\n',
                     'workbench/workbench': b'#!/usr/bin/python3\n',
                     'docs/connector.md': b'# Historical guide\n'}
            archive(payload, files)
            seen = {}
            subject.unpack(payload, root / 'release', 'platform', seen)
            for name, content in files.items():
                self.assertEqual((root / 'release' / name).read_bytes(), content)
                self.assertEqual(seen[name], 'platform')
            with self.assertRaisesRegex(ValueError, 'conflicting archive ownership'):
                subject.unpack(payload, root / 'release', 'platform', seen)
            archive(payload, {'bin/node': b'not a platform product'})
            with self.assertRaisesRegex(ValueError, 'platform cannot own'):
                subject.unpack(payload, root / 'release', 'platform', {})

    def test_prepare_retains_guides_but_rejects_owner_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            root = stage / 'test/e2e'
            case = root / 'cases/storage.fixture.sh'
            case.parent.mkdir(parents=True)
            case.write_text('exit 0\n')
            guide = root / 'accelerator/README.md'
            guide.parent.mkdir()
            guide.write_text('# Prepared E2E\n')
            files = {owner: [] for owner in subject.OWNERS}
            files['accelerator'] = [case.name]
            self.assertEqual(subject.normalize_e2e_cases(stage, files, from_owners=False),
                             {case.name: 'accelerator'})
            self.assertEqual(guide.read_text(), '# Prepared E2E\n')
            for name, mode in [('accelerator/run_all.sh', 0o755),
                               ('accelerator/cases/storage.fixture.sh', 0o644),
                               ('accelerator/execute.md', 0o755),
                               ('unknown/README.md', 0o644)]:
                with self.subTest(name=name):
                    path = root / name
                    path.parent.mkdir(exist_ok=True)
                    path.write_text('content\n')
                    path.chmod(mode)
                    with self.assertRaisesRegex(ValueError, 'superseded owner'):
                        subject.normalize_e2e_cases(stage, files, from_owners=False)
                    path.unlink()
                    if path.parent != guide.parent:
                        path.parent.rmdir()


class ArtifactBuildContracts(unittest.TestCase):
    def test_helper_checkout_uses_test_pin_and_preserves_product_source(self):
        spec = importlib.util.spec_from_file_location("builder", Path(__file__).with_name("build-artifacts.py"))
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            def git(*args):
                return subprocess.check_output(["git", "-C", str(repository), *args])
            git("init", "-q")
            git("config", "user.name", "test-pin-fixture")
            git("config", "user.email", "test-pin@example.invalid")
            (repository / "cmd").mkdir()
            (repository / "cmd/product").write_bytes(b"unchanged product input")
            (repository / "go.mod").write_text("module proxy-helper-fixture\n\ngo 1.26.1\n")
            test = repository / "cmd/custom-proxy/main.go"
            test.parent.mkdir(parents=True)
            test.write_text('package main\nimport "fmt"\n'
                            'func main() { fmt.Println("old helper") }\n')
            script = repository / "scripts/ci-e2e-build.sh"
            script.parent.mkdir()
            script.write_text('cd "$(dirname "$0")/.."; GOWORK=off go build -o "$3/custom-proxy" ./cmd/custom-proxy\n')
            git("add", ".")
            git("commit", "-qm", "product and old helper")
            product = git("rev-parse", "HEAD").decode().strip()
            test.write_text(test.read_text().replace("old helper", "pinned helper"))
            git("add", ".")
            git("commit", "-qm", "helper-only change")
            pin = git("rev-parse", "HEAD").decode().strip()
            def checkout(name, sha, destination):
                destination.mkdir()
                if name == "kuasar-sandbox/orchestrator":
                    with tarfile.open(fileobj=io.BytesIO(git("archive", sha))) as tree:
                        tree.extractall(destination, filter="data")
            for rebuild in (False, True):
                plan = {"sources": test_revisions(product), "test_revisions": test_revisions(product),
                        "mode": "source", "test_overlays": list(subject.OWNERS), "product_sources":
                        {"node-ctl": {"kuasar-sandbox/orchestrator": product}} if rebuild else {},
                        "lanes": {"x86_64": {"selection": selection(["orchestrator"], "x86_64")}}}
                plan["test_revisions"]["orchestrator"]["sha"] = pin
                sources = root / str(rebuild)
                with patch.object(builder, "checkout", side_effect=checkout):
                    builder.materialize(plan, "x86_64", sources)
                    helper = builder.helper_sources(plan, "x86_64", sources)
                self.assertEqual(plan["sources"]["orchestrator"]["sha"], product)
                self.assertEqual((sources / "orchestrator/cmd/product").read_bytes(), b"unchanged product input")
                self.assertEqual(helper == sources, not rebuild)
                output = root / ("helper-" + str(rebuild))
                output.mkdir()
                subprocess.run(["bash", str(helper / "orchestrator/scripts/ci-e2e-build.sh"),
                                "fixtures", "x86_64", str(output)], check=True)
                result = subprocess.check_output([str(output / "custom-proxy")], text=True)
                self.assertEqual(result, "pinned helper\n")
                if rebuild:
                    self.assertIn("old helper", (sources / "orchestrator/cmd/custom-proxy/main.go").read_text())

    def test_build_launches_nonexecutable_framework_helper_and_preserves_failure(self):
        spec = importlib.util.spec_from_file_location("builder", Path(__file__).with_name("build-artifacts.py"))
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        plan = {"schema": 2, "mode": "source", "case_files": CASES, "framework_sha": "a" * 40, "owners": ["platform"],
                "baseline": {"delivery": "historical"},
                "test_overlays": list(subject.OWNERS), "product_sources": {}, "test_revisions": test_revisions(),
                "sources": test_revisions(),
                "lanes": {arch: {"products": [], "performance": ["working-set-smoke"] if arch == "x86_64" else [], "selection": selection(["platform"], arch)}
                          for arch in subject.ARCHES}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "ci/hosted/exact-assets-tools.sh"
            helper.parent.mkdir(parents=True)
            (root / "test/e2e/lib").mkdir(parents=True)
            (root / "test/e2e/e2e").write_text("#!/usr/bin/env python3\n")
            (root / "test/e2e/e2e").chmod(0o755)
            (root / "test/e2e/lib/common.sh").write_text("#!/usr/bin/env bash\n")
            helper.write_text('#!/usr/bin/env bash\n'
                              'printf "%s\\n" "$TARGET_ARCH" > "$KUASAR_E2E_TOOL_OUTPUT/invoked"\n'
                              'exit 23\n')
            helper.chmod(0o644)  # Match the framework script's Git mode.
            for owner, names in CASES.items():
                for name in names:
                    case = root / "test/e2e" / ("platform/cases" if owner == "platform" else "cases") / name
                    case.parent.mkdir(parents=True, exist_ok=True)
                    case.write_text("exit 0\n")
            output = root / "build-output"
            credentials = {key: "" for key in ("GH_TOKEN", "GITHUB_TOKEN", "CALLER_TOKEN", "KUASAR_CI_APP_PRIVATE_KEY")}
            with patch.object(builder, "ROOT", root), patch.object(builder.build_helpers, "ROOT", root), \
                 patch.object(builder.build_demo_wheels, "build"), \
                 patch.object(builder, "materialize"), patch.object(builder, "configure_workspace"), \
                 patch.object(builder, "test_source", return_value=root), patch.object(builder, "helper_sources", return_value=root), \
                 patch.object(builder.platform, "machine", return_value="x86_64"), \
                 patch.object(builder.subprocess, "check_output", return_value=("a" * 40 + "\n")), \
                 patch.dict(os.environ, credentials):
                with self.assertRaises(subprocess.CalledProcessError) as failure:
                    builder.build(plan, "x86_64", root / "assets", root / "sources", output)
            self.assertEqual(failure.exception.returncode, 23)
            self.assertEqual((output / "helpers/invoked").read_text(), "x86_64\n")
            self.assertFalse((output / "outputs.json").exists())


class ArtifactExecutionContracts(unittest.TestCase):
    def setUp(self):
        def load(name, file):
            spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        self.executor = load('executor', 'run-artifact-tests.py')
        self.performance = load('performance', 'run-artifact-performance.py')
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'prepared'
        (self.root / 'bin').mkdir(parents=True)
        source = Path(__file__).resolve().parents[2] / 'test/e2e'
        shutil.copytree(source / 'lib', self.root / 'test/e2e/lib')
        shutil.copy2(source / 'e2e', self.root / 'test/e2e/e2e')
        spec = importlib.util.spec_from_file_location('prepared_inputs', source / 'lib/workspace.py')
        self.inputs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.inputs)
        self.case = 'storage.fixture.sh'
        script = self.root / 'test/e2e/cases' / self.case
        script.parent.mkdir()
        self.marker = self.root.parent / 'scratch-marker'
        script.write_text('set -eu\nprintf "%s" "$TMPDIR" > "' + str(self.marker) + '"\n'
                          'install -d -m 700 "$TMPDIR/owned"\ntouch "$TMPDIR/owned/state"\n')
        self.plan = {'lanes': {'x86_64': {'performance': []}}, 'test_revisions': test_revisions()}
        self.provenance = {'arch': 'x86_64', 'selection': {'cases': [self.case]}, 'helpers': {},
                           'embedded': {'init': 'a' * 64}, 'test_revisions': test_revisions(),
                           'prepared_cases': [self.case]}
        self.result = self.root.parent / 'result.json'
        self.addCleanup(self.remove_scratch)

    def remove_scratch(self):
        if self.marker.exists():
            state = Path(self.marker.read_text()).parent
            self.assertEqual(state.parent, Path(os.environ.get('TMPDIR', '/var/tmp')).resolve())
            self.assertTrue(state.name.startswith('ki-'))
            privilege = [] if os.geteuid() == 0 else ['sudo', '-n']
            subprocess.run([*privilege, 'rm', '-rf', '--', str(state)], check=True)

    def execute(self, shard='storage', performance=False):
        self.inputs.seal(self.root, self.provenance)
        credentials = {key: '' for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'CALLER_TOKEN', 'KUASAR_CI_APP_PRIVATE_KEY')}
        with patch.object(self.executor.artifacts, 'verify_workspace', return_value=self.provenance), \
             patch.object(self.executor.platform, 'machine', return_value='x86_64'), patch.dict(os.environ, credentials):
            if performance:
                return self.performance.execute(self.plan, 'x86_64', self.root, self.result)
            return self.executor.execute(self.plan, 'x86_64', shard, self.root, self.result)

    def image(self, *, identity=None, architecture='amd64'):
        archive = self.root / 'image.tar'
        archive.write_bytes(b'prepared image')
        expected = 'sha256:' + 'b' * 64
        self.provenance['images'] = {'python': {'archive': archive.name, 'sha256': subject.digest(archive),
                                               'image_id': expected, 'platform': 'linux/amd64'}}
        binary = self.root.parent / 'host-bin'
        binary.mkdir(exist_ok=True)
        record = [{'Id': identity or expected, 'Os': 'linux', 'Architecture': architecture}]
        docker = binary / 'docker'
        docker.write_text('#!/usr/bin/env python3\nimport sys\n'
                          'if sys.argv[1:3] == ["image", "inspect"]: print(' + repr(json.dumps(record)) + ')\n')
        docker.chmod(0o755)
        return str(binary) + os.pathsep + os.environ['PATH']

    def test_orchestrator_receives_prepared_proxy_helper_and_selected_products(self):
        case = 'orchestrator.proxy.sh'
        (self.root / 'test/e2e/cases' / case).write_text(
            'set -eu\n[ "$CUSTOM_PROXY_BIN" = "$(dirname "$BIN")/fixtures/bin/custom-proxy" ]\n')
        self.provenance['selection']['cases'] = self.provenance['prepared_cases'] = [case]
        self.provenance['helpers'] = {'custom-proxy': {}}
        self.assertEqual(self.execute('orchestrator')['conclusion'], 'success')

    def test_staged_case_socket_path_uses_direct_public_runner(self):
        case = 'sandbox.cgroup.sh'
        (self.root / 'test/e2e/cases' / case).write_text(
            'set -eu\npython3 - "$WORK" <<\'PY\'\n'
            'from pathlib import Path\nimport socket, sys\n'
            'path = Path(sys.argv[1]) / "false-shared/runtime/cg-false-shared-10672/uffd.sock"\n'
            'path.parent.mkdir(parents=True)\n'
            'with socket.socket(socket.AF_UNIX) as sock: sock.bind(str(path))\n'
            'PY\n')
        self.provenance['selection']['cases'] = self.provenance['prepared_cases'] = [case]
        self.plan['mode'] = 'exact-assets'
        result = self.execute('sandbox')
        self.assertEqual(result['conclusion'], 'success')
        self.assertEqual(result['cases'], [case])

    def test_working_set_receives_exact_source_manifest_and_preserves_exit(self):
        self.plan['sources'] = test_revisions('c' * 40)
        self.plan['lanes']['x86_64']['performance'] = ['working-set-smoke']
        perf = self.root / 'test/perf'
        perf.mkdir()
        (perf / 'working-set-netns.sh').write_text('exec bash "$@"\n')
        recorded = self.root.parent / 'recorded-revisions.tsv'
        (perf / 'sandbox-perf-working-set.sh').write_text(
            'set -eu\ncp "$KUASAR_REVISION_MANIFEST" "' + str(recorded) + '"\nexit "$TEST_WORKING_SET_EXIT"\n')
        path = self.image()
        for exit_code in (0, 41):
            with self.subTest(exit_code=exit_code), patch.dict(os.environ, PATH=path, TEST_WORKING_SET_EXIT=str(exit_code)):
                if exit_code:
                    with self.assertRaisesRegex(ValueError, 'working-set smoke failed'):
                        self.execute(performance=True)
                else:
                    self.execute(performance=True)
                result = json.loads(self.result.read_text())
                self.assertEqual(result['conclusion'], 'failure' if exit_code else 'success')
                self.assertEqual([item['exit_code'] for item in result['timings']], [exit_code])
                with recorded.open() as manifest:
                    rows = list(csv.DictReader(manifest, delimiter='\t'))
                self.assertEqual(rows, [{'repository': record['repository'], 'requested_ref': record['sha'],
                                         'resolved_sha': record['sha'], 'role': record['role']}
                                        for record in self.plan['sources'].values()])

    def test_privileged_case_state_is_removed_before_success(self):
        result = self.execute()
        self.assertEqual(result['conclusion'], 'success')
        self.assertEqual(result['timings'][0]['exit_code'], 0)
        self.assertFalse(Path(self.marker.read_text()).parent.exists())

    def test_cleanup_failure_keeps_result_failed(self):
        original_run = subprocess.run
        def run(command, **kwargs):
            if command[:5] == ['sudo', '-n', 'rm', '-rf', '--']:
                raise subprocess.CalledProcessError(23, command)
            return original_run(command, **kwargs)
        with patch.object(self.executor.execution.shutil, 'rmtree', side_effect=PermissionError('root-owned state')), \
             patch.object(self.executor.subprocess, 'run', side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError) as failure:
                self.execute()
        self.assertEqual(failure.exception.returncode, 23)
        result = json.loads(self.result.read_text())
        self.assertEqual(result['timings'][0]['exit_code'], 0)
        self.assertEqual(result['conclusion'], 'failure')

    def test_loaded_image_identity_and_platform_are_required_before_case_execution(self):
        for options in ({'architecture': 'arm64'}, {'identity': 'sha256:' + 'c' * 64}):
            with self.subTest(options=options), patch.dict(os.environ, PATH=self.image(**options)):
                with self.assertRaisesRegex(ValueError, 'public runner reported failure'):
                    self.execute()
            self.assertEqual(json.loads(self.result.read_text())['timings'], [])
            self.assertEqual(json.loads(self.result.read_text())['conclusion'], 'failure')
            self.assertFalse(self.marker.exists())
        with patch.dict(os.environ, PATH=self.image()):
            self.assertEqual(self.execute()['conclusion'], 'success')

    def test_changed_image_archive_is_rejected_before_docker_load(self):
        path = self.image()
        self.provenance['images']['python']['sha256'] = '0' * 64
        with patch.dict(os.environ, PATH=path), self.assertRaisesRegex(ValueError, 'public runner reported failure'):
            self.execute()
        self.assertFalse(self.marker.exists())
        self.assertEqual(json.loads(self.result.read_text())['conclusion'], 'failure')

    def test_missing_execution_records_cannot_claim_success(self):
        with self.assertRaisesRegex(ValueError, 'execution evidence'):
            subject.check_timings([], [self.case])
        with self.assertRaisesRegex(ValueError, 'did not pass'):
            subject.check_timings([{'case': self.case, 'exit_code': 1, 'wall_seconds': 0.1}], [self.case])


class KernelChecksumArchiveContracts(unittest.TestCase):
    def test_optional_checksum_is_kernel_owned_metadata_not_a_product(self):
        self.assertNotIn("vmlinux.sha256", subject.PRODUCTS)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = kernel("x86_64")
            checksum = (hashlib.sha256(raw).hexdigest() + "  vmlinux\n").encode()
            payload = root / "kernel.tar.gz"
            archive(payload, {"bin/vmlinux": raw, "bin/vmlinux.sha256": checksum})
            seen = {}
            subject.unpack(payload, root / "out", "vmlinux", seen)
            self.assertEqual(seen["bin/vmlinux.sha256"], "vmlinux")
            self.assertEqual((root / "out/bin/vmlinux.sha256").read_bytes(), checksum)
            # Visit the metadata first: otherwise the existing kernel-owner
            # rejection could hide a regression in the new sidecar rule.
            archive(payload, {"bin/vmlinux.sha256": checksum, "bin/vmlinux": raw})
            for unit in ("runtime", "sandboxer", "platform"):
                with self.subTest(unit=unit), self.assertRaisesRegex(ValueError, "invalid kernel checksum metadata entry"):
                    subject.unpack(payload, root / unit, unit, {})
            archive(payload, {"bin/vmlinux.sha256": checksum})
            with self.assertRaisesRegex(ValueError, "same archive"):
                subject.unpack(payload, root / "orphan", "vmlinux", {})
            archive(payload, {"bin/vmlinux": raw, "bin/vmlinux.sha256": checksum + b"extra"})
            with self.assertRaisesRegex(ValueError, "checksum metadata"):
                subject.unpack(payload, root / "extra", "vmlinux", {})


class ArtifactContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ("mkfs.erofs", "fsck.erofs", "dump.erofs"):
            if not shutil.which(tool):
                raise RuntimeError("required trusted host reader is missing: " + tool)
        if not os.environ.get("KUASAR_RUNTIME_READER"):
            raise RuntimeError("KUASAR_RUNTIME_READER must select the existing pinned Runtime verifier")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.plan = {
            "schema": 2, "mode": "source", "case_files": CASES, "framework_sha": "a" * 40, "owners": ["accelerator"],
            "baseline": {"version": "release-v1.2.3", "sha": "b" * 40, "units": {
                unit: {"version": (unit + "-" if unit in ("runtime", "vmlinux") else "") + "v1.2.3",
                       "sha": "c" * 40} for unit in subject.UNITS}, "assets": []},
            "lanes": {arch: {"products": ["manifest-ctl"], "performance": [], "selection": selection(["accelerator"], arch)}
                      for arch in subject.ARCHES},
            "test_overlays": list(subject.OWNERS),
            "test_revisions": test_revisions(),
            "product_sources": {"manifest-ctl": {"accelerator": "e" * 40}},
        }
        self.files = {}
        for arch in subject.ARCHES:
            files = {name: elf(arch, name) for name in subject.PRODUCTS}
            files["vmlinux"] = kernel(arch)
            files["sandbox-runtime.bundle"] = runtime(self.root, arch, files)
            self.files[arch] = files
            for unit, selected in self.plan["baseline"]["units"].items():
                name = subject.archive_name(unit, selected["version"], arch)
                record = archive(self.assets / name, {"bin/" + name: data for name, data in files.items()
                                                      if subject.PRODUCTS[name] == unit})
                self.plan["baseline"]["assets"].append(record)
        tests = {f"test/e2e/{owner}/run_all.sh": b"#!/bin/sh\nexit 0\n" for owner in subject.OWNERS}
        tests["test/e2e/accelerator/lib/removed-helper.py"] = b"old helper"
        self.plan["baseline"]["assets"].append(archive(self.assets / "platform-release-v1.2.3.tar.gz", tests))

    def workbench_stage(self):
        """Identity fixtures for binding only; no image execution is simulated."""
        version, sha = self.plan['baseline']['version'], self.plan['baseline']['sha']
        (self.root / 'workbench').mkdir()
        receipts = {}
        for index, arch in enumerate(subject.ARCHES, 1):
            name = f'workbench-{arch}-{version.removeprefix("release-")}.tar.gz'
            path = self.assets / name
            path.write_bytes(('unit-test image identity ' + arch).encode())
            receipts[arch] = {'arch': arch, 'aggregate_version': version, 'source_revision': sha,
                'archive': name, 'sha256': subject.digest(path), 'size': path.stat().st_size,
                'image_id': 'sha256:' + str(index) * 64}
            (self.root / 'workbench' / f'workbench-{arch}.json').write_text(json.dumps(receipts[arch]))
        (self.assets / 'SHA256SUMS').write_text(''.join(f'{value}  {name}\n' for name, value in subject.tree_files(self.assets).items()))
        self.plan['baseline'].update(delivery='workbench-v1', staged=True, assets=[
            {'name': path.name, 'size': path.stat().st_size, 'digest': 'sha256:' + subject.digest(path)}
            for path in sorted(self.assets.iterdir())])
        self.plan.update(mode='exact-assets', case_files=WORKBENCH_CASES, owners=['platform'], test_overlays=[], product_sources={})
        for arch in subject.ARCHES:
            self.plan['lanes'][arch] = {'products': [], 'performance': [],
                'selection': subject.suite_selection(['platform'], arch, WORKBENCH_CASES)}
        return receipts

    def delta(self, arch="x86_64"):
        root = self.root / f"delta-{arch}"
        (root / "bin").mkdir(parents=True)
        records = {}
        for name in self.plan["lanes"][arch]["products"]:
            path = root / "bin" / name
            path.write_bytes(elf(arch, "candidate " + name))
            path.chmod(0o755)
            records[name] = {"sha256": subject.digest(path), "sources": self.plan["product_sources"][name]}
        test_records = {}
        for owner, names in self.plan["case_files"].items():
            source = root / subject.test_overlay_root(owner)
            for name in names:
                case = source / subject.overlay_case_path(owner, name)
                case.parent.mkdir(parents=True, exist_ok=True)
                case.write_text("#!/bin/sh\nexit 0\n")
            lib = source / ("e2e/lib" if owner == "platform" else "lib")
            lib.mkdir(parents=True)
            (lib / "new-helper.py").write_text("new helper")
            if owner == "platform":
                runner = Path(__file__).resolve().parents[2] / "test/e2e"
                shutil.copy2(runner / "e2e", source / "e2e/e2e")
                shutil.copy2(runner / "lib/workspace.py", lib / "workspace.py")
                shutil.copy2(runner / "lib/demo_wheels.py", lib / "demo_wheels.py")
                shutil.copy2(runner / "lib/common.sh", lib / "common.sh")
            test_records[owner] = subject.tree_files(source)
        self.metadata = {"plan_id": subject.identity(self.plan), "arch": arch, "products": records,
                         "test_revisions": self.plan["test_revisions"],
                         "tests": test_records, "helpers": {}, "build_context": {"host": "x86_64"}}
        (root / "outputs.json").write_text(json.dumps(self.metadata))
        return root

    def compose(self, arch="x86_64", delta=None):
        return subject.compose(self.plan, arch, self.assets, delta or self.delta(arch), self.root / "workspaces" / arch)

    def add_kernel_checksum(self, arch, checksum=None):
        name = subject.archive_name("vmlinux", self.plan["baseline"]["units"]["vmlinux"]["version"], arch)
        raw = self.files[arch]["vmlinux"]
        if checksum is None:
            checksum = (hashlib.sha256(raw).hexdigest() + "  vmlinux\n").encode()
        record = archive(self.assets / name, {"bin/vmlinux": raw, "bin/vmlinux.sha256": checksum})
        next(item for item in self.plan["baseline"]["assets"] if item["name"] == name).update(record)
        return checksum

    def test_kernel_checksum_is_preserved_and_included_in_provenance(self):
        for arch in subject.ARCHES:
            expected = self.add_kernel_checksum(arch)
            provenance = self.compose(arch)
            workspace = self.root / "workspaces" / arch
            checksum = workspace / "bin/vmlinux.sha256"
            self.assertEqual(checksum.read_bytes(), expected)
            self.assertEqual(provenance["files"]["bin/vmlinux.sha256"], subject.digest(checksum))
            self.assertNotIn("vmlinux.sha256", provenance["products"])
            subject.verify_workspace(workspace, self.plan, arch)
            checksum.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "changed after composition"):
                subject.verify_workspace(workspace, self.plan, arch)

    def test_kernel_delta_refreshes_existing_checksum_after_identity_validation(self):
        self.plan["product_sources"]["vmlinux"] = {"guest-runtime": "e" * 40}
        for lane in self.plan["lanes"].values():
            lane["products"] = ["manifest-ctl", "vmlinux"]
        for arch in subject.ARCHES:
            old = self.add_kernel_checksum(arch)
            delta = self.delta(arch)
            changed = kernel(arch) + b"candidate kernel body"
            (delta / "bin/vmlinux").write_bytes(changed)
            self.metadata["products"]["vmlinux"]["sha256"] = hashlib.sha256(changed).hexdigest()
            (delta / "outputs.json").write_text(json.dumps(self.metadata))
            provenance = self.compose(arch, delta)
            workspace = self.root / "workspaces" / arch
            actual = (workspace / "bin/vmlinux.sha256").read_bytes()
            self.assertNotEqual(actual, old)
            self.assertEqual(actual, (hashlib.sha256(changed).hexdigest() + "  vmlinux\n").encode())
            self.assertEqual(provenance["products"]["vmlinux"]["origin"], "candidate")
            subject.verify_workspace(workspace, self.plan, arch)

    def test_invalid_baseline_checksum_is_not_hidden_by_kernel_delta(self):
        self.plan["product_sources"]["vmlinux"] = {"guest-runtime": "e" * 40}
        self.plan["lanes"]["x86_64"]["products"] = ["manifest-ctl", "vmlinux"]
        self.add_kernel_checksum("x86_64", b"0" * 64 + b"  vmlinux\n")
        with self.assertRaisesRegex(ValueError, "baseline kernel checksum differs"):
            self.compose()

    def preparation(self, *, mutate=False):
        spec = importlib.util.spec_from_file_location('ci_prepare', Path(__file__).with_name('prepare-artifacts.py'))
        prepare = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prepare)
        select_plan(self.plan, ['connector'])
        delta = self.delta()
        workspace = self.root / 'prepared/x86_64'
        original_run = subprocess.run
        def invoke(command, **kwargs):
            result = original_run(command, **kwargs)
            if mutate and 'prepare' in command and any(str(arg).endswith('/test/e2e/e2e') for arg in command):
                product = workspace / 'bin/manifest-ctl'
                product.write_bytes(b'changed during preparation')
                provenance = json.loads((workspace / 'provenance.json').read_text())
                provenance['files']['bin/manifest-ctl'] = subject.digest(product)
                (workspace / 'provenance.json').write_text(json.dumps(provenance))
            return result
        with patch.object(prepare.subprocess, 'run', side_effect=invoke):
            return prepare.prepare(self.plan, 'x86_64', self.assets, delta, workspace)

    def test_ci_preparation_calls_the_packaged_public_entry(self):
        prepared = self.preparation()
        self.assertEqual(prepared['prepared_cases'], ['network.tap.sh'])
        self.assertEqual(prepared['preparation_environment'], {'kind': 'native-host'})
        self.assertIn('ARCH', prepared['files'])
        self.assertEqual(prepared['files']['test/e2e/cases/network.tap.sh'],
                         hashlib.sha256(b'#!/bin/sh\nexit 0\n').hexdigest())
        self.assertFalse(list((self.root / 'prepared').glob('compose-*')))

    def test_public_preparation_cannot_reseal_changed_products(self):
        with self.assertRaisesRegex(ValueError, 'public preparation modified composed'):
            self.preparation(mutate=True)

    def test_two_isolated_architectures_retain_baseline_bytes_and_replace_complete_owner(self):
        for arch in subject.ARCHES:
            provenance = self.compose(arch)
            workspace = self.root / "workspaces" / arch
            self.assertEqual(provenance["products"]["store-ctl"]["sha256"],
                             hashlib.sha256(self.files[arch]["store-ctl"]).hexdigest())
            self.assertEqual(provenance["products"]["manifest-ctl"]["origin"], "candidate")
            self.assertFalse((workspace / "test/e2e/lib/accelerator/removed-helper.py").exists())
            self.assertTrue((workspace / "test/e2e/lib/accelerator/new-helper.py").exists())
            subject.verify_workspace(workspace, self.plan, arch)
        self.assertNotEqual(subject.digest(self.root / "workspaces/x86_64/bin/manifest-ctl"),
                            subject.digest(self.root / "workspaces/aarch64/bin/manifest-ctl"))

    def test_test_only_pin_changes_keep_every_product_byte_and_reject_mismatches(self):
        self.plan["product_sources"] = {}
        for lane in self.plan["lanes"].values():
            lane["products"] = []
        self.plan["test_revisions"]["accelerator"]["sha"] = "f" * 40
        for arch in subject.ARCHES:
            delta = self.delta(arch)
            wrong = copy.deepcopy(self.metadata)
            wrong["test_revisions"]["accelerator"]["sha"] = "c" * 40
            (delta / "outputs.json").write_text(json.dumps(wrong))
            with self.assertRaisesRegex(ValueError, "build test pins"):
                self.compose(arch, delta)
            (delta / "outputs.json").write_text(json.dumps(self.metadata))
            provenance = self.compose(arch, delta)
            for name, product in provenance["products"].items():
                self.assertEqual(product["origin"], "baseline")
                self.assertEqual(product["sha256"], hashlib.sha256(self.files[arch][name]).hexdigest())
            workspace = self.root / "workspaces" / arch
            self.assertEqual((workspace / "test/e2e/lib/accelerator/new-helper.py").read_text(), "new helper")
            self.assertEqual(provenance["test_revisions"], self.plan["test_revisions"])
            broken = json.loads((workspace / "provenance.json").read_text())
            broken["test_revisions"]["accelerator"]["sha"] = "c" * 40
            (workspace / "provenance.json").write_text(json.dumps(broken))
            with self.assertRaisesRegex(ValueError, "prepared test pins"):
                subject.verify_workspace(workspace, self.plan, arch)
        for wrong in ({}, {**test_revisions(), "orchestrator": {"repository": "other/repo", "sha": "c" * 40, "role": "release"}}):
            with self.assertRaisesRegex(ValueError, "test pin"):
                subject.validate_test_revisions(wrong)

    def test_release_and_later_baseline_preserve_independent_test_pins(self):
        spec = importlib.util.spec_from_file_location("resolver", Path(__file__).with_name("resolve-artifacts.py"))
        resolver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(resolver)
        binding_spec = importlib.util.spec_from_file_location("binding", Path(__file__).resolve().parents[2] / "release/bind-validation.py")
        binding = importlib.util.module_from_spec(binding_spec)
        binding_spec.loader.exec_module(binding)
        version, sha = self.plan["baseline"]["version"], self.plan["baseline"]["sha"]
        units = self.plan["baseline"]["units"]
        pins = {owner: "f" * 40 for owner in subject.OWNERS if owner != "platform"}
        manifest = "delivery: workbench-v1\nversion: " + version + "\ncomponents:\n" + "".join(
            f"  {unit}: {record['version']}\n" for unit, record in units.items())
        manifest += "test_revisions:\n" + "".join(f"  {owner}: {pin}\n" for owner, pin in pins.items())
        (self.root / "selection.tsv").write_text("".join(f"{unit}\t{units[unit]['version']}\n" for unit in subject.UNITS))
        pin_file = self.root / "test-revisions.json"
        pin_file.write_text(json.dumps(pins))
        receipts = self.workbench_stage()
        with patch.object(resolver, "source_text", return_value=manifest), patch.object(resolver, "public"), patch.object(resolver, "case_files", return_value=WORKBENCH_CASES), \
             patch.object(resolver.release, "tag_sha", side_effect=lambda repo, tag: sha if repo == resolver.PLATFORM else "c" * 40), \
             patch.dict(os.environ, {"RELEASE_VERSION": version, "PLATFORM_SOURCE_SHA": sha}):
            plan = resolver.exact_assets_plan("a" * 40, self.root)
            self.assertEqual(plan["case_files"], WORKBENCH_CASES)
            self.assertEqual(plan["test_overlays"], [])
            self.assertEqual(plan["product_sources"], {})
            selected = plan["lanes"]["x86_64"]["selection"]["cases"]
            self.assertIn("sandbox.lifecycle.sh", selected)
            self.assertFalse(any(name.endswith("run_all.sh") for name in selected))
            self.assertEqual(plan["test_revisions"]["orchestrator"]["sha"], "f" * 40)
            self.assertEqual(plan["baseline"]["units"]["orchestrator"]["sha"], "c" * 40)
            pin_file.write_text(json.dumps({**pins, "orchestrator": "c" * 40}))
            with self.assertRaisesRegex(ValueError, "staged test pins"):
                resolver.exact_assets_plan("a" * 40, self.root)
            pin_file.unlink()
            with self.assertRaises(FileNotFoundError):
                resolver.exact_assets_plan("a" * 40, self.root)
            pin_file.write_text(json.dumps(pins))
            results = {arch: architecture_result(plan, arch) for arch in subject.ARCHES}
            wrong = copy.deepcopy(results)
            wrong["aarch64"]["test_revisions"]["orchestrator"]["sha"] = "c" * 40
            with self.assertRaisesRegex(ValueError, "result test pins"):
                subject.collect_results(plan, wrong)
            notes = self.root / "release-notes.md"
            notes.write_text("Exact stage\n")
            binding.bind(self.root, plan, subject.collect_results(plan, results), workbench_results(plan, receipts))
            published = json.loads(resolver.PROFILE_BINDING.search(notes.read_text())[1])
            published['registry'] = registry_binding(version, published['workbench'])
            notes.write_text("<!-- kuasar-integration-validation " + json.dumps(published) + " -->")
            state = {"tag_name": version, "target_commitish": sha, "draft": False, "prerelease": False,
                "id": 1, "body": notes.read_text(), "assets": [dict(record, id=index, state="uploaded")
                    for index, record in enumerate(plan["baseline"]["assets"], 1)]}
            run = {"id": 1, "status": "completed", "conclusion": "success", "html_url": "https://example.invalid/run/1",
                   "display_title": resolver.release.aggregate_run_title(version, sha)}
            with patch.object(resolver.release, "api_optional", return_value=state), \
                 patch.object(resolver.release, "aggregate_runs", return_value=[run]):
                baseline = resolver.aggregate(version)
                self.assertEqual(baseline["test_revisions"], plan["test_revisions"])
                original_body, original_assets = state["body"], state["assets"]
                for old_cases in ({}, {owner: names for owner, names in CASES.items() if owner != "platform"}):
                    historical = json.loads(resolver.PROFILE_BINDING.search(original_body)[1])
                    for field in ('delivery', 'workbench', 'registry'):
                        historical.pop(field)
                    historical['assets'] = {name: digest for name, digest in historical['assets'].items() if not name.startswith('workbench-')}
                    state['assets'] = [row for row in original_assets if not row['name'].startswith('workbench-')]
                    for arch, result in historical["architectures"].items():
                        result.pop("selection")
                        result["profile"] = resolver.historical_profile(arch, old_cases)
                    state["body"] = "<!-- kuasar-integration-validation " + json.dumps(historical) + " -->"
                    with patch.object(resolver, "historical_case_files", return_value=old_cases), patch.object(
                            resolver, 'source_text', return_value=manifest.replace('delivery: workbench-v1\n', '')):
                        self.assertEqual(resolver.aggregate(version)["test_revisions"], plan["test_revisions"])
                        entries = historical["architectures"]["x86_64"]["profile"]["cases"]
                        if old_cases:
                            entries.remove("test/e2e/sandboxer/cases/sandbox.lifecycle.sh")
                        else:
                            entries.pop()
                        state["body"] = "<!-- kuasar-integration-validation " + json.dumps(historical) + " -->"
                        with self.assertRaisesRegex(ValueError, "predeclared architecture profile"):
                            resolver.aggregate(version)
                state["body"], state["assets"] = original_body, original_assets
                with patch.object(resolver, "baseline", return_value=baseline), \
                     patch.object(resolver, "changed_files", return_value=["docs/ci.md"]), \
                     patch.object(resolver, "candidate_case_names", return_value=[]), \
                     patch.dict(os.environ, {"CANDIDATE_REPOSITORY": resolver.PLATFORM, "CANDIDATE_PR": "1",
                        "CANDIDATE_SHA": "e" * 40, "CANDIDATE_BASE_SHA": sha, "CANDIDATE_HEAD_SHA": "d" * 40,
                        "CANDIDATE_BASE_REF": "main", "COMPANION_CANDIDATES": "[]"}):
                    candidate = resolver.source_plan("a" * 40)
                self.assertEqual(candidate["test_revisions"]["orchestrator"], plan["test_revisions"]["orchestrator"])
                self.assertEqual(candidate["sources"]["orchestrator"]["sha"], "c" * 40)
                self.assertEqual(candidate["lanes"]["x86_64"]["products"], [])
                recorded = json.loads(resolver.PROFILE_BINDING.search(state["body"])[1])
                recorded["test_revisions"]["orchestrator"]["sha"] = "c" * 40
                state["body"] = "<!-- kuasar-integration-validation " + json.dumps(recorded) + " -->"
                with self.assertRaisesRegex(ValueError, "published test pins"):
                    resolver.aggregate(version)

    def test_baseline_and_candidate_tampering_fail(self):
        delta = self.delta()
        with (delta / "bin/manifest-ctl").open("ab") as file:
            file.write(b"tamper")
        with self.assertRaisesRegex(ValueError, "candidate digest"):
            self.compose(delta=delta)
        asset = self.assets / self.plan["baseline"]["assets"][0]["name"]
        with asset.open("ab") as file:
            file.write(b"tamper")
        with self.assertRaisesRegex(ValueError, "baseline asset digest"):
            self.compose(delta=delta)

    def test_unplanned_product_and_wrong_architecture_fail(self):
        delta = self.delta()
        (delta / "bin/connector-ctl").write_bytes(elf("x86_64", "unowned"))
        with self.assertRaisesRegex(ValueError, "undeclared candidate"):
            self.compose(delta=delta)
        (delta / "bin/connector-ctl").unlink()
        path = delta / "bin/manifest-ctl"
        path.write_bytes(elf("aarch64", "wrong target"))
        self.metadata["products"]["manifest-ctl"]["sha256"] = subject.digest(path)
        (delta / "outputs.json").write_text(json.dumps(self.metadata))
        with self.assertRaisesRegex(ValueError, "wrong ELF architecture"):
            self.compose(delta=delta)

    def test_candidate_init_must_reach_embedded_runtime(self):
        self.plan["lanes"]["x86_64"]["products"] = ["sandbox-init"]
        self.plan["product_sources"]["sandbox-init"] = {"sandboxer": "f" * 40}
        with self.assertRaisesRegex(ValueError, "embedded init differs"):
            self.compose()

    def test_usage_probe_is_a_required_target_helper(self):
        select_plan(self.plan, ["sandboxer"])
        delta = self.delta()
        with self.assertRaisesRegex(ValueError, "helper selection differs"):
            self.compose(delta=delta)
        helpers = subject.planned_helpers(self.plan["lanes"]["x86_64"]["selection"])
        self.assertEqual(helpers["usage-probe"], "sandboxer")
        (delta / "helpers").mkdir()
        self.metadata["helpers"] = {}
        for name, owner in helpers.items():
            path = delta / "helpers" / name
            path.write_bytes(elf("x86_64", name))
            path.chmod(0o755)
            self.metadata["helpers"][name] = {"sha256": subject.digest(path), "source_sha":
                self.plan["framework_sha"] if owner == "framework" else self.plan["test_revisions"][owner]["sha"]}
        (delta / "outputs.json").write_text(json.dumps(self.metadata))
        provenance = self.compose(delta=delta)
        probe = self.root / "workspaces/x86_64/fixtures/bin/usage-probe"
        self.assertEqual(subject.digest(probe), provenance["helpers"]["usage-probe"]["sha256"])
        probe.unlink()
        with self.assertRaisesRegex(ValueError, "prepared workspace changed"):
            subject.verify_workspace(self.root / "workspaces/x86_64", self.plan, "x86_64")

    def test_proxy_helper_requires_exact_test_pin_and_survives_preparation(self):
        select_plan(self.plan, ["orchestrator"])
        self.plan["test_revisions"]["orchestrator"]["sha"] = "f" * 40
        delta = self.delta()
        helpers = subject.planned_helpers(self.plan["lanes"]["x86_64"]["selection"])
        self.assertEqual(helpers["custom-proxy"], "orchestrator")
        self.assertNotIn("custom-proxy", subject.planned_helpers(self.plan["lanes"]["aarch64"]["selection"]))
        with self.assertRaisesRegex(ValueError, "helper selection differs"):
            self.compose(delta=delta)
        (delta / "helpers").mkdir()
        self.metadata["helpers"] = {}
        for name, owner in helpers.items():
            path = delta / "helpers" / name
            path.write_bytes(elf("x86_64", name))
            path.chmod(0o755)
            self.metadata["helpers"][name] = {"sha256": subject.digest(path), "source_sha":
                self.plan["framework_sha"] if owner == "framework" else self.plan["test_revisions"][owner]["sha"]}
        self.assertEqual(helpers['node-ctl-runner-test'], 'orchestrator')
        runner_helper = self.metadata['helpers']['node-ctl-runner-test']
        runner_helper['source_sha'] = 'c' * 40
        (delta / 'outputs.json').write_text(json.dumps(self.metadata))
        with self.assertRaisesRegex(ValueError, 'test helper identity mismatch: node-ctl-runner-test'):
            self.compose(delta=delta)
        runner_helper['source_sha'] = 'f' * 40
        helper = self.metadata["helpers"]["custom-proxy"]
        helper["source_sha"] = "c" * 40
        (delta / "outputs.json").write_text(json.dumps(self.metadata))
        with self.assertRaisesRegex(ValueError, "test helper identity mismatch: custom-proxy"):
            self.compose(delta=delta)
        helper["source_sha"] = "f" * 40
        (delta / "outputs.json").write_text(json.dumps(self.metadata))
        provenance = self.compose(delta=delta)
        prepared = self.root / "workspaces/x86_64/fixtures/bin/custom-proxy"
        self.assertEqual(subject.digest(prepared), provenance["helpers"]["custom-proxy"]["sha256"])
        runner_prepared = self.root / 'workspaces/x86_64/fixtures/bin/node-ctl-runner-test'
        self.assertEqual(subject.digest(runner_prepared), provenance['helpers']['node-ctl-runner-test']['sha256'])
        prepared.unlink()
        with self.assertRaisesRegex(ValueError, "prepared workspace changed"):
            subject.verify_workspace(self.root / "workspaces/x86_64", self.plan, "x86_64")

    def test_guest_execution_binds_init_to_the_validated_runtime_bytes(self):
        # A legal unchanged runtime may embed a different init revision than the
        # separately published sandboxer unit. Candidate-init equality is checked
        # separately; execution must retain this baseline's embedded identity.
        files = dict(self.files["x86_64"])
        files["sandbox-init"] = elf("x86_64", "separately published init")
        name = subject.archive_name("sandboxer", "v1.2.3", "x86_64")
        record = archive(self.assets / name, {"bin/" + name: data for name, data in files.items()
                                            if subject.PRODUCTS[name] == "sandboxer"})
        self.plan["baseline"]["assets"] = [record if entry["name"] == name else entry
                                           for entry in self.plan["baseline"]["assets"]]
        provenance = self.compose()
        self.assertNotEqual(provenance["embedded"]["init"], provenance["products"]["sandbox-init"]["sha256"])
        spec = importlib.util.spec_from_file_location("prepared_inputs", Path(__file__).resolve().parents[2] / "test/e2e/lib/workspace.py")
        inputs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inputs)
        environment = inputs.case_environment(self.root / "workspaces/x86_64", provenance, "sandbox.lifecycle.sh")
        self.assertEqual(environment["KUASAR_EXPECTED_RUNTIME_INIT_SHA256"], provenance["embedded"]["init"])

    def test_missing_selected_inputs_cannot_fall_back_to_source(self):
        self.plan["baseline"]["assets"] = [record for record in self.plan["baseline"]["assets"]
                                                  if "aarch64" not in record["name"]]
        with self.assertRaisesRegex(ValueError, "explicit ARM initialization"):
            self.compose("aarch64")
        delta = self.delta()
        (delta / "test/e2e/accelerator/cases/storage.cache.sh").unlink()
        self.metadata["tests"]["accelerator"].pop("cases/storage.cache.sh")
        (delta / "outputs.json").write_text(json.dumps(self.metadata))
        with self.assertRaisesRegex(ValueError, "missing pinned case"):
            self.compose(delta=delta)

    def test_orchestrator_config_and_app_are_product_inputs(self):
        expected = sorted(name for name, unit in subject.PRODUCTS.items() if unit == "orchestrator")
        for path in ("config/config.go", "app/proxy/proxy.go"):
            self.assertEqual(subject.changed_products({"orchestrator": [path]}), expected)
        for path in ("docs/orchestrator.md", "test/e2e/run_all.sh", "app/proxy/proxy_test.go"):
            self.assertEqual(subject.changed_products({"orchestrator": [path]}), [])

    def test_linked_product_closure_and_platform_test_only_change(self):
        self.assertEqual(subject.changed_products({"platform": ["test/e2e/run_all.sh"]}), [])
        self.assertEqual(subject.changed_products({"accelerator": ["docs/accelerator.md"]}), [])
        products = subject.changed_products({"sandboxer": ["cmd/sandbox-init/main.go"]})
        self.assertIn("sandbox-runtime.bundle", products)
        self.assertNotIn("node-ctl", products)
        products = subject.changed_products({"accelerator": ["pkg/flatten/flatten.go"]})
        self.assertIn("flatten-ctl", products)
        self.assertIn("sandbox-runtime.bundle", products)
        self.assertNotIn("sandbox-ctl", products)

    def test_results_require_both_exact_architectures_and_predeclared_suites(self):
        results = {arch: architecture_result(self.plan, arch) for arch in subject.ARCHES}
        subject.collect_results(self.plan, results)
        with self.assertRaisesRegex(ValueError, "both architecture"):
            subject.collect_results(self.plan, {"x86_64": results["x86_64"]})
        results["aarch64"] = copy.deepcopy(results["aarch64"])
        results["aarch64"]["selection"]["cases"] = []
        with self.assertRaisesRegex(ValueError, "did not pass"):
            subject.collect_results(self.plan, results)

    def test_shards_require_all_selected_cases_and_one_prepared_identity(self):
        select_plan(self.plan, ["platform"])
        arch = "x86_64"
        results = {shard: {"arch": arch, "shard": shard, "plan_id": subject.identity(self.plan),
                          "test_revisions": self.plan["test_revisions"],
                          "cases": cases, "conclusion": "success", "provenance_sha256": "a" * 64,
                          "timings": [{"case": case, "exit_code": 0, "wall_seconds": 0.1} for case in cases]}
                   for shard, cases in subject.shards(self.plan["lanes"][arch]["selection"]).items()}
        for shard, record in results.items():
            if record['cases']:
                record['preparation_environment'] = {'kind': 'clean-container', 'verified': True,
                                                    'compilers': [], 'component_source_trees': [], 'image_id': 'sha256:' + 'd' * 64}
            if subject.requires_clean_runtime(arch, shard):
                record['environment'] = {'kind': 'clean-container', 'verified': True,
                                         'compilers': [], 'component_source_trees': [], 'image_id': 'sha256:' + 'e' * 64}
        results['performance'] = {'arch': arch, 'plan_id': subject.identity(self.plan),
                                  'test_revisions': self.plan['test_revisions'], 'conclusion': 'success',
                                  'provenance_sha256': 'a' * 64, 'checks': ['working-set-smoke'],
                                  'timings': [{'case': 'working-set-smoke', 'exit_code': 0, 'wall_seconds': 0.1}]}
        subject.collect_shard_results(self.plan, arch, results)
        missing_prepare = copy.deepcopy(results)
        missing_prepare['storage']['preparation_environment']['verified'] = False
        with self.assertRaisesRegex(ValueError, 'source/compiler-free'):
            subject.collect_shard_results(self.plan, arch, missing_prepare)
        missing_clean = copy.deepcopy(results)
        missing_clean['storage']['environment']['verified'] = False
        with self.assertRaisesRegex(ValueError, 'source/compiler-free'):
            subject.collect_shard_results(self.plan, arch, missing_clean)
        incomplete = dict(results)
        del incomplete["sandbox"]
        with self.assertRaisesRegex(ValueError, "validation results"):
            subject.collect_shard_results(self.plan, arch, incomplete)
        results["orchestrator"]["provenance_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "different prepared"):
            subject.collect_shard_results(self.plan, arch, results)

    def test_transport_rejects_unsafe_inputs_and_preserves_executable_mode(self):
        path = self.root / "transport.tar.gz"
        with tarfile.open(path, "w:gz") as output:
            entry = tarfile.TarInfo("bin/tool")
            entry.size, entry.mode = len(b"executable"), 0o755
            output.addfile(entry, io.BytesIO(b"executable"))
        destination = self.root / "transport"
        transport.extract(path, destination)
        self.assertEqual((destination / "bin/tool").read_bytes(), b"executable")
        self.assertEqual((destination / "bin/tool").stat().st_mode & 0o777, 0o755)
        for name, kind, mode in (("../escape", tarfile.REGTYPE, 0o644),
                                 ("link", tarfile.SYMTYPE, 0o644),
                                 ("writable", tarfile.REGTYPE, 0o777)):
            with self.subTest(name=name), tarfile.open(path, "w:gz") as output:
                entry = tarfile.TarInfo(name)
                entry.type, entry.mode, entry.linkname = kind, mode, "/outside"
                output.addfile(entry)
            with self.assertRaises(ValueError):
                transport.extract(path, self.root / "rejected")
            self.assertFalse((self.root / "rejected").exists())

    def test_publication_binds_both_results_to_unchanged_stage_bytes(self):
        spec = importlib.util.spec_from_file_location("binding", Path(__file__).resolve().parents[2] / "release/bind-validation.py")
        binding = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(binding)
        receipts = self.workbench_stage()
        workbench = workbench_results(self.plan, receipts)
        results = {arch: architecture_result(self.plan, arch) for arch in subject.ARCHES}
        validation = subject.collect_results(self.plan, results)
        notes = self.root / "release-notes.md"
        (self.root / "test-revisions.json").write_text(json.dumps(
            {owner: record["sha"] for owner, record in self.plan["test_revisions"].items() if owner != "platform"}))
        notes.write_text("Original release notes\n")
        hashes = subject.tree_files(self.assets)
        binding.bind(self.root, self.plan, validation, workbench)
        self.assertEqual(subject.tree_files(self.assets), hashes)
        self.assertTrue(notes.read_text().startswith("Original release notes\n"))
        notes.write_text("Original release notes\n")
        asset = self.assets / next(iter(hashes))
        with asset.open("ab") as output:
            output.write(b"changed after validation")
        with self.assertRaisesRegex(ValueError, "publisher bytes differ"):
            binding.bind(self.root, self.plan, validation, workbench)
        self.assertEqual(notes.read_text(), "Original release notes\n")

    def test_connector_source_failures_block_connector_and_platform(self):
        spec = importlib.util.spec_from_file_location("source_checks", Path(__file__).with_name("run-source-checks.py"))
        source_checks = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(source_checks)
        self.plan["sources"] = {owner: {"repository": "kuasar-sandbox/" + owner, "sha": "c" * 40}
                                for owner in subject.OWNERS}
        def checkout(repository, sha, destination):
            destination.mkdir()
        def execute(command, *, cwd, env):
            code = 33 if cwd.name == "connector" and command == ["bash", "scripts/ci-source-checks.sh"] else 0
            return subprocess.CompletedProcess(command, code)
        for owner in ("connector", "platform"):
            select_plan(self.plan, [owner])
            result = self.root / (owner + "-source-result.json")
            with patch.object(source_checks.build, "checkout", side_effect=checkout), \
                 patch.object(source_checks.subprocess, "run", side_effect=execute):
                with self.assertRaisesRegex(ValueError, "required source check failed: connector-unit-race-vet"):
                    source_checks.execute(self.plan, self.root / (owner + "-sources"), result)
            record = json.loads(result.read_text())
            self.assertEqual(record["conclusion"], "failure")
            self.assertEqual(record["checks"][-1]["name"], "connector-unit-race-vet")
            self.assertEqual(record["checks"][-1]["exit_code"], 33)


if __name__ == "__main__":
    unittest.main()
