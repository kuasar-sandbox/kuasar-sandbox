import hashlib
import subprocess
import tempfile
import unittest
from io import StringIO
import urllib.error
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stderr
import kuasar_deploy as deploy


def release_entries(include_aarch64=False):
    digest = hashlib.sha256(b"release artifact").hexdigest()
    names = [
        "platform-release-v0.1.5.tar.gz",
        "accelerator-v0.1.5-linux-x86_64.tar.gz",
        "connector-v0.1.4-linux-x86_64.tar.gz",
        "orchestrator-v0.1.5-linux-x86_64.tar.gz",
        "sandboxer-v0.1.5-linux-x86_64.tar.gz",
        "sandbox-runtime-x86_64-v0.1.5.tar.gz",
        "vmlinux-x86_64-v0.1.3.tar.gz",
    ]
    if include_aarch64:
        names += [
            "accelerator-v0.1.5-linux-aarch64.tar.gz",
            "connector-v0.1.4-linux-aarch64.tar.gz",
            "orchestrator-v0.1.5-linux-aarch64.tar.gz",
            "sandboxer-v0.1.5-linux-aarch64.tar.gz",
            "sandbox-runtime-aarch64-v0.1.5.tar.gz",
            "vmlinux-aarch64-v0.1.3.tar.gz",
        ]
    return [(digest, name) for name in names]


def manifest(entries):
    return "".join(f"{digest}  {name}\n" for digest, name in entries)

