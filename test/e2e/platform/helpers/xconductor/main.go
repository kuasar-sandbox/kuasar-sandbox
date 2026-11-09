// A native App SDK fixture; the official node-ctl bootstrap remains the entry.
package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"

	"github.com/kuasar-sandbox/orchestrator/app/conductor"
)

type extension struct{ marker string }

func (*extension) Start(context.Context, conductor.Host) error { return nil }
func (e *extension) WrapAPI(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/node-infra-extension" {
			fmt.Fprint(w, e.marker)
			return
		}
		next.ServeHTTP(w, r)
	})
}
func main() {
	app := conductor.New(conductor.Hooks{Configure: func(_ context.Context, _ *conductor.Config, runtime *conductor.Runtime) error {
		data, err := os.ReadFile(os.Getenv("NODE_INFRA_TEST_CONFIG"))
		if err != nil {
			return err
		}
		runtime.Extension = &extension{marker: strings.TrimSpace(string(data))}
		return nil
	}})
	if err := app.Run(); err != nil {
		log.Fatal(err)
	}
}
