#!/usr/bin/env python3
"""Cache producer isolation with real private Git trees and no external calls."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("cache_scope", Path(__file__).with_name("cache-scope.py"))
SUBJECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUBJECT)


class CacheScopes(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cache-scope-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.event = self.root / "event.json"
        self.receipt = self.root / "receipt.json"
        self.environment = patch.dict(os.environ, {
            "GITHUB_REPOSITORY": "kuasar-sandbox/kuasar-sandbox", "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_EVENT_PATH": str(self.event),
            "GITHUB_RUN_ID": "100", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GH_TOKEN": "fixture-token-must-not-reach-git", "ACTIONS_RUNTIME_TOKEN": "fixture-runtime-token"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.pr = {"number": 21, "pull_request": {"number": 21,
                   "base": {"ref": "main", "sha": "b" * 40, "repo": {"full_name": "kuasar-sandbox/kuasar-sandbox"}},
                   "head": {"ref": "feature", "sha": "c" * 40, "repo": {"full_name": "contributor/kuasar-sandbox"}}}}
        self.event.write_text(json.dumps(self.pr))
        self.main = patch.object(SUBJECT, "public_main", return_value=True)
        self.admit = self.main.start()
        self.addCleanup(self.main.stop)

    def git(self, directory, *arguments):
        return subprocess.check_output(["git", "-C", str(directory), *arguments], text=True).strip()

    def repository(self, relative="sandboxer"):
        source = self.sources / relative
        source.mkdir(parents=True)
        self.git(source, "init", "-q")
        for key, value in (("user.name", "Chen Xiaohui"), ("user.email", "graych@gmail.com"),
                           ("commit.gpgsign", "false")):
            self.git(source, "config", "--local", key, value)
        self.git(source, "remote", "add", "origin", "https://github.com/" + SUBJECT.OWNERS[source.name] + ".git")
        (source / "source.txt").write_text("exact source\n")
        (source / ".gitignore").write_text("ignored.txt\n")
        self.git(source, "add", "source.txt", ".gitignore")
        self.git(source, "commit", "-qm", "fixture source")
        return source

    def decide(self):
        return SUBJECT.decide(self.sources, self.receipt)

    def again(self):
        self.receipt.unlink(missing_ok=True)
        return self.decide()

    def dispatch(self, inputs=None):
        os.environ["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        self.event.write_text(json.dumps({"inputs": inputs or {}}))

    def plan(self, source):
        directory = self.sources / ".ci"
        directory.mkdir(exist_ok=True)
        selected = {source.name: {"repository": SUBJECT.OWNERS[source.name], "sha": self.git(source, "rev-parse", "HEAD")}}
        value = {"sources": selected, "test_revisions": selected, "framework_sha": "a" * 40,
                 "kernel_sha": "e" * 40, "candidate_records": []}
        (directory / "plan.json").write_text(json.dumps(value))
        return value

    def test_different_prs_on_the_same_default_ref_cannot_share_candidate_prefixes(self):
        self.repository()
        first = self.decide()
        self.pr["number"] = self.pr["pull_request"]["number"] = 22
        self.event.write_text(json.dumps(self.pr))
        second = self.again()
        self.assertEqual(first["scope"], "candidate")
        self.assertEqual(first["ref"], second["ref"])
        self.assertNotEqual(first["namespace"], second["namespace"])
        self.assertFalse((second["namespace"] + "-recipe").startswith(first["namespace"] + "-"))
        self.admit.assert_not_called()

    def test_changed_actual_pr_head_or_base_changes_namespace(self):
        self.repository()
        first = self.decide()["namespace"]
        for field in ("head", "base"):
            event = copy.deepcopy(self.pr)
            event["pull_request"][field]["sha"] = "f" * 40
            self.event.write_text(json.dumps(event))
            self.assertNotEqual(self.again()["namespace"], first)

    def test_same_input_retries_ignore_run_directory_plan_id_and_image_labels(self):
        source = self.repository()
        plan = self.plan(source)
        (self.sources / ".ci/workbench.json").write_text(json.dumps({"aggregate_version": "day-one"}))
        before = self.decide()["namespace"]
        newer = self.root / "another-run-sources"
        shutil.copytree(self.sources, newer)
        self.sources = newer
        os.environ["GITHUB_RUN_ID"] = "200"
        os.environ["GITHUB_RUN_ATTEMPT"] = "3"
        plan.update(plan_id="other-plan", run_id="200", daily_version="day-two")
        (newer / ".ci/plan.json").write_text(json.dumps(plan))
        (newer / ".ci/workbench.json").write_text(json.dumps({"aggregate_version": "day-two"}))
        self.assertEqual(self.again()["namespace"], before)

    def test_companion_set_and_exact_test_or_kernel_pins_change_namespace(self):
        source = self.repository()
        plan = self.plan(source)
        before = self.decide()["namespace"]
        variants = []
        companion = copy.deepcopy(plan)
        companion["candidate_records"] = [{"repository": "kuasar-sandbox/connector", "pull_request_number": 10,
             "candidate_sha": "d" * 40, "base_sha": "a" * 40, "head_sha": "e" * 40, "base_ref": "main"}]
        variants.append(companion)
        test = copy.deepcopy(plan)
        test["test_revisions"]["sandboxer"]["sha"] = "f" * 40
        variants.append(test)
        kernel = copy.deepcopy(plan)
        kernel["kernel_sha"] = "f" * 40
        variants.append(kernel)
        for value in variants:
            (self.sources / ".ci/plan.json").write_text(json.dumps(value))
            self.assertNotEqual(self.again()["namespace"], before)

    def test_all_concrete_nested_source_groups_are_bound(self):
        self.repository()
        nested = [self.repository("test-helpers/orchestrator"), self.repository("test-overlays/sandboxer"),
                  self.repository("kernel-unit/guest-runtime")]
        baseline = self.decide()
        self.assertEqual(len(baseline["sources"]), 4)
        for source in nested:
            (source / "source.txt").write_text("different exact input\n")
            self.assertNotEqual(self.again()["namespace"], baseline["namespace"])
            (source / "source.txt").write_text("exact source\n")

    def test_default_main_dispatch_candidate_does_not_gain_trust(self):
        self.repository()
        self.dispatch({"candidate_repository": "kuasar-sandbox/sandboxer", "candidate_sha": "f" * 40})
        self.assertEqual(self.decide()["scope"], "candidate")
        self.admit.assert_not_called()

    def test_full_clean_main_sources_with_known_transport_can_save_trusted_cache(self):
        source = self.repository()
        self.repository("test-helpers/orchestrator")
        self.repository("test-overlays/sandboxer")
        self.repository("kernel-unit/guest-runtime")
        self.plan(source)
        self.dispatch()
        result = self.decide()
        self.assertEqual((result["scope"], result["namespace"]), ("trusted", "trusted"))
        self.assertTrue(all(row["clean"] and row["on_main"] for row in result["sources"]))
        self.assertEqual(self.admit.call_count, 4)

    def test_dirty_untracked_and_gitignored_files_cannot_be_trusted(self):
        source = self.repository()
        self.dispatch()
        for relative in ("source.txt", "new.go", "ignored.txt"):
            with self.subTest(relative=relative):
                (source / relative).write_text("unadmitted bytes\n")
                value = self.again()
                self.assertEqual(value["scope"], "candidate")
                self.assertFalse(value["sources"][0]["clean"])
                self.admit.assert_not_called()
                if relative == "source.txt":
                    (source / relative).write_text("exact source\n")
                else:
                    (source / relative).unlink()

    def test_empty_directories_are_inputs_and_cannot_make_a_dirty_tree_trusted(self):
        source = self.repository()
        self.dispatch()
        trusted = self.decide()
        self.assertEqual(trusted["scope"], "trusted")
        (source / "new-empty").mkdir()
        changed = self.again()
        self.assertEqual(changed["scope"], "candidate")
        self.assertNotEqual(changed["sources"][0]["content_sha256"], trusted["sources"][0]["content_sha256"])
        (source / "new-empty").rmdir()
        (self.sources / "test-helpers").mkdir()
        self.assertEqual(self.again()["scope"], "candidate")

    def test_declared_archives_and_named_readers_allow_trusted_save(self):
        source = self.repository()
        plan = self.plan(source)
        assets = self.sources / ".ci/assets"
        assets.mkdir()
        (assets / "sandboxer-day-one.tar.gz").write_bytes(b"exact frozen package")
        (assets / "SHA256SUMS").write_text("frozen checksum manifest\n")
        plan["baseline"] = {"assets": [{"name": file.name,
            "digest": "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest()} for file in sorted(assets.iterdir())]}
        (self.sources / ".ci/plan.json").write_text(json.dumps(plan))
        readers = self.sources / ".ci/readers"
        readers.mkdir()
        for name in ("mkfs.erofs", "fsck.erofs", "dump.erofs", "runtime-payloads.py", "erofs-readers.COPYING", "SHA256SUMS"):
            (readers / name).write_text("reader bytes for " + name)
        self.dispatch()
        self.assertEqual(self.decide()["namespace"], "trusted")
        # Different labels for verified identical archive bytes are locators.
        self.event.write_text(json.dumps({"inputs": {"candidate_sha": "c" * 40}}))
        first = self.again()["namespace"]
        (assets / "sandboxer-day-one.tar.gz").rename(assets / "sandboxer-day-two.tar.gz")
        for record in plan["baseline"]["assets"]:
            record["name"] = record["name"].replace("day-one", "day-two")
        (self.sources / ".ci/plan.json").write_text(json.dumps(plan))
        self.assertEqual(self.again()["namespace"], first)
        (readers / "fsck.erofs").write_text("reader bytes for dump.erofs")
        (readers / "dump.erofs").write_text("reader bytes for fsck.erofs")
        self.assertNotEqual(self.again()["namespace"], first)
        self.dispatch()
        (assets / "extra-input").write_text("not selected by plan")
        self.assertEqual(self.again()["scope"], "candidate")

    def test_unknown_root_and_transport_inputs_remain_candidate_and_are_hashed(self):
        source = self.repository()
        self.plan(source)
        self.dispatch()
        for relative in ("unknown-input", ".ci/unselected-source.py"):
            with self.subTest(relative=relative):
                path = self.sources / relative
                path.write_text("first\n")
                first = self.again()
                self.assertEqual(first["scope"], "candidate")
                path.write_text("second\n")
                self.assertNotEqual(self.again()["namespace"], first["namespace"])
                path.unlink()

    def test_snapshot_changes_bind_without_following_external_symlinks(self):
        snapshot = self.sources / "sandboxer"
        snapshot.mkdir()
        (snapshot / "source.txt").write_text("snapshot one\n")
        outside = self.root / "outside"
        outside.write_text("must not be read\n")
        (snapshot / "link").symlink_to(outside)
        first = self.decide()
        self.assertEqual(first["sources"][0]["kind"], "snapshot")
        outside.write_text("a destination change is not a followed input\n")
        self.assertEqual(self.again()["namespace"], first["namespace"])
        (snapshot / "source.txt").write_text("snapshot two\n")
        self.assertNotEqual(self.again()["namespace"], first["namespace"])

    def test_receipt_first_never_reads_candidate_modified_git_or_event(self):
        source = self.repository()
        first = self.decide()
        self.git(source, "config", "--local", "core.fsmonitor", "candidate-hook")
        self.event.unlink()
        with patch.object(SUBJECT, "git_read", side_effect=AssertionError("Git after candidate execution")), \
             patch.object(SUBJECT, "snapshot", side_effect=AssertionError("source after candidate execution")):
            self.assertEqual(self.decide(), first)

    def test_foreign_receipt_fails_before_git(self):
        self.repository()
        self.decide()
        os.environ["GITHUB_RUN_ID"] = "another-run"
        with patch.object(SUBJECT, "git_read", side_effect=AssertionError("Git before rejection")):
            with self.assertRaisesRegex(ValueError, "foreign cache scope"):
                self.decide()

    def test_real_fsmonitor_and_clean_filters_do_not_execute_or_receive_credentials(self):
        source = self.repository()
        (source / ".gitattributes").write_text("source.txt filter=fixture\n")
        self.git(source, "add", ".gitattributes")
        self.git(source, "commit", "-qm", "tracked filter declaration")
        marker = self.root / "hook-ran"
        hook = self.root / "fixture-hook"
        hook.write_text("#!/bin/sh\nprintf executed > '" + str(marker) + "'\ncat\n")
        hook.chmod(0o755)
        self.git(source, "config", "--local", "core.fsmonitor", str(hook))
        self.git(source, "config", "--local", "filter.fixture.clean", str(hook))
        subprocess.run(["git", "-C", source, "status", "--porcelain"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        self.assertTrue(marker.exists(), "fixture must demonstrate the host Git hook hazard")
        marker.unlink()
        real_output = subprocess.check_output
        def output(command, **kwargs):
            if command[0] == "git":
                self.assertNotIn("GH_TOKEN", kwargs["env"])
                self.assertNotIn("ACTIONS_RUNTIME_TOKEN", kwargs["env"])
                self.assertEqual(kwargs["env"]["GIT_NO_REPLACE_OBJECTS"], "1")
            return real_output(command, **kwargs)
        with patch.object(SUBJECT.subprocess, "check_output", side_effect=output):
            self.decide()
        self.assertFalse(marker.exists())


class PublicMain(unittest.TestCase):
    def test_public_repository_and_ancestor_are_both_required(self):
        repository, sha = "kuasar-sandbox/sandboxer", "a" * 40
        for private, ancestor, allowed in ((False, sha, True), (False, "b" * 40, False), (True, sha, None)):
            with self.subTest(private=private, ancestor=ancestor), patch.object(
                    SUBJECT.subprocess, "check_output", side_effect=[
                        json.dumps({"full_name": repository, "private": private, "visibility": "private" if private else "public"}),
                        json.dumps({"merge_base_commit": {"sha": ancestor}})]):
                if allowed is None:
                    with self.assertRaisesRegex(ValueError, "not public"):
                        SUBJECT.public_main(repository, sha)
                else:
                    self.assertEqual(SUBJECT.public_main(repository, sha), allowed)


if __name__ == "__main__":
    unittest.main()
