// A static guest workload for the delivered node-infra platform tests.
package main

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"
)

const marker = "/state/node-infra-marker"

func main() {
	if len(os.Args) > 1 && os.Args[1] == "read" {
		value, err := os.ReadFile(marker)
		if err != nil {
			log.Fatal(err)
		}
		fmt.Print(string(value))
		return
	}
	if err := os.MkdirAll("/state", 0755); err != nil {
		log.Fatal(err)
	}
	value := make([]byte, 16)
	if _, err := rand.Read(value); err != nil {
		log.Fatal(err)
	}
	token := hex.EncodeToString(value)
	var mutex sync.Mutex
	var envdOnce sync.Once
	var envdError error
	count := 0
	http.HandleFunc("/start-envd", func(w http.ResponseWriter, r *http.Request) {
		envdOnce.Do(func() {
			// Exercise the delivered runtime's real Files service. The fixture
			// itself only supplies the workload and never implements that API.
			cmd := exec.Command("/opt/sandbox-runtime/bin/envd", "-isnotfc", "-port", "49983")
			cmd.Stdout, cmd.Stderr = os.Stderr, os.Stderr
			envdError = cmd.Start()
			if envdError == nil {
				go func() { _ = cmd.Wait() }()
			}
		})
		if envdError != nil {
			http.Error(w, envdError.Error(), 500)
		}
	})
	http.HandleFunc("/state", func(w http.ResponseWriter, r *http.Request) {
		mutex.Lock()
		defer mutex.Unlock()
		if r.Method == http.MethodPost {
			data, err := io.ReadAll(io.LimitReader(r.Body, 4097))
			if err != nil || len(data) > 4096 {
				http.Error(w, "invalid marker", 400)
				return
			}
			if err := os.WriteFile(marker, data, 0600); err != nil {
				http.Error(w, err.Error(), 500)
				return
			}
		}
		count++
		disk, err := os.ReadFile(marker)
		if err != nil && !os.IsNotExist(err) {
			http.Error(w, err.Error(), 500)
			return
		}
		boot, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
		if err != nil {
			http.Error(w, err.Error(), 500)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"token": token, "count": count, "disk": string(disk), "boot_id": strings.TrimSpace(string(boot))})
	})
	http.HandleFunc("/mmds", func(w http.ResponseWriter, r *http.Request) {
		vip := r.URL.Query().Get("ip")
		if vip == "" {
			vip = "169.254.169.254"
		}
		if ip := net.ParseIP(vip); ip == nil || ip.To4() == nil {
			http.Error(w, "invalid MMDS test VIP", 400)
			return
		}
		endpoint := "http://" + vip
		client := &http.Client{Timeout: 5 * time.Second}
		request, _ := http.NewRequest(http.MethodPut, endpoint+"/latest/api/token", nil)
		request.Header.Set("X-metadata-token-ttl-seconds", "60")
		response, err := client.Do(request)
		if err != nil {
			http.Error(w, err.Error(), 502)
			return
		}
		token, err := io.ReadAll(io.LimitReader(response.Body, 4096))
		response.Body.Close()
		if err != nil || response.StatusCode != 200 {
			http.Error(w, "MMDS token failed", 502)
			return
		}
		request, _ = http.NewRequest(http.MethodGet, endpoint+"/", nil)
		request.Header.Set("X-metadata-token", string(token))
		response, err = client.Do(request)
		if err != nil {
			http.Error(w, err.Error(), 502)
			return
		}
		defer response.Body.Close()
		w.WriteHeader(response.StatusCode)
		_, _ = io.Copy(w, io.LimitReader(response.Body, 4096))
	})
	http.HandleFunc("/egress", func(w http.ResponseWriter, r *http.Request) {
		client := &http.Client{Timeout: 5 * time.Second}
		response, err := client.Get(r.URL.Query().Get("url"))
		if err != nil {
			http.Error(w, err.Error(), 502)
			return
		}
		defer response.Body.Close()
		w.WriteHeader(response.StatusCode)
		_, _ = io.Copy(w, io.LimitReader(response.Body, 4096))
	})
	http.HandleFunc("/udp", func(w http.ResponseWriter, r *http.Request) {
		connection, err := net.DialTimeout("udp", r.URL.Query().Get("address"), 5*time.Second)
		if err != nil {
			http.Error(w, err.Error(), 502)
			return
		}
		defer connection.Close()
		_ = connection.SetDeadline(time.Now().Add(5 * time.Second))
		if _, err := connection.Write([]byte(r.URL.Query().Get("marker"))); err != nil {
			http.Error(w, err.Error(), 502)
			return
		}
		data := make([]byte, 4096)
		n, err := connection.Read(data)
		if err != nil {
			http.Error(w, err.Error(), 502)
			return
		}
		_, _ = w.Write(data[:n])
	})
	http.HandleFunc("/otlp", func(w http.ResponseWriter, r *http.Request) {
		payload := fmt.Sprintf(`{"resourceMetrics":[{"scopeMetrics":[{"scope":{"name":"node-infra"},"metrics":[{"name":"node_infra_probe","gauge":{"dataPoints":[{"timeUnixNano":"%d","asDouble":17}]}}]}]}]}`, time.Now().UnixNano())
		client := &http.Client{Timeout: 5 * time.Second}
		response, err := client.Post("http://169.254.169.254:4318/v1/metrics", "application/json", bytes.NewBufferString(payload))
		if err != nil {
			http.Error(w, err.Error(), 502)
			return
		}
		defer response.Body.Close()
		w.WriteHeader(response.StatusCode)
		_, _ = io.Copy(w, io.LimitReader(response.Body, 4096))
	})
	log.Fatal(http.ListenAndServe(":8000", nil))
}
