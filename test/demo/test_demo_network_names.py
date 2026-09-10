"""Check actual Demo name selection before it can create any host object."""
import os
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent
NAMES = re.search(r"(?ms)^RUN_KEY=.*?(?=^FIP_CIDR=)", (ROOT / "demo_e2b.sh").read_text()).group()


class NetworkNames(unittest.TestCase):
    def select(self, run_id=None, switch=None):
        environment = {"PATH": os.environ["PATH"]}
        if run_id is not None:
            environment["DEMO_RUN_ID"] = run_id
        if switch is not None:
            environment["SWITCH"] = switch
        command = 'set -euo pipefail\ndemo_die() { echo "$*" >&2; exit 1; }\n'
        command += NAMES + '\nprintf "%s\\n" "$RUN_KEY" "$SWITCH" "$SW_MGMT"\n'
        return subprocess.run(["bash", "-c", command], env=environment,
                              text=True, capture_output=True, timeout=5)

    def test_default_and_supported_run_ids_leave_room_for_connector_suffixes(self):
        for run_id in (None, "abc123", "abc1234567", "abc123456789"):
            with self.subTest(run_id=run_id):
                result = self.select(run_id)
                self.assertEqual(result.returncode, 0, result.stderr)
                selected_id, switch, management = result.stdout.splitlines()
                self.assertEqual(switch, "k" + selected_id[:8])
                for suffix in ("-dummy", "-t4096", "-m0"):
                    self.assertLessEqual(len(switch + suffix), 15)
                self.assertLessEqual(len(management), 15)

    def test_custom_switch_limit_is_checked_before_host_setup(self):
        self.assertEqual(self.select("abc1234567", "abcdefghi").returncode, 0)
        result = self.select("abc1234567", "abcdefghij")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("1-9", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
