// SPDX-License-Identifier: Apache-2.0
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"github.com/vishvananda/netlink"
	"github.com/vishvananda/netns"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"syscall"
	"time"
)

var egressConfigPath = "/etc/connector-egress/gateway.json"
var egressLinkPath = "/etc/connector-egress/link.json"

type egressLink struct {
	Name              string `json:"name"`
	OwnerNS           string `json:"owner_netns"`
	GatewayNS         string `json:"gateway_netns"`
	SwitchNS          string `json:"switch_netns"`
	Switch            string `json:"switch"`
	Transit           string `json:"transit"`
	Gateway           string `json:"gateway"`
	MTU               int    `json:"mtu"`
	StateDir          string `json:"state_dir"`
	GatewayRuntimeDir string `json:"gateway_runtime_dir"`
}
type egressRecord struct {
	RuntimeDir string      `json:"runtime_dir"`
	Link       *egressLink `json:"link,omitempty"`
}

func describeEgress(r *record) (*egressRecord, error) {
	data, err := os.ReadFile(egressConfigPath)
	if errors.Is(err, os.ErrNotExist) {
		if _, err := os.Stat(egressLinkPath); !errors.Is(err, os.ErrNotExist) {
			return nil, errors.New("local Egress link requires gateway.json")
		}
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	// The selected Connector parser remains the schema authority.
	if output, err := exec.Command(connector, "egress", "validate", "--config", egressConfigPath).CombinedOutput(); err != nil {
		return nil, fmt.Errorf("Egress configuration: %w: %s", err, output)
	}
	var gateway struct {
		Name       string `json:"name"`
		RuntimeDir string `json:"runtime_dir"`
	}
	if err := json.Unmarshal(data, &gateway); err != nil {
		return nil, err
	}
	if gateway.RuntimeDir == "" {
		gateway.RuntimeDir = filepath.Join("/run/connector-egress", gateway.Name)
	}
	result := &egressRecord{RuntimeDir: gateway.RuntimeDir}
	data, err = os.ReadFile(egressLinkPath)
	if errors.Is(err, os.ErrNotExist) {
		return result, nil
	}
	if err != nil {
		return nil, err
	}
	var link egressLink
	decoder := json.NewDecoder(strings.NewReader(string(data)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&link); err != nil {
		return nil, err
	}
	if link.OwnerNS != "" || link.Switch != r.SwitchName || link.SwitchNS != r.SwitchNetNS || link.Transit != r.Transit.Name {
		return nil, errors.New("local Egress must use the retained primary owner and selected switch/transit")
	}
	for _, name := range []string{link.Name, link.GatewayNS, link.Transit, link.Gateway} {
		if err := safeName(name); err != nil {
			return nil, err
		}
	}
	if link.GatewayNS == r.SwitchNetNS || link.GatewayNS == r.PortNetNS || link.Gateway == link.Transit || link.MTU < 128 || link.MTU > 65535 {
		return nil, errors.New("invalid local Egress namespace, endpoint or MTU")
	}
	stateRoot := filepath.Dir(recordPath)
	if link.StateDir == "" {
		link.StateDir = filepath.Join(stateRoot, "egress-link")
	}
	relative, err := filepath.Rel(stateRoot, link.StateDir)
	if err != nil || relative == "." || relative == ".." || strings.HasPrefix(relative, "../") || !filepath.IsAbs(link.StateDir) {
		return nil, errors.New("local Egress state_dir must be inside the retained node recovery directory")
	}
	if link.GatewayRuntimeDir != result.RuntimeDir {
		return nil, errors.New("local Egress runtime directory mismatch")
	}
	result.Link = &link
	return result, nil
}
func sameEgress(a, b *egressRecord) bool { return reflect.DeepEqual(a, b) }
func localEgress(r *record) bool         { return r.Egress != nil && r.Egress.Link != nil }
func savedEgressLink(r *record) (string, error) {
	path := filepath.Join(filepath.Dir(recordPath), "egress-link.json")
	data, err := json.Marshal(r.Egress.Link)
	if err != nil {
		return "", err
	}
	// This is a node deployment ownership record, never a gateway policy source.
	if err = os.WriteFile(path, data, 0600); err != nil {
		return "", err
	}
	return path, nil
}
func egressLinkCommand(r *record, operation string) error {
	path, err := savedEgressLink(r)
	if err != nil {
		return err
	}
	return execute(connector, "egress", "link", operation, "--config", path)
}
func verifyEgressLink(r *record) error {
	if !localEgress(r) || r.State == "releasing" {
		return nil
	}
	if _, err := os.Stat(filepath.Join(r.Egress.Link.StateDir, "state.json")); errors.Is(err, os.ErrNotExist) && r.State == "preparing" {
		return nil
	}
	path, err := savedEgressLink(r)
	if err != nil {
		return err
	}
	output, err := exec.Command(connector, "egress", "link", "check", "--config", path).CombinedOutput()
	if err != nil {
		return fmt.Errorf("local Egress ownership: %w: %s", err, output)
	}
	return nil
}
func egressReady(r *record) error {
	if r.Egress == nil {
		return nil
	}
	var status struct {
		PID        int    `json:"pid"`
		NativePID  int    `json:"native_pid"`
		Generation uint64 `json:"configuration_generation"`
		Interfaces []struct {
			Up      bool `json:"up"`
			Carrier bool `json:"carrier"`
		} `json:"interfaces"`
	}
	path := filepath.Join(r.Egress.RuntimeDir, "status.json")
	info, err := os.Stat(path)
	if err != nil {
		return err
	}
	if time.Since(info.ModTime()) > 5*time.Second {
		return errors.New("Egress status is stale")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if err = json.Unmarshal(data, &status); err != nil {
		return err
	}
	output, err := exec.Command("systemctl", "show", "connector-egress.service", "--property=MainPID", "--value").Output()
	if err != nil {
		return err
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(output)))
	if err != nil || pid <= 1 || pid != status.PID || status.NativePID <= 1 || status.Generation == 0 || len(status.Interfaces) == 0 {
		return errors.New("Egress has no current configured data plane")
	}
	if err = syscall.Kill(status.NativePID, 0); err != nil {
		return err
	}
	for _, port := range status.Interfaces {
		if !port.Up || !port.Carrier {
			return errors.New("Egress interface is unavailable")
		}
	}
	if localEgress(r) {
		path, err := savedEgressLink(r)
		if err != nil {
			return err
		}
		output, err := exec.Command(connector, "egress", "link", "check", "--config", path).Output()
		if err != nil {
			return err
		}
		var link struct {
			PairValid        bool `json:"pair_valid"`
			SwitchRunning    bool `json:"switch_running"`
			RecoveryRequired bool `json:"recovery_required"`
		}
		if err = json.Unmarshal(output, &link); err != nil {
			return err
		}
		if !link.PairValid || !link.SwitchRunning || link.RecoveryRequired {
			return errors.New("local Egress wiring requires recovery")
		}
	}
	return nil
}

// Refuse to destroy a Pool owner's devices when the gateway namespace was
// created by this deployment. Borrowed gateway namespaces retain their owner.
func egressNamespaceEmpty(r *record) error {
	if !localEgress(r) {
		return nil
	}
	for _, ns := range r.Namespaces {
		if ns.Name != r.Egress.Link.GatewayNS || ns.External {
			continue
		}
		handle, err := netns.GetFromName(ns.Name)
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		if err != nil {
			return err
		}
		defer handle.Close()
		links, err := netlink.NewHandleAt(handle)
		if err != nil {
			return err
		}
		defer links.Close()
		devices, err := links.LinkList()
		if err != nil {
			return err
		}
		for _, device := range devices {
			if device.Attrs().Name != "lo" {
				return fmt.Errorf("gateway namespace still contains Pool-owned device %s", device.Attrs().Name)
			}
		}
	}
	return nil
}
