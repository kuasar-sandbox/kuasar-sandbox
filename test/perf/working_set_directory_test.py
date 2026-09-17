"""Exercise the benchmark's actual workspace assignment without KVM setup."""
import os
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
