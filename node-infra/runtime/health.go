// SPDX-License-Identifier: Apache-2.0
package main

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"os/exec"
	"time"

	"gopkg.in/yaml.v3"
)

func unixReady(socket, path string) error {
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
	// Do not emit a response body that could contain customer configuration.
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1<<20))
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("native readiness endpoint %s returned HTTP %d", path, response.StatusCode)
	}
	return nil
}

func health() error {
	output, err := exec.Command("/usr/libexec/node-infra/service", "config", "proxy").Output()
	if err != nil {
		return err
	}
	var normalized struct {
		ConfigSocket string `yaml:"config_socket"`
		StatsSocket  string `yaml:"stats_socket"`
	}
	if err := yaml.Unmarshal(output, &normalized); err != nil {
		return err
	}
	if err := unixReady(normalized.ConfigSocket, "/internal/admin/resource-pressure"); err != nil {
		return err
	}
	// The native endpoint requires workers, listeners and routes to be ready;
	// an active systemd Type=simple process by itself proves none of these.
	return unixReady(normalized.StatsSocket, "/readyz")
}
