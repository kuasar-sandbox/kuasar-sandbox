"""Reject unusable or misbound prebuilt helper packages before publication."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('e2e_package', Path(__file__).with_name('validate-e2e-package.py'))
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)
from test_fixtures import make_demo_wheelhouse


class PrebuiltPackage(unittest.TestCase):
    def setUp(self):
        self.pins = {owner: 'a' * 40 for owner in package.artifacts.OWNERS if owner != 'platform'}
        self.metadata, self.files = {}, {'test/e2e/cases/basic.fixture.sh': (b'exit 0\n', 0o644)}
        self.directories = []
        for arch, machine in [('x86_64', 62), ('aarch64', 183)]:
            names = ['zot', 'versitygw', 'custom-proxy', 'telemetry-grpc-probe', 'usage-probe', 'cgroup-fork-probe']
            helpers = {}
            for name in names:
                data = bytearray(64)
                data[:7] = b'\x7fELF\x02\x01\x01'
                struct.pack_into('<H', data, 18, machine)
                self.files[f'test/e2e/helpers/{arch}/{name}'] = (bytes(data), 0o755)
                helpers[name] = {'sha256': hashlib.sha256(data).hexdigest(),
                                 'source_sha': ('b' if name in {'zot', 'versitygw'} else 'a') * 40}
            self.metadata[arch] = {'arch': arch, 'framework_sha': 'b' * 40,
                                   'test_revisions': copy.deepcopy(self.pins), 'helpers': helpers}

    def validate(self, expected=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'package.tar.gz'
            files = self.files | {f'test/e2e/helpers/{arch}/helpers.json': (json.dumps(record).encode(), 0o644)
                                  for arch, record in self.metadata.items()}
            with tarfile.open(path, 'w:gz') as output:
                for name in self.directories:
                    member = tarfile.TarInfo(name)
                    member.type, member.mode = tarfile.DIRTYPE, 0o755
                    output.addfile(member)
                for name, (data, mode) in files.items():
                    member = tarfile.TarInfo(name)
                    member.size, member.mode = len(data), mode
                    output.addfile(member, io.BytesIO(data))
            package.validate(path, self.pins if expected is None else expected)

    def test_both_architectures_have_complete_exact_helpers(self):
        self.validate()

    def test_arm_cgroup_probe_is_required_in_new_release_packages(self):
        del self.metadata['aarch64']['helpers']['cgroup-fork-probe']
        del self.files['test/e2e/helpers/aarch64/cgroup-fork-probe']
        with self.assertRaisesRegex(ValueError, 'missing or unexpected prebuilt E2E helper'):
            self.validate()

    def test_arm_probe_architecture_hash_and_source_are_checked(self):
        name = 'test/e2e/helpers/aarch64/cgroup-fork-probe'
        data, mode = self.files[name]
        self.files[name] = self.files[name.replace('aarch64', 'x86_64')]
        with self.assertRaisesRegex(ValueError, 'binary architecture'):
            self.validate()
        self.files[name] = (data + b'tamper', mode)
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.validate()
        self.files[name] = (data, mode)
        self.metadata['aarch64']['helpers']['cgroup-fork-probe']['source_sha'] = 'c' * 40
        with self.assertRaisesRegex(ValueError, 'source identity'):
            self.validate()

    def test_owner_guides_and_their_directories_are_packaged(self):
        for owner in ('accelerator', 'guest-runtime'):
            self.directories += [f'test/e2e/{owner}', f'test/e2e/{owner}/guides']
            for name in ('README.md', 'README_zh.md', 'guides/preparation.md'):
                self.files[f'test/e2e/{owner}/{name}'] = (b'# Prepared E2E\n', 0o644)
        self.validate()

    def test_guides_do_not_admit_legacy_or_executable_owner_content(self):
        self.files['test/e2e/accelerator/README.md'] = (b'# Prepared E2E\n', 0o644)
        for name, mode in [('accelerator/run_all.sh', 0o755),
                           ('accelerator/cases/storage.cache.sh', 0o644),
                           ('accelerator/notes.txt', 0o644),
                           ('accelerator/execute.md', 0o755),
                           ('unknown/README.md', 0o644)]:
            with self.subTest(name=name):
                path = 'test/e2e/' + name
                self.files[path] = (b'content\n', mode)
                with self.assertRaisesRegex(ValueError, 'superseded owner'):
                    self.validate()
                del self.files[path]

    def test_unrelated_empty_owner_directory_is_rejected(self):
        self.files['test/e2e/accelerator/README.md'] = (b'# Prepared E2E\n', 0o644)
        self.directories = ['test/e2e/accelerator/cases']
        with self.assertRaisesRegex(ValueError, 'superseded owner'):
            self.validate()

    def add_demo(self):
        self.files['test/e2e/cases/basic.demo.sh'] = (b'exit 0\n', 0o644)
        with tempfile.TemporaryDirectory() as directory:
            demo = Path(directory)
            make_demo_wheelhouse(demo, demo / 'wheels')
            for path in demo.rglob('*'):
                if path.is_file():
                    self.files['test/demo/' + str(path.relative_to(demo))] = (path.read_bytes(), 0o644)

    def test_demo_requires_both_architectures_of_complete_wheel_inputs(self):
        self.add_demo()
        self.validate()
        del self.files['test/demo/wheels/aarch64/manifest.json']
        with self.assertRaisesRegex(ValueError, 'missing Demo wheel manifest'):
            self.validate()

    def test_demo_missing_and_tampered_dependency_cannot_be_published(self):
        self.add_demo()
        name = 'test/demo/wheels/x86_64/prepared_dependency-1.0-py3-none-any.whl'
        data, mode = self.files[name]
        self.files[name] = (data + b'tamper', mode)
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.validate()
        del self.files[name]
        with self.assertRaisesRegex(ValueError, 'missing or undeclared Demo wheels'):
            self.validate()

    def test_selected_pin_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'pins differ'):
            self.validate(self.pins | {'orchestrator': 'c' * 40})

    def test_architecture_source_sets_must_agree(self):
        self.metadata['aarch64']['framework_sha'] = 'c' * 40
        with self.assertRaisesRegex(ValueError, 'source sets differ'):
            self.validate()

    def test_wrong_architecture_and_missing_helper_are_rejected(self):
        name = 'test/e2e/helpers/aarch64/usage-probe'
        self.files[name] = self.files[name.replace('aarch64', 'x86_64')]
        with self.assertRaisesRegex(ValueError, 'binary architecture'):
            self.validate()
        del self.files[name]
        with self.assertRaisesRegex(ValueError, 'undeclared helper'):
            self.validate()

    def test_changed_bytes_and_nonexecutable_mode_are_rejected(self):
        name = 'test/e2e/helpers/x86_64/custom-proxy'
        data, _ = self.files[name]
        self.files[name] = (data + b'tamper', 0o755)
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.validate()
        self.files[name] = (data, 0o644)
        with self.assertRaisesRegex(ValueError, 'permissions'):
            self.validate()

    def test_helper_source_identity_is_bound(self):
        self.metadata['x86_64']['helpers']['usage-probe']['source_sha'] = 'c' * 40
        with self.assertRaisesRegex(ValueError, 'source identity'):
            self.validate()

    def test_unknown_architecture_and_owner_runner_are_rejected(self):
        name = 'test/e2e/helpers/ppc64le/tool'
        self.files[name] = (b'not a selected architecture', 0o755)
        with self.assertRaisesRegex(ValueError, 'unknown helper architecture'):
            self.validate()
        del self.files[name]
        self.files['test/e2e/connector/run_all.sh'] = (b'exit 0', 0o755)
        with self.assertRaisesRegex(ValueError, 'superseded owner'):
            self.validate()


if __name__ == '__main__':
    unittest.main()
