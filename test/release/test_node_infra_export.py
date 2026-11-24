"""Exercise Quick Start's shipped export assertions without Docker or KVM."""
import base64
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

LIB = Path(__file__).resolve().parents[1] / "e2e/platform/lib"
spec = importlib.util.spec_from_file_location("quickstart_export", LIB / "node_infra_quickstart.py")
subject = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(LIB))
try:
    spec.loader.exec_module(subject)
finally:
    sys.path.pop(0)


def located(key):
    return f"file://{key}.bundle@manifest:{key}@location:quickstart"


class ExportAssertions(unittest.TestCase):
    def report(self, *, memory=True, annotation=""):
        disk, snapshot = located("a" * 64), located("b" * 64) + annotation
        root, kind = (snapshot, "snp") if memory else (disk, "sbx")
        return {"sandboxRef": disk, "snapshotRef": snapshot if memory else "",
                "result": f"bare-{kind}-" + base64.urlsafe_b64encode(root.encode()).decode().rstrip("=")}

    def export(self, report, *, memory=True):
        node = subject.QuickStartNode.__new__(subject.QuickStartNode)
        node.api = Mock(side_effect=[None, report])
        result = node.export("sandbox", memory)
        self.assertEqual(node.api.call_args_list[0].args,
                         ("POST", "/sandboxes/sandbox/pause", {"memory": memory}, 204))
        return result

    def test_old_and_declared_memory_exports_keep_the_full_template(self):
        for suffix in ("", "@runtime:sandbox-runtime.bundle:digest:" + "c" * 64):
            with self.subTest(suffix=suffix):
                report = self.report(annotation=suffix)
                self.assertIs(self.export(report), report)
                payload = report["result"].split("-", 2)[2]
                self.assertEqual(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode(),
                                 report["snapshotRef"])

    def test_metadata_cannot_hide_the_same_memory_and_disk_carrier(self):
        report = self.report(annotation="@runtime:sandbox-runtime.bundle:digest:" + "c" * 64)
        report["snapshotRef"] = report["snapshotRef"].replace("b" * 64, "a" * 64)
        with self.assertRaisesRegex(RuntimeError, "same Manifest selector"):
            self.export(report)

    def test_changed_template_or_missing_carrier_is_rejected(self):
        for value in ("manifest://" + "b" * 64,
                      "file://" + "b" * 64 + ".bundle@manifest:" + "b" * 64):
            report = self.report()
            report["snapshotRef"] = value
            with self.assertRaisesRegex(RuntimeError, "located Bundle selector"):
                self.export(report)
        report = self.report(annotation="@runtime:sandbox-runtime.bundle:digest:" + "c" * 64)
        report["result"] = self.report()["result"]
        with self.assertRaisesRegex(RuntimeError, "template ID differs"):
            self.export(report)

    def test_disk_only_contract_is_unchanged(self):
        report = self.report(memory=False)
        self.assertIs(self.export(report, memory=False), report)
        report["snapshotRef"] = located("b" * 64)
        with self.assertRaisesRegex(RuntimeError, "disk-only"):
            self.export(report, memory=False)


if __name__ == "__main__":
    unittest.main()