class DownloadTests(unittest.TestCase):
    def test_manifest_rejects_unsafe_names_duplicates_and_empty(self):
        digest = '0' * 64
        for value in ('', digest + '  ../evil.tar.gz', (digest + '  a.tar.gz\n') * 2):
            with self.subTest(value=value), self.assertRaises(deploy.DeployError):
                deploy.checksum_entries(value)

    def test_uninstalled_package_record_is_missing(self):
        result = subprocess.CompletedProcess([], 0, stdout="unknown ok not-installed")
        with patch.object(deploy, "APT_PACKAGES", ("python3-venv",)), patch.object(deploy.shutil, "which", return_value="/usr/bin/dpkg-query"), patch.object(deploy.subprocess, "run", return_value=result):
            self.assertEqual(deploy.missing_apt_packages(), ["python3-venv"])

    def test_python_environment_uses_release_requirements(self):
        with tempfile.TemporaryDirectory() as root:
            install = Path(root)
            requirements = install / "test" / "demo" / "requirements.txt"
            requirements.parent.mkdir(parents=True)
            requirements.write_text("e2b==2.25.1\n")
            with patch.object(deploy, "run") as run:
                deploy.prepare_python_environment(install)
            self.assertEqual(
                run.call_args_list[-1].args[0],
                [str(install / ".venv" / "bin" / "python"), "-m", "pip", "install", "-r", str(requirements)],
            )

    def test_default_release_is_current_stable_version(self):
        args = deploy.build_parser().parse_args(["prepare"])
        self.assertEqual(args.version, "release-v0.1.5")

    def test_validate_release_extracts_only_x86_64_assets(self):
        entries = release_entries(include_aarch64=True)
        with tempfile.TemporaryDirectory() as root:
            download = Path(root) / "download"
            install = Path(root) / "install"
            download.mkdir(); install.mkdir()
            (download / "SHA256SUMS").write_text(manifest(entries))
            for _, name in entries:
                if "aarch64" not in name:
                    (download / name).write_bytes(b"release artifact")
            for name in ("bin", "deploy", "docs", "test"):
                (install / name).mkdir()
            for name in ("cache-ctl", "cloud-hypervisor"):
                (install / "bin" / name).touch()
            result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            with patch.object(deploy, "run", return_value=result) as run:
                deploy.validate_release(download, install, "release-v0.1.5")
            commands = [call.args[0] for call in run.call_args_list]
            extracted = [command[2] for command in commands if command[:2] == ["tar", "-xzf"]]
            self.assertEqual(len(extracted), 7)
            self.assertTrue(all("aarch64" not in archive for archive in extracted))

    def test_version_cannot_escape_root(self):
        with self.assertRaises(deploy.DeployError):
            deploy.release_download_base('owner/repo', '../../bad')

    def test_download_verify_and_cache_without_auth(self):
        data = b"release artifact"
        calls = []
        def fetch(url, target):
            calls.append(url)
            target.write_bytes(manifest(release_entries(True)).encode() if target.name == "SHA256SUMS" else data)
        with tempfile.TemporaryDirectory() as root, patch.object(deploy, "download_file", side_effect=fetch), patch.object(deploy, "run", side_effect=AssertionError("no CLI permitted")):
            deploy.download_release("owner/repo", "release-v0.1.5", Path(root))
            deploy.download_release("owner/repo", "release-v0.1.5", Path(root))
        archives = [url for url in calls if url.endswith(".tar.gz")]
        self.assertEqual(len(archives), 7)
        self.assertTrue(all("aarch64" not in url for url in archives))

    def test_checksum_mismatch_fails(self):
        def fetch(url, target):
            target.write_bytes(manifest(release_entries()).encode() if target.name == "SHA256SUMS" else b"corrupt")
        with tempfile.TemporaryDirectory() as root, patch.object(deploy, "download_file", side_effect=fetch), self.assertRaisesRegex(deploy.DeployError, "SHA256 mismatch"):
            deploy.download_release("owner/repo", "release-v0.1.5", Path(root))

    def test_x86_64_assets_fail_when_missing_or_ambiguous(self):
        entries = release_entries()
        with self.assertRaisesRegex(deploy.DeployError, "runtime archive; found 0"):
            deploy.x86_64_release_entries([(digest, name) for digest, name in entries if "sandbox-runtime" not in name])
        with self.assertRaisesRegex(deploy.DeployError, "connector archive; found 2"):
            deploy.x86_64_release_entries(entries + [entries[2]])
        with self.assertRaisesRegex(deploy.DeployError, "platform archive for release-v0.1.6"):
            deploy.x86_64_release_entries(entries, "release-v0.1.6")

    def test_unit_isolation_is_explicit_for_each_release_style(self):
        with tempfile.TemporaryDirectory() as root:
            demo = Path(root) / "test" / "demo" / "demo_e2b.sh"
            demo.parent.mkdir(parents=True)
            demo.write_text('RUN_KEY="${DEMO_RUN_ID:-x}"\nRUNNER_TEMPLATE="unit"\n')
            self.assertEqual(deploy.unit_isolation_mode(Path(root), "kuasar-demo"), "per-run")
            with self.assertRaisesRegex(deploy.DeployError, "does not support --unit-prefix"):
                deploy.unit_isolation_mode(Path(root), "other")
            demo.write_text('UNIT_PREFIX="${DEMO_UNIT_PREFIX:-sandbox}"\n')
            self.assertEqual(deploy.unit_isolation_mode(Path(root), "other"), "configurable")
            demo.write_text('# no isolation support\n')
            with self.assertRaisesRegex(deploy.DeployError, "release-v0.1.5"):
                deploy.unit_isolation_mode(Path(root), "kuasar-demo")

    def test_failure_summary_includes_check_reason(self):
        args = deploy.argparse.Namespace(command="check")
        with patch.object(deploy, "build_parser") as parser, \
             patch.object(deploy, "check_environment", side_effect=deploy.DeployError("host details")), \
             patch.object(deploy.os, "geteuid", return_value=1000), \
             redirect_stderr(StringIO()) as stderr:
            parser.return_value.parse_args.return_value = args
            self.assertEqual(deploy.main(), 1)
            self.assertIn("Reason: host details", stderr.getvalue())

    def test_tls_failure_is_reported_without_disabling_verification(self):
        with tempfile.TemporaryDirectory() as root, patch.object(deploy.urllib.request, 'urlopen', side_effect=urllib.error.URLError('certificate verify failed')) as opened:
            target = Path(root) / 'asset'
            with self.assertRaisesRegex(deploy.DeployError, 'CA trust'):
                deploy.download_file('https://github.com/asset', target)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_name('asset.part').exists())
            self.assertNotIn('context', opened.call_args.kwargs)

if __name__ == '__main__':
    unittest.main()
