// SPDX-License-Identifier: Apache-2.0
package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

func consumerTemplates() ([]string, error) {
	output, err := exec.Command("/usr/libexec/node-infra/service", "config", "conductor").Output()
	if err != nil {
		return nil, err
	}
	var cfg struct {
		Units struct {
			Runner      string `yaml:"runner"`
			Builder     string `yaml:"builder"`
			RunnerPools []struct {
				Unit string `yaml:"unit"`
			} `yaml:"runner_pools"`
			BuilderPools []struct {
				Unit string `yaml:"unit"`
			} `yaml:"builder_pools"`
		} `yaml:"units"`
	}
	if err := yaml.Unmarshal(output, &cfg); err != nil {
		return nil, err
	}
	units := []string{"sandbox-runner@.service", "sandbox-builder@.service"}
	for _, unit := range []string{cfg.Units.Runner, cfg.Units.Builder} {
		if unit != "" {
			units = append(units, unit)
		}
	}
	for _, p := range cfg.Units.RunnerPools {
		if p.Unit != "" {
			units = append(units, p.Unit)
		}
	}
	for _, p := range cfg.Units.BuilderPools {
		if p.Unit != "" {
			units = append(units, p.Unit)
		}
	}
	for _, unit := range units {
		if err := safeName(unit); err != nil {
			return nil, err
		}
		if strings.ContainsAny(unit, "*?[]") || !strings.HasSuffix(unit, ".service") {
			return nil, fmt.Errorf("invalid native consumer unit %q", unit)
		}
	}
	return units, nil
}

func consumerUnit(name string, templates []string) bool {
	for _, unit := range templates {
		if name == unit {
			return true
		}
		if i := strings.Index(unit, "@."); i >= 0 && strings.HasPrefix(name, unit[:i+1]) && strings.HasSuffix(name, unit[i+1:]) {
			return true
		}
	}
	return false
}

// Enumerate actual live cgroups as well as native template names. Drop-ins may
// place a pool outside sandbox.slice, and multiple pools may share a template.
func consumerGroups(r *record) ([]string, error) {
	var groups []string
	err := filepath.WalkDir("/sys/fs/cgroup", func(path string, entry os.DirEntry, err error) error {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		if err != nil {
			return err
		}
		if !entry.IsDir() {
			return nil
		}
		if consumerUnit(entry.Name(), r.ConsumerUnits) || entry.Name() == "sandbox-runner.slice" || entry.Name() == "sandbox-builder.slice" {
			groups = append(groups, path)
			return filepath.SkipDir
		}
		return nil
	})
	return groups, err
}

func populated(path string) (bool, error) {
	data, err := os.ReadFile(filepath.Join(path, "cgroup.events"))
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	return strings.Contains(string(data), "populated 1"), err
}

func consumersGone(r *record) error {
	if init, _ := os.ReadFile("/proc/1/comm"); strings.TrimSpace(string(init)) == "systemd" {
		for _, unit := range []string{"node-conductor.service", "node-proxy.service", "node-telemetry.service", "connector-vswitch.service"} {
			output, err := exec.Command("systemctl", "show", unit, "-p", "ActiveState", "--value").Output()
			if err != nil {
				return err
			}
			state := strings.TrimSpace(string(output))
			if state != "inactive" && state != "failed" {
				return fmt.Errorf("producer %s is still %s", unit, state)
			}
		}
	}
	groups, err := consumerGroups(r)
	if err != nil {
		return err
	}
	for _, group := range groups {
		live, err := populated(group)
		if err != nil {
			return err
		}
		if live {
			return fmt.Errorf("consumer cgroup %s is still populated", group)
		}
	}
	// Check real processes too, including VMs whose custom unit moved them to
	// another cgroup. The cleanup helper itself is a different executable.
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if _, err := strconv.Atoi(entry.Name()); err != nil {
			continue
		}
		path := filepath.Join("/proc", entry.Name())
		executable, err := os.Readlink(filepath.Join(path, "exe"))
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return err
		}
		base := strings.TrimSuffix(filepath.Base(executable), " (deleted)")
		if base == "cloud-hypervisor" || base == "sandbox-ctl" || base == "node-ctl" || base == "connector-ctl" {
			return fmt.Errorf("native producer/consumer PID %s (%s) is still running", entry.Name(), base)
		}
	}
	return nil
}

func drain(timeout time.Duration) error {
	if timeout <= 0 {
		return errors.New("drain timeout must be positive")
	}
	r, err := readRecord()
	if err != nil {
		return err
	}
	// New work has stopped by unit ordering. Stop every recorded native pool
	// and wait for real cgroup emptiness, including nested VM descendants.
	output, err := exec.Command("systemctl", "list-units", "--type=service", "--all", "--plain", "--no-legend").Output()
	if err != nil {
		return err
	}
	var units []string
	for _, line := range strings.Split(string(output), "\n") {
		fields := strings.Fields(line)
		if len(fields) > 0 && consumerUnit(fields[0], r.ConsumerUnits) {
			units = append(units, fields[0])
		}
	}
	units = append(units, "sandbox-runner.slice", "sandbox-builder.slice")
	if err := execute("systemctl", append([]string{"stop", "--no-block"}, units...)...); err != nil {
		return err
	}
	deadline := time.Now().Add(timeout)
	for {
		groups, err := consumerGroups(r)
		if err != nil {
			return err
		}
		live := false
		for _, group := range groups {
			occupied, err := populated(group)
			if err != nil {
				return err
			}
			live = live || occupied
		}
		if !live {
			return nil
		}
		if time.Now().After(deadline) {
			return errors.New("consumer cgroups remain populated; network cleanup will refuse")
		}
		time.Sleep(100 * time.Millisecond)
	}
}
