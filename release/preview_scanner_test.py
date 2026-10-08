#!/usr/bin/env python3
"""Exercise the actual scanner shell with a deterministic GitHub CLI."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parent.parent
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ["FAKE_GH_ROOT"])
state_file = root / "state.json"
s = json.loads(state_file.read_text())
args = sys.argv[1:]
def save(): state_file.write_text(json.dumps(s))
if args[0] == "api":
    endpoint = next(a for a in args if a.startswith("repos/"))
    if "/branches?" in endpoint:
        print("\n".join(s["refs"][1:]))
    elif "/git/ref/heads/" in endpoint:
        print(("a" if not s["dispatches"] else "b") * 40)
    elif "preview-gc.yml" in endpoint:
        runs = [dict(id=999, status="in_progress")] if s.get("gc_once") else []
        s["gc_once"] = False
        save()
        print(json.dumps([{"workflow_runs": runs}]))
    elif "daily-preview-branch.yml" in endpoint:
        runs = [] if not s["dispatches"] else [dict(id=len(s["dispatches"]), display_title=s["title"], status="completed")]
        print(json.dumps([{"workflow_runs": runs}]))
    elif "/actions/runs/" in endpoint:
        print(json.dumps(dict(status="in_progress" if endpoint.endswith("/999") else "completed", conclusion="success")))
    else: raise SystemExit("unexpected API " + endpoint)
elif args[:2] == ["workflow", "run"]:
    fields = dict(a.split("=", 1) for a in args if "=" in a)
    s["dispatches"].append(fields)
    s["title"] = f'Daily preview {fields["platform_ref"]}@{fields["platform_sha"]} for {fields["date"]}'
    save()
elif args[:2] == ["run", "download"]:
    directory = Path(args[args.index("--dir")+1])
    f = s["dispatches"][-1]
    outcomes = s["outcomes"]
    outcome = outcomes[min(len(s["dispatches"])-1, len(outcomes)-1)]
    data = dict(status=outcome, platform_ref=f["platform_ref"], selected_sha=f["platform_sha"], final_sha="b"*40, date=f["date"])
    if outcome == "moved-head": data.update(status="published", final_sha="a"*40)
    if outcome == "missing-final": data.pop("final_sha")
    if outcome == "bad-final": data["final_sha"] = "main"
    if outcome == "wrong-head": data.update(status="published", selected_sha="c"*40)
    (directory/"preview-result.json").write_text(json.dumps(data))
else: raise SystemExit("unexpected gh " + repr(args))
'''

class PreviewScannerTest(unittest.TestCase):
    def scan(self, outcomes, refs=("main",), gc_seconds=0):
        workflow = (ROOT/".github/workflows/daily-preview.yml").read_text()
        script = textwrap.dedent("          set -euo pipefail\n" + workflow.split("          set -euo pipefail\n", 1)[1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"gh").write_text(FAKE_GH)
            (root/"gh").chmod(0o755)
            state = root/"state.json"
            state.write_text(json.dumps(dict(outcomes=outcomes, dispatches=[], refs=refs, gc_once=bool(gc_seconds))))
            env = dict(os.environ, PATH=str(root)+os.pathsep+os.environ["PATH"],
                       FAKE_GH_ROOT=str(root), GITHUB_REPOSITORY="kuasar-sandbox/kuasar-sandbox",
                       REQUESTED_DATE="20261008", REQUESTED_REF="main" if len(refs)==1 else "")
            script = f"sleep() {{ SECONDS=$((SECONDS + {gc_seconds})); }};\n" + script
            result = subprocess.run(["bash", "-c", script], env=env, text=True,
                                    capture_output=True, timeout=15)
            return result, json.loads(state.read_text())["dispatches"]

    def test_pending_success_run_resumes_latest_head(self):
        result, dispatches = self.scan(["pending", "published"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([d["platform_sha"] for d in dispatches], ["a"*40, "b"*40])

    def test_pending_exhaustion_does_not_report_success(self):
        result, dispatches = self.scan(["pending"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(dispatches), 3)
        self.assertIn("not converged", result.stderr)

    def test_pending_branches_share_the_scanner_wait_budget(self):
        refs = ("main", "release/v0.1.x", "release/v0.2.x")
        result, dispatches = self.scan(["pending"], refs)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([d["platform_ref"] for d in dispatches],
                         [ref for ref in refs for _ in range(3)])
        self.assertEqual(len(dispatches), 9)
        self.assertLessEqual(sum(int(d["wait_seconds"]) for d in dispatches), 300*60)
        self.assertTrue(all(0 < int(d["wait_seconds"]) <= 3000 for d in dispatches))

    def test_gc_time_reduces_every_branches_remaining_wait(self):
        refs = ("main", "release/v0.1.x", "release/v0.2.x")
        result, dispatches = self.scan(["pending"], refs, gc_seconds=75*60)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(dispatches), 9)
        self.assertLessEqual(sum(int(d["wait_seconds"]) for d in dispatches), 225*60)

    def test_gc_budget_exhaustion_does_not_dispatch(self):
        result, dispatches = self.scan(["pending"], gc_seconds=300*60)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(dispatches, [])

    def test_terminal_receipt_requires_the_current_final_head(self):
        result, dispatches = self.scan(["moved-head", "published"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(dispatches), 2)

    def test_final_head_is_required_and_exact(self):
        for outcome in ("missing-final", "bad-final"):
            with self.subTest(outcome=outcome):
                result, dispatches = self.scan([outcome])
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(dispatches), 1)

    def test_wrong_head_receipt_is_rejected(self):
        result, dispatches = self.scan(["wrong-head"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(dispatches), 1)

    def test_rule_based_noops_remain_terminal(self):
        for outcome in ("unchanged", "closed"):
            with self.subTest(outcome=outcome):
                result, dispatches = self.scan([outcome])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(dispatches), 1)

if __name__ == "__main__":
    unittest.main()
