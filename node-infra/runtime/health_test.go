// SPDX-License-Identifier: Apache-2.0
package main

import (
	"encoding/json"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"testing"
)

func TestProxyReadinessDiscoversEffectiveOwnedListener(t *testing.T) {
	// A real kernel listener at a custom path exercises fd/inode discovery;
	// no static config or customer hook is available to the health probe.
	path := filepath.Join(t.TempDir(), "customer stats.sock")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	for _, scenario := range []string{"ready", "unready", "foreign-pid", "wrong-path", "generic-200"} {
		t.Run(scenario, func(t *testing.T) {
			server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				status := map[string]any{"pid": os.Getpid(), "stats_socket": path, "ready": true}
				switch scenario {
				case "unready":
					status["ready"] = false
				case "foreign-pid":
					status["pid"] = os.Getpid() + 1
				case "wrong-path":
					status["stats_socket"] = "/different.sock"
				case "generic-200":
					status = map[string]any{"message": "OK"}
				}
				json.NewEncoder(w).Encode(status)
			})}
			go server.Serve(listener)
			err := proxyReady(os.Getpid())
			server.Close()
			if (err == nil) != (scenario == "ready") {
				t.Fatalf("%s: %v", scenario, err)
			}
		})
		listener, err = net.Listen("unix", path)
		if err != nil {
			t.Fatal(err)
		}
	}
	listener.Close()
}

func TestNativeReadinessRequiresHTTP200(t *testing.T) {
	for _, code := range []int{200, 403, 503} {
		path := filepath.Join(t.TempDir(), "ready.sock")
		listener, err := net.Listen("unix", path)
		if err != nil {
			t.Fatal(err)
		}
		server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/readyz" {
				t.Errorf("wrong native endpoint: %s", r.URL.Path)
			}
			w.WriteHeader(code)
		})}
		go server.Serve(listener)
		err = unixReady(path, "/readyz")
		server.Close()
		if (err == nil) != (code == 200) {
			t.Fatalf("status %d: %v", code, err)
		}
	}
	if err := unixReady(filepath.Join(t.TempDir(), "missing"), "/readyz"); err == nil {
		t.Fatal("missing socket accepted")
	}
}
