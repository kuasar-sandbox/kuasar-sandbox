"""Kernel release notes use complete history and only the release input paths."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preview_selection_test
import vmlinux_updates as subject


class KernelUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.repo = preview_selection_test.Repository()
        self.addCleanup(self.repo.close)
        self.config = "native-deps/deps/vmlinux/sandbox-common.config"
        self.base = "vmlinux-v1.2.3"
        self.current = "vmlinux-v1.2.4"
        self.base_sha = self.repo.commit_files("initial kernel", {
            self.config: "CONFIG_A=y\n",
            "native-deps/deps/common.sh": "# shared helper\n",
        })
        self.repo.git("tag", self.base)
        self.repo.git("remote", "add", "origin", self.repo.root.as_uri())

    def finish(self):
        self.repo.git("tag", self.current)
        return subject.render_updates(self.repo.root, self.base, self.current)

    def test_script_and_runtime_changes_are_not_kernel_updates(self):
        self.repo.commit_files("runtime and EROFS change", {
            "runtime/main.go": "package runtime\n",
            "native-deps/deps/common.sh": "# new helper comment\n",
            "native-deps/Makefile": "LINUX_VERSION=changed\n",
        })
        result = self.finish()
        self.assertIn("No changes to kernel release inputs", result)
        self.assertNotIn("runtime and EROFS change", result)

    def test_config_comments_and_patches_are_reported_without_unrelated_commits(self):
        self.repo.commit_files("runtime fix", {"runtime/main.go": "package runtime\n"})
        self.repo.commit_files("kernel config comment", {self.config: "# new compiler\nCONFIG_A=y\n"})
        self.repo.commit_files("EROFS update", {"native-deps/deps/erofs-patches/one.patch": "erofs\n"})
        self.repo.commit_files("kernel patch", {"native-deps/deps/linux-patches/one.patch": "kernel\n"})
        result = self.finish()
        self.assertIn("kernel config comment", result)
        self.assertIn("kernel patch", result)
        self.assertNotIn("runtime fix", result)
        self.assertNotIn("EROFS update", result)

    def test_reverted_inputs_do_not_claim_an_update(self):
        self.repo.commit_files("temporary config change", {self.config: "CONFIG_A=n\n"})
        self.repo.commit_files("revert config", {self.config: "CONFIG_A=y\n"})
        result = self.finish()
        self.assertIn("No changes to kernel release inputs", result)
        self.assertNotIn("temporary config change", result)

    def test_rollback_is_explicit(self):
        self.repo.commit_files("kernel change", {self.config: "CONFIG_A=n\n"})
        self.repo.git("tag", self.current)
        result = subject.render_updates(self.repo.root, self.current, self.base)
        self.assertIn("version rollback", result)
        self.assertIn("Kernel release inputs differ", result)

    def test_diverged_history_preserves_the_selected_baseline(self):
        self.repo.commit_files("previous kernel change", {self.config: "CONFIG_A=n\n"})
        previous = self.current
        self.repo.git("tag", previous)
        self.repo.git("checkout", "-qb", "alternative", self.base_sha)
        self.repo.commit_files("selected kernel change", {self.config: "CONFIG_A=m\n"})
        self.current = "vmlinux-v1.2.5"
        self.repo.git("tag", self.current)
        result = subject.render_updates(self.repo.root, previous, self.current)
        self.assertIn("diverged", result)
        self.assertIn("selected kernel change", result)
        self.assertNotIn("previous kernel change", result)

    def test_more_than_compare_api_default_limit_is_complete(self):
        for index in range(255):
            self.repo.commit_files(f"kernel revision {index:03}", {self.config: f"# revision {index}\nCONFIG_A=y\n"})
        result = self.finish()
        self.assertEqual(sum(line.startswith("- ") for line in result.splitlines()), 255)
        self.assertIn("kernel revision 000", result)
        self.assertIn("kernel revision 254", result)

    def test_shallow_history_is_fetched_before_rendering(self):
        self.repo.commit_files("kernel change", {self.config: "# compiler\nCONFIG_A=y\n"})
        self.repo.git("tag", self.current)
        with tempfile.TemporaryDirectory() as directory:
            clone = Path(directory) / "source"
            subprocess.run(["git", "clone", "-q", "--depth=1", "--branch", self.current,
                            self.repo.root.as_uri(), str(clone)], check=True, capture_output=True)
            with self.assertRaisesRegex(ValueError, "history is incomplete"):
                subject.render_updates(clone, self.base, self.current)
            sha = self.repo.git("rev-parse", self.current)
            subject.fetch_history(clone, self.base, self.current, sha)
            self.assertIn("kernel change", subject.render_updates(clone, self.base, self.current))
            self.assertEqual(subject.tag_sources.git(clone, "rev-parse", "HEAD"), sha)

    def test_missing_or_moved_tags_fail_closed(self):
        self.repo.commit_files("kernel change", {self.config: "CONFIG_A=n\n"})
        self.repo.git("tag", self.current)
        refs = ["refs/tags/" + tag for tag in (self.base, self.current)]
        with self.assertRaisesRegex(ValueError, "tag is missing"):
            subject.remote_tags(self.repo.root, refs + ["refs/tags/vmlinux-v9.9.9"])
        expected = {refs[0]: self.base_sha, refs[1]: self.repo.git("rev-parse", self.current)}
        with mock.patch.object(subject, "remote_tags", side_effect=[expected, {**expected, refs[0]: "a" * 40}]):
            with self.assertRaisesRegex(ValueError, "tags moved during fetch"):
                subject.fetch_history(self.repo.root, self.base, self.current, expected[refs[1]])

    def test_runtime_tag_cannot_be_used_as_kernel_baseline(self):
        with self.assertRaisesRegex(ValueError, "unit tag"):
            subject.render_updates(self.repo.root, "runtime-v1.2.3", self.current)


if __name__ == "__main__":
    unittest.main()
