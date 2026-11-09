// SPDX-License-Identifier: Apache-2.0
package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDescribeUsesSelectedNativeOverlay(t *testing.T) {
	dir := t.TempDir()
	base := filepath.Join(dir, "base.yaml")
	overlay := filepath.Join(dir, "instance.yaml")
	if err := os.WriteFile(base, []byte(`switch_name: old
switch_netns: old-switch
num_ports: 16
mac_addr: "02:00:00:00:00:01"
floating_ip_base: 100.100.96.0
transit_dev: net1
mgmt_extracts: [":old_m0:169.254.169.254/32"]
mgmt_services: ["169.254.169.254:80:127.0.0.1:19254"]
`), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(overlay, []byte(`switch_name: custom
switch_netns: custom-switch
port_netns: custom-ports
transit_dev: tenant-uplink
mgmt_extracts: ["custom-mgmt:metadata:169.254.169.254/32"]
`), 0600); err != nil {
		t.Fatal(err)
	}
	r, err := describe(base + ":" + overlay)
	if err != nil {
		t.Fatal(err)
	}
	if r.SwitchName != "custom" || r.SwitchNetNS != "custom-switch" || r.Transit.Name != "tenant-uplink" {
		t.Fatalf("not the effective native configuration: %+v", r)
	}
	if len(r.Namespaces) != 3 || len(r.Management) != 1 || !r.Management[0].Loopback || r.Management[0].Device != "metadata" {
		t.Fatalf("lost native namespace/management input: %+v", r)
	}
	if len(r.ConfigSHA256) != 64 || r.State != "described" || r.MainNetNSInode != 0 {
		t.Fatalf("describe unexpectedly needs running instance state: %+v", r)
	}
	if len(r.Management[0].Addresses) != 1 || r.Management[0].Addresses[0] != "169.254.169.254/32" {
		t.Fatalf("missing local management VIP: %+v", r.Management)
	}
}

func TestCleanupNamesCannotEscapeOwnedDirectory(t *testing.T) {
	for _, name := range []string{"", ".", "..", "/sys/fs/bpf", "a/../other", "with\nnewline", "two names"} {
		if safeName(name) == nil {
			t.Errorf("unsafe cleanup resource name accepted: %q", name)
		}
	}
	for _, name := range []string{"sw0-switch", "custom_switch.2", "tenant-net1"} {
		if err := safeName(name); err != nil {
			t.Errorf("valid resource name rejected: %v", err)
		}
	}
}
