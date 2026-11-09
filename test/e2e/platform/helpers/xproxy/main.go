// Exercise service EnvironmentFile and private configuration on every worker.
package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"

	"github.com/kuasar-sandbox/orchestrator/app/proxy"
)

type extension struct{ marker string }

func (*extension) Start(context.Context, proxy.WorkerHost) error { return nil }
func (e *extension) WrapIngress(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/node-infra-extension" {
			fmt.Fprint(w, e.marker)
			return
		}
		next.ServeHTTP(w, r)
	})
}
func main() {
	app := proxy.New(proxy.Hooks{Configure: func(_ context.Context, cfg *proxy.Config) error {
		// Deliberately differ from the YAML default. Readiness must discover
		// this effective native endpoint without replaying Configure.
		cfg.StatsSocket = "/run/sandbox/customer-proxy-stats.sock"
		return nil
	}, BindRuntime: func(_ context.Context, process proxy.Process, runtime *proxy.Runtime) error {
		data, err := os.ReadFile(os.Getenv("NODE_INFRA_TEST_CONFIG"))
		if err != nil {
			return err
		}
		if process.Role == proxy.RoleWorker {
			runtime.WorkerExtension = &extension{marker: strings.TrimSpace(string(data))}
		}
		return nil
	}})
	if err := app.Run(); err != nil {
		log.Fatal(err)
	}
}
