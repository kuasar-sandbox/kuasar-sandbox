// SPDX-License-Identifier: Apache-2.0
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

func unixResponse(socket, path string, result any) error {
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", socket)
	}}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 2 * time.Second}
	response, err := client.Get("http://localhost" + path)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("native readiness endpoint %s returned HTTP %d", path, response.StatusCode)
	}
	// Do not emit a response body that could contain customer configuration.
	if result != nil {
		return json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(result)
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1<<20))
	return nil
}

func unixReady(socket, path string) error { return unixResponse(socket, path, nil) }

var unixListener = regexp.MustCompile(`^\S+\s+\S+\s+\S+\s+00010000\s+0001\s+01\s+(\d+)\s+(/.*)$`)

// Discover the running master's native UNIX listeners, not the pre-Configure
// YAML defaults. Linux exposes socket inode ownership via fd and net/unix.
// Neither discovery nor probing executes the customer's Configure hook.
func unixListeners(proc string) ([]string, error) {
	entries, err := os.ReadDir(filepath.Join(proc, "fd"))
	if err != nil {
		return nil, err
	}
	owned := map[string]bool{}
	for _, entry := range entries {
		target, err := os.Readlink(filepath.Join(proc, "fd", entry.Name()))
		if err == nil {
			owned[target] = true
		}
	}
	table, err := os.ReadFile(filepath.Join(proc, "net/unix"))
	if err != nil {
		return nil, err
	}
	var paths []string
	for _, line := range strings.Split(string(table), "\n") {
		match := unixListener.FindStringSubmatch(strings.TrimSpace(line))
		if match != nil && owned["socket:["+match[1]+"]"] {
			paths = append(paths, match[2])
		}
	}
	return paths, nil
}

func proxyReady(pid int) error {
	paths, err := unixListeners(fmt.Sprintf("/proc/%d", pid))
	if err != nil {
		return err
	}
	for _, socket := range paths {
		var status struct {
			Ready       bool   `json:"ready"`
			PID         int    `json:"pid"`
			StatsSocket string `json:"stats_socket"`
		}
		if unixResponse(socket, "/readyz", &status) == nil && status.Ready && status.PID == pid && status.StatsSocket == socket {
			return nil
		}
	}
	return fmt.Errorf("Proxy master %d has no ready native management listener", pid)
}

func health() error {
	output, err := exec.Command("/usr/libexec/node-infra/service", "config", "proxy").Output()
	if err != nil {
		return err
	}
	var normalized struct {
		ConfigSocket string `yaml:"config_socket"`
	}
	if err := yaml.Unmarshal(output, &normalized); err != nil {
		return err
	}
	if err := unixReady(normalized.ConfigSocket, "/internal/admin/resource-pressure"); err != nil {
		return err
	}
	// The native endpoint requires workers, listeners and routes to be ready;
	// an active systemd Type=simple process by itself proves none of these.
	output, err = exec.Command("systemctl", "show", "node-proxy.service", "--property=MainPID", "--value").Output()
	if err != nil {
		return err
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(output)))
	if err != nil || pid <= 1 {
		return fmt.Errorf("Proxy has no running master")
	}
	return proxyReady(pid)
}
