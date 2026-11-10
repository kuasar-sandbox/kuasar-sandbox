// SPDX-License-Identifier: Apache-2.0
package main

import (
	"net"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/kuasar-sandbox/connector/pkg/vswitch"
)

func TestOptionalTransitNativeConfig(t *testing.T) {
	for _, value := range []string{"", "transit_dev: \"\"\n", "transit_dev: null\n", "transit_dev: uplink0\n"} {
		t.Run(value, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "vswitch.yaml")
			if err := os.WriteFile(path, []byte("switch_name: sw0\nswitch_netns: sw0-switch\nnum_ports: 8\nmac_addr: '02:00:00:00:00:01'\nfloating_ip_base: 100.100.96.0\n"+value), 0600); err != nil {
				t.Fatal(err)
			}
			r, err := describe(path)
			if err != nil {
				t.Fatal(err)
			}
			if value == "transit_dev: uplink0\n" {
				if r.Transit.Name != "uplink0" {
					t.Fatal(r.Transit)
				}
			} else {
				if r.Transit != (transit{}) {
					t.Fatalf("fictitious device: %+v", r.Transit)
				}
				for _, restore := range []bool{false, true} {
					if err := verifyTransit(r, restore); err != nil {
						t.Fatal(err)
					}
				}
			}
		})
	}
}

func TestNativeReturnRouteConflictRange(t *testing.T) {
	for _, tt := range []struct {
		base string
		bits uint8
		want []string
	}{
		{"100.100.96.0", 0, []string{"100.100.96.0/20"}},
		{"100.100.96.1", 0, []string{"100.100.96.0/20", "100.100.112.0/20"}},
		{"100.100.96.0", 1, []string{"100.100.96.0/19"}},
		{"0.0.0.0", 20, []string{"0.0.0.0/0"}},
	} {
		var got []string
		for _, subnet := range returnNetworks(&vswitch.Config{FloatingIPBase: net.ParseIP(tt.base), GenerationBits: tt.bits}) {
			got = append(got, subnet.String())
		}
		if !reflect.DeepEqual(got, tt.want) {
			t.Fatalf("%s/%d: %v != %v", tt.base, tt.bits, got, tt.want)
		}
	}
	_, owned, _ := net.ParseCIDR("100.100.96.0/20")
	for cidr, want := range map[string]bool{"100.64.0.0/10": true, "100.100.97.2/32": true, "192.0.2.0/24": false} {
		_, existing, _ := net.ParseCIDR(cidr)
		if overlaps(owned, existing) != want {
			t.Fatal(cidr)
		}
	}
}

func TestListenerConflictPreservesExistingSocket(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	if err := probeListeners([]string{listener.Addr().String()}); err == nil {
		t.Fatal("accepted occupied listener")
	}
	connection, err := net.Dial("tcp", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	connection.Close()
	if err := probeListeners([]string{"", "fd=data", "127.0.0.1:0"}); err != nil {
		t.Fatal(err)
	}
}

func TestWrongHelperNamespaceRejectedBeforePreparation(t *testing.T) {
	if err := run([]string{"prepare", "--primary-netns-inode=1"}); err == nil {
		t.Fatal("accepted a foreign primary namespace")
	}
}
