// SPDX-License-Identifier: Apache-2.0
package main

import "testing"

func TestEgressDeploymentMetadataUsesYAMLAndJSON(t *testing.T) {
	for _, input := range []string{
		"# gateway file\nname: local\nruntime_dir: /run/connector-egress/local\nareas: [{vni: 7}]\n",
		`{"name":"local","runtime_dir":"/run/connector-egress/local","areas":[{"vni":7}]}`,
		"Name: local\nRuntime_dir: /run/connector-egress/local\n",
	} {
		metadata, err := readEgressMetadata([]byte(input))
		if err != nil || metadata.Name != "local" || metadata.RuntimeDir != "/run/connector-egress/local" {
			t.Fatalf("metadata=%+v error=%v", metadata, err)
		}
	}
	metadata, err := readEgressMetadata([]byte("name: local\n"))
	if err != nil || metadata.Name != "local" || metadata.RuntimeDir != "" {
		t.Fatal(metadata, err)
	}
}
