// SPDX-License-Identifier: Apache-2.0
package main

import (
	"net"
	"net/http"
	"path/filepath"
	"testing"
)

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
