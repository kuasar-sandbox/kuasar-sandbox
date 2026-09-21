#!/usr/bin/env python3
import pathlib, subprocess, unittest
ROOT=pathlib.Path(__file__).resolve().parents[2]
HELPER=ROOT/"ci/integration/source-owner.sh"
def run(cmd,owner): return subprocess.run(["bash",str(HELPER),cmd,owner],text=True,capture_output=True)
class SourceOwnerClosureTest(unittest.TestCase):
    def words(self,c,o):
        r=run(c,o); self.assertEqual(r.returncode,0,r.stderr); return r.stdout.split()
    def test_bins(self):
        self.assertEqual(self.words("required-bins","connector"),["connector-ctl"])
        self.assertEqual(set(self.words("required-bins","guest-runtime")),{"mkfs.erofs","store-ctl","flatten-ctl"})
        self.assertEqual(set(self.words("required-bins","accelerator")),{"mkfs.erofs","manifest-ctl","store-ctl","cache-ctl","flatten-ctl"})
        self.assertTrue({"vmlinux","sandbox-ctl","cloud-hypervisor","sandbox-runtime.bundle","cache-ctl"} <= set(self.words("required-bins","sandboxer")))
        full=self.words("required-bins","orchestrator"); self.assertEqual(full,self.words("required-bins","kuasar-sandbox")); self.assertIn("connector-ctl",full); self.assertIn("node-ctl",full)
    def test_native(self):
        self.assertEqual(self.words("native-components","connector"),[])
        self.assertEqual(self.words("native-components","guest-runtime"),["erofs"])
        self.assertEqual(set(self.words("native-components","accelerator")),{"erofs","rocksdb"})
        self.assertEqual(set(self.words("native-components","sandboxer")),{"vmlinux","erofs","envd","rocksdb","cloud-hypervisor"})
    def test_images(self):
        self.assertEqual(self.words("images","connector"),[]); self.assertEqual(self.words("images","guest-runtime"),[])
        self.assertEqual(set(self.words("images","accelerator")),{"python:3.12-slim","python:3.12-alpine"})
        self.assertEqual(set(self.words("images","sandboxer")),{"python:3.12-slim","busybox:latest"})
        self.assertEqual(self.words("images","orchestrator"),["python:3.12-slim"])
    def test_heavy_gates(self):
        for o in ("sandboxer","kuasar-sandbox"): self.assertEqual(run("needs-uffd",o).returncode,0); self.assertEqual(run("needs-working-set",o).returncode,0)
        for o in ("accelerator","connector","guest-runtime","orchestrator"): self.assertNotEqual(run("needs-uffd",o).returncode,0); self.assertNotEqual(run("needs-working-set",o).returncode,0)
    def test_unknown(self): self.assertNotEqual(run("required-bins","unknown").returncode,0)
if __name__=="__main__": unittest.main()
