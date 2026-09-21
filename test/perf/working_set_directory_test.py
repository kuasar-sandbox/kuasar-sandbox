"""Exercise the benchmark's actual workspace assignment without KVM setup."""
import os
import re
import tempfile
from pathlib import Path
import subprocess
import unittest


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
