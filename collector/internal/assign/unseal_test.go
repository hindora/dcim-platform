package assign

import (
	"context"
	"encoding/base64"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/hari/dcim-platform/collector/internal/config"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/internal/sealedbox"
)

// TestRefreshUnsealsARealPythonSealedCredential is the end-to-end proof for
// docs/26 Phase 4's credential sealing on the collector side: a real HTTP
// fetch, a real JSON assignment body carrying a credential sealed by an
// actual call to backend/app/services/sealed_credential.py's
// seal_for_collector (not reconstructed in Go), unsealed through the exact
// path Refresh uses in production. If this package's construction ever
// silently diverges from the Python encoder's, this is what catches it -
// internal/sealedbox's own tests only prove Go decodes what Go encoded.
func TestRefreshUnsealsARealPythonSealedCredential(t *testing.T) {
	privB64 := "BwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwc="
	sealedB64 := "ZqilLmeMRUVZVwILs7BISgPPUmk13Fp0d2kwG1R5mkzi+RCCDzxMvBTtoAjSQklw" +
		"9P60WNU0e9g8PvCsE5G7foaPsS+ZKCbLpDhjSaAVhwv2FEdDpub8wQYdaDfhXMksvQ=="

	body := fmt.Sprintf(`{
		"version": 1, "generated_at": "2026-01-01T00:00:00Z", "collector_id": "col-1",
		"site": "", "endpoints": [{
			"id": "ep-1", "device_id": "dev-1", "device_name": "dev-1",
			"device_type": "switch", "protocol": "snmp", "role": "primary",
			"address": "10.0.0.1", "port": 161, "addressing": {},
			"via_endpoint_id": "",
			"credential": {"kind": "snmp_v2c", "data": null, "sealed_b64": %q},
			"poll": {"interval_s": 60, "timeout_ms": 3000, "retries": 2,
				"metric_groups": [], "push_enabled": false}
		}], "resolve": []
	}`, sealedB64)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(body))
	}))
	defer server.Close()

	privRaw, err := base64.StdEncoding.DecodeString(privB64)
	if err != nil {
		t.Fatalf("decode private key fixture: %v", err)
	}
	kp, err := sealedbox.Load(privRaw)
	if err != nil {
		t.Fatalf("sealedbox.Load: %v", err)
	}

	cfg := config.Default()
	cfg.Collector.ID = "col-1"
	cfg.DCIM.BaseURL = server.URL

	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	client := New(cfg, log, obs.NewMetrics(), nil, kp)
	if err := client.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh: %v", err)
	}

	eps := client.Endpoints()
	if len(eps) != 1 {
		t.Fatalf("got %d endpoints, want 1", len(eps))
	}
	cred := eps[0].Credential
	if cred == nil {
		t.Fatal("credential is nil after Refresh")
	}
	if cred.Sealed != "" {
		t.Errorf("Sealed was not cleared after unsealing: %q", cred.Sealed)
	}
	if got := cred.Community(); got != "assignment-e2e-secret" {
		t.Errorf("unsealed community = %q, want assignment-e2e-secret", got)
	}
}
