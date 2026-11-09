// SPDX-License-Identifier: Apache-2.0
package main

import (
	"encoding/json"
	"os"
	"os/exec"

	"gopkg.in/yaml.v3"
)

// Normalize with the native CLI first. This adapter only converts its complete
// diagnostic output to JSON for the stdlib host tool; it never merges configs
// or validates a competing schema, and emits no credentials.
func budget() error {
	output, err := exec.Command("/usr/libexec/node-infra/service", "config", "conductor").Output()
	if err != nil {
		return err
	}
	var normalized struct {
		ResourceListen struct {
			Enabled bool `yaml:"enabled" json:"enabled"`
			Pools   []struct {
				Name        string `yaml:"name" json:"name"`
				Allocatable struct {
					CPU    *float64 `yaml:"cpu" json:"cpu"`
					Memory *string  `yaml:"memory" json:"memory"`
				} `yaml:"allocatable" json:"allocatable"`
			} `yaml:"pools" json:"pools"`
		} `yaml:"resource_listen"`
	}
	if err := yaml.Unmarshal(output, &normalized); err != nil {
		return err
	}
	return json.NewEncoder(os.Stdout).Encode(normalized.ResourceListen)
}
