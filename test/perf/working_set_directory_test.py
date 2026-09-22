"""Exercise the benchmark's actual workspace assignment without KVM setup."""
import copy
import csv
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
import subprocess
import sys
import unittest


class WorkingSetEnvironmentTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "prepared/x86_64"
        self.bindir = self.root / "bin"
        self.bindir.mkdir(parents=True)
        for name in ("cache-ctl", "cloud-hypervisor", "flatten-ctl", "manifest-ctl", "mkfs.erofs",
                     "sandbox-ctl", "sandbox-init", "sandbox-runtime.bundle", "store-ctl", "vmlinux"):
            (self.bindir / name).write_bytes(name.encode())
        hypervisor = self.bindir / "cloud-hypervisor"
        hypervisor.write_text('#!/bin/sh\nprintf "test hypervisor\\n"\n')
        hypervisor.chmod(0o755)
        commands = self.root.parent / "commands"
        commands.mkdir()
        self.git_calls = commands / "git-calls"
        self.image = "sha256:" + "e" * 64
        for name, body in {
            "git": 'printf "%s\\n" "$*" >> "$GIT_CALLS"\nexit 128\n',
            "docker": 'printf "%s\\n" "$TEST_IMAGE_ID"\n',
        }.items():
            path = commands / name
            path.write_text("#!/bin/sh\n" + body)
            path.chmod(0o755)
        self.environment = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                                GIT_CALLS=str(self.git_calls), TEST_IMAGE_ID=self.image)
        self.environment.pop("KUASAR_ARTIFACT_E2E", None)
        self.environment.pop("KUASAR_REVISION_MANIFEST", None)
        self.provenance = {
            "plan_id": "a" * 64, "framework_sha": "b" * 40, "arch": "x86_64",
            "test_revisions": {
                owner: {"repository": "kuasar-sandbox/" + ("kuasar-sandbox" if owner == "platform" else owner),
                        "sha": hashlib.sha1(owner.encode()).hexdigest(), "role": "candidate"}
                for owner in ("accelerator", "connector", "guest-runtime", "orchestrator", "platform", "sandboxer")
            },
            "products": {
                "cache-ctl": {"origin": "baseline", "unit": "accelerator", "sha256": "c" * 64,
                              "sources": {"repository": "kuasar-sandbox/accelerator", "sha": "d" * 40,
                                          "version": "v1.2.3"}},
                "flatten-ctl": {"origin": "candidate", "unit": "runtime", "sha256": "f" * 64,
                                "sources": {"kuasar-sandbox/accelerator": "1" * 40,
                                            "kuasar-sandbox/guest-runtime": "2" * 40}},
            },
        }
        self.manifest = self.root / "provenance.json"
        self.output = self.root.parent / "environment.json"
        source = Path(__file__).with_name("sandbox-perf-working-set.sh").read_text()
        block = re.search(r'^python3 - "\$ENVIRONMENT"[^\n]*<<\'PY\'\n(.*?)^PY$', source, re.M | re.S)
        self.assertIsNotNone(block)
        self.code = block[1]

    def run_environment(self, artifact=False):
        environment = dict(self.environment)
        if artifact:
            environment["KUASAR_ARTIFACT_E2E"] = "1"
        # Execute the actual metadata block; only external host probes are stubs.
        return subprocess.run([sys.executable, "-c", self.code, str(self.output), str(self.root),
                               str(self.bindir), str(self.bindir / "vmlinux"), self.image, "1", "A B C D",
                               "16777216", "targeted-posix-fadvise-dontneed"],
                              env=environment, text=True, capture_output=True)

    def test_artifact_workspace_records_product_and_test_identities_without_git(self):
        self.manifest.write_text(json.dumps(self.provenance))
        self.environment["KUASAR_REVISION_MANIFEST"] = str(self.root / "absent-source-manifest.tsv")
        result = self.run_environment(artifact=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.output.read_text())
        for key, value in self.provenance.items():
            self.assertEqual(report["prepared_inputs"][key], value)
        self.assertEqual(report["prepared_inputs"]["provenance_sha256"],
                         hashlib.sha256(self.manifest.read_bytes()).hexdigest())
        for owner, pin in self.provenance["test_revisions"].items():
            self.assertEqual(report["repositories"][owner],
                             {"sha": pin["sha"], "dirty": False, "role": pin["role"],
                              "source": "prepared-test-revision"})
        self.assertEqual(report["inputs"]["image_id"], self.image)
        self.assertEqual(report["artifacts"]["kernel_sha256"], hashlib.sha256(b"vmlinux").hexdigest())
        for name, digest in report["artifacts"]["binaries_sha256"].items():
            self.assertEqual(digest, hashlib.sha256((self.bindir / name).read_bytes()).hexdigest())
        self.assertFalse(self.git_calls.exists())

    def test_artifact_metadata_failure_never_falls_back_to_git(self):
        for defect in ("missing-provenance", "missing-owner", "invalid-sha"):
            with self.subTest(defect=defect):
                provenance = copy.deepcopy(self.provenance)
                if defect == "missing-owner":
                    del provenance["test_revisions"]["accelerator"]
                elif defect == "invalid-sha":
                    provenance["test_revisions"]["accelerator"]["sha"] = "not-an-exact-sha"
                if defect != "missing-provenance":
                    self.manifest.write_text(json.dumps(provenance))
                result = self.run_environment(artifact=True)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.git_calls.exists(), result.stderr)

    def test_source_manifest_still_supplies_exact_revisions(self):
        source_manifest = self.root / "source-set.tsv"
        with source_manifest.open("w", newline="") as output:
            writer = csv.writer(output, delimiter="\t")
            writer.writerow(("repository", "requested_ref", "resolved_sha", "role"))
            for pin in self.provenance["test_revisions"].values():
                writer.writerow((pin["repository"], "main", pin["sha"], "candidate"))
        self.environment["KUASAR_REVISION_MANIFEST"] = str(source_manifest)
        result = self.run_environment()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.output.read_text())
        self.assertNotIn("prepared_inputs", report)
        for owner, pin in self.provenance["test_revisions"].items():
            self.assertEqual(report["repositories"][owner],
                             {"sha": pin["sha"], "dirty": False, "requested_ref": "main",
                              "role": "candidate", "source": "revision-manifest"})
        self.assertFalse(self.git_calls.exists())

    def test_missing_source_checkout_still_fails(self):
        result = self.run_environment()
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot resolve exact revision for accelerator", result.stderr)
        self.assertIn("exit status 128", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertIn("rev-parse --show-toplevel", self.git_calls.read_text())


class WorkingSetDirectoryTest(unittest.TestCase):
    def selected_template(self, tmpdir):
        source = Path(__file__).with_name("sandbox-perf-working-set.sh").read_text()
        lines = [line for line in source.splitlines() if line.startswith("WORK=")]
        self.assertEqual(len(lines), 1)
        # Stub only mktemp: assert the real assignment preserves one quoted
        # template argument, then expose it without creating a host directory.
        script = 'set -eu\nmktemp() { [ "$#" = 2 ] && [ "$1" = -d ]; printf "%s" "$2"; }\n'
        script += lines[0] + '\nprintf "%s" "$WORK"\n'
        env = dict(os.environ)
        env.pop("TMPDIR", None)
        if tmpdir is not None:
            env["TMPDIR"] = tmpdir
        return subprocess.check_output(["bash", "-c", script], env=env, text=True)

    def test_unset_and_empty_default_to_disk_directory(self):
        for value in (None, ""):
            with self.subTest(value=value):
                self.assertEqual(self.selected_template(value), "/var/tmp/perf-working-set-XXXXXX")

    def test_explicit_directory_is_respected_and_quoted(self):
        self.assertEqual(self.selected_template("/custom path"), "/custom path/perf-working-set-XXXXXX")


class WorkingSetDiffTest(unittest.TestCase):
    source_path = Path(__file__).with_name("sandbox-perf-working-set.sh")

    def prepare(self, mode, reused=False):
        source = self.source_path.read_text()
        # Execute the real helpers, including write_config's mode dispatch.
        start = re.search(r"^(?:format|prepare)_diff\(\) ", source, re.MULTILINE)
        self.assertIsNotNone(start)
        helpers = source[start.start():source.index("\nstart_sandbox()")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diffs = root / "diffs"
            diffs.mkdir()
            if reused:
                for name in ("root", "scratch", "dataset"):
                    (diffs / f"{name}.ext4").write_bytes(b"stale" * 1024)
            # Only stub the formatter: record its actual argv, not the helper.
            # Real sparse-file creation remains observable on the host.
            script = "set -euo pipefail\n"
            script += 'mkfs.ext4() { printf "%s\\n" "$*" >> "$FORMAT_CALLS"; }\n'
            script += helpers
            script += '\nwrite_config "$1/config.yaml" "$1/diffs" off'
            if mode is not None:
                script += ' "$2"'
            env = dict(os.environ, FORMAT_CALLS=str(root / "formats"),
                       TAP_NAME="test-tap", GUEST_HTTP_IP="169.254.1.1",
                       VMLINUX="/test/kernel", BIN="/test/bin",
                       ROOT_KEY="root-key", DATASET_KEY="dataset-key")
            subprocess.run(["bash", "-c", script, "diff-test", tmp, mode or ""],
                           env=env, check=True, capture_output=True, text=True)
            calls = (root / "formats").read_text().splitlines() if (root / "formats").exists() else []
            files = {}
            for name in ("root", "scratch", "dataset"):
                path = diffs / f"{name}.ext4"
                stat = path.stat()
                with path.open("rb") as stream:
                    files[name] = (stat.st_size, stat.st_blocks, stream.read(4096))
            return calls, files

    def test_cold_formats_all_disks_without_journal(self):
        calls, files = self.prepare("cold")
        self.assertEqual(len(calls), 3)
        for call in calls:
            self.assertIn("-O ^has_journal", call)
        self.assertEqual({name: value[0] for name, value in files.items()},
                         {"root": 1 << 30, "scratch": 512 << 20, "dataset": 512 << 20})

    def test_restore_inherits_snapshot_without_formatting(self):
        for mode in (None, "restore"):
            with self.subTest(mode=mode):
                calls, files = self.prepare(mode)
                self.assertEqual(calls, [])
                self.assertEqual({name: value[0] for name, value in files.items()},
                                 {"root": 1 << 30, "scratch": 512 << 20, "dataset": 512 << 20})
                for size, blocks, prefix in files.values():
                    self.assertEqual(blocks, 0)
                    self.assertEqual(prefix, bytes(4096))

    def test_restore_discards_stale_fixture_pages(self):
        calls, files = self.prepare("restore", reused=True)
        self.assertEqual(calls, [])
        for size, blocks, prefix in files.values():
            self.assertEqual(blocks, 0)
            self.assertEqual(prefix, bytes(4096))


class WorkingSetCheckpointTest(unittest.TestCase):
    source_path = Path(__file__).with_name("sandbox-perf-working-set.sh")

    def check_sample(self, group=None, policy="off"):
        source = self.source_path.read_text()
        stage = re.search(r"^stage_checkpoint\(\).*?^}\n", source, re.M | re.S)
        if group:
            start = source.index('        SAMPLE_DIR="$WORK/sample-$group-$iteration"')
            finish = source.index('        SOURCE_PID=', start)
            preparation = source[start:finish]
        else:
            start = source.index('capture_local_crypto_w()')
            finish = source.index('    local source_pid=', start)
            preparation = source[start:finish] + '\n}\ncapture_local_crypto_w "$POLICY" 1\n'
        with tempfile.TemporaryDirectory(prefix="owned-checkpoint-") as tmp:
            root = Path(tmp)
            baseline = root / "immutable base"
            baseline.mkdir()
            names = ["a" * 64 + ".snapshot", "b" * 64 + ".sandbox",
                     "c" * 64 + ".overlay", "d" * 64 + ".overlay"]
            for name in names:
                (baseline / name).write_text(name)
            (baseline / "ws-base.snapshot").symlink_to(names[0])
            calls = root / "calls"
            script = 'set -euo pipefail\n' + (stage[0] if stage else '')
            script += '''write_config() { :; }
reset_sample_storage() { :; }
drop_host_caches() {
    local owned="${w_out:-${W_OUT:-}}"
    [ -n "$owned" ] && [ -f "$owned/$B_BASENAME" ]
}
start_sandbox() {
    printf '%s\\n' "$1" "$4" "$7" "${w_out:-${W_OUT:-}}" > "$CALLS"
}
'''
            script += preparation
            env = dict(os.environ, WORK=str(root), POLICY=policy,
                       group=group or "D", iteration="1", CALLS=str(calls),
                       B=str(baseline / "ws-base.snapshot"), B_OUT=str(baseline),
                       B_DIR=str(baseline.parent), B_ARTIFACT=str(baseline / names[0]),
                       B_BASENAME=names[0], AUTO_B=str(baseline / "ws-base.snapshot"),
                       AUTO_B_OUT=str(baseline), AUTO_B_INFO=str(root / "auto.json"),
                       AUTO_B_ARTIFACT=str(baseline / names[0]), AUTO_B_BASENAME=names[0])
            result = subprocess.run(["bash", "-c", script], env=env,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            sid, base, restored, output = calls.read_text().splitlines()
            checkpoint = Path(base) / sid / "checkpoint"
            self.assertEqual(Path(output), checkpoint)
            self.assertEqual(Path(restored), checkpoint / names[0])
            for name in names:
                self.assertEqual((checkpoint / name).read_text(), name)
                self.assertEqual((baseline / name).read_text(), name)
            self.assertEqual((checkpoint / "ws-base.snapshot").resolve(), checkpoint / names[0])
            # Hiding a sample-owned disk name must not rename the shared B input.
            disk = checkpoint / names[2]
            disk.rename(checkpoint / (names[2] + ".hidden"))
            self.assertTrue((baseline / names[2]).is_file())
            self.assertEqual((baseline / names[2]).read_text(), names[2])

    def test_matrix_sources_and_outputs_use_owned_checkpoint(self):
        for group in "ABCD":
            with self.subTest(group=group):
                self.check_sample(group=group)

    def test_crypto_sources_and_outputs_use_owned_checkpoint(self):
        for policy in ("off", "auto"):
            with self.subTest(policy=policy):
                self.check_sample(policy=policy)

    def test_publication_hides_only_sample_disk_names(self):
        source = self.source_path.read_text()
        start = source.index("        # The W disk tops")
        source = source[start:source.index("        PUBLISH_START=", start)]
        self.assertIn('snapshot_disk_tops "$B_DIR/info.json" "$W_OUT"', source)
        self.assertIn('for path in "${SAMPLE_B_DISK_TOPS[@]}"', source)
        self.assertNotIn('for path in "${B_DISK_TOPS[@]}"', source)
