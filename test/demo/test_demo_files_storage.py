"""Exercise actual Demo COPY configuration without contacting object storage."""
import os
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent
CONFIG = re.search(r'(?ms)^FILES_STORAGE_CFG=.*?(?=^cat > "\$WORK/conductor.yaml")',
                   (ROOT / "demo_e2b.sh").read_text()).group()


class FilesStorage(unittest.TestCase):
    def configure(self, endpoint):
        command = 'set -euo pipefail\ndie() { echo "$*" >&2; exit 1; }\n'
        command += 'MGMT_SERVICE_ARGS=(fixture-registry)\n' + CONFIG
        command += '\nprintf "%s\\n" "$FILES_STORAGE_CFG" "${MGMT_SERVICE_ARGS[*]}"\n'
        return subprocess.run(["bash", "-c", command], text=True, capture_output=True, timeout=5,
                              env={"PATH": os.environ["PATH"], "VGW_ENDPOINT": endpoint,
                                   "MGMT_VIP": "169.254.169.254", "VGW_BUCKET": "fixture-bucket",
                                   "VGW_ACCESS_KEY_VALUE": "fixture-access",
                                   "VGW_SECRET_KEY_VALUE": "fixture-secret"})

    def test_copy_endpoint_stays_reachable_by_host_sdk_conductor_and_builder(self):
        for endpoint in ("http://127.0.0.1:5050", "http://localhost:5050",
                         "https://storage.example.invalid"):
            with self.subTest(endpoint=endpoint):
                result = self.configure(endpoint)
                self.assertEqual(result.returncode, 0, result.stderr)
                config, services = result.stdout.splitlines()
                self.assertIn("endpoint: '" + endpoint + "'", config)
                self.assertIn("force_path_style: true", config)
                self.assertEqual(services, "fixture-registry")

    def test_absent_storage_keeps_quickstart_copy_optional(self):
        result = self.configure("")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "\nfixture-registry\n")


if __name__ == "__main__":
    unittest.main()
