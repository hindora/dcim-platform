package assign

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/hari/dcim-platform/collector/internal/config"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/internal/vault"
)

type stubResolver struct {
	out map[string]any
	err error
}

func (s *stubResolver) Resolve(context.Context, string) (map[string]any, error) {
	return s.out, s.err
}

func newTestClient(t *testing.T, baseURL string) *Client {
	t.Helper()
	cfg := config.Default()
	cfg.Collector.ID = "col-1"
	cfg.DCIM.BaseURL = baseURL
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	return New(cfg, log, obs.NewMetrics(), nil, nil)
}

func assignmentBody(credentialJSON string) string {
	return fmt.Sprintf(`{
		"version": 1, "generated_at": "2026-01-01T00:00:00Z", "collector_id": "col-1",
		"site": "", "endpoints": [{
			"id": "ep-1", "device_id": "dev-1", "device_name": "dev-1",
			"device_type": "server", "protocol": "redfish", "role": "bmc",
			"address": "10.0.0.1", "port": 443, "addressing": {},
			"via_endpoint_id": "",
			"credential": %s,
			"poll": {"interval_s": 60, "timeout_ms": 3000, "retries": 2,
				"metric_groups": [], "push_enabled": false}
		}], "resolve": []
	}`, credentialJSON)
}

func TestRefreshResolvesACredentialRefThroughTheRegistry(t *testing.T) {
	body := assignmentBody(
		`{"kind": "credential_ref", "data": {"ref": "vault:hashicorp:bmc/core1"}}`)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(body))
	}))
	defer server.Close()

	client := newTestClient(t, server.URL)
	reg := vault.NewRegistry()
	reg.Register("hashicorp", &stubResolver{
		out: map[string]any{"username": "admin", "password": "resolved-secret"}})
	client.SetVaultRegistry(reg)

	if err := client.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh: %v", err)
	}
	eps := client.Endpoints()
	if len(eps) != 1 {
		t.Fatalf("got %d endpoints, want 1", len(eps))
	}
	if got := eps[0].Credential.Data["password"]; got != "resolved-secret" {
		t.Errorf("resolved credential password = %v, want resolved-secret", got)
	}
}

func TestRefreshLeavesARefUnresolvedWithoutARegistry(t *testing.T) {
	body := assignmentBody(
		`{"kind": "credential_ref", "data": {"ref": "vault:hashicorp:bmc/core1"}}`)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(body))
	}))
	defer server.Close()

	client := newTestClient(t, server.URL)
	// No SetVaultRegistry call - the ordinary case for a deployment with no
	// credential_ref endpoints at all.
	if err := client.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh: %v", err)
	}
	got := client.Endpoints()[0].Credential.Data["ref"]
	if got != "vault:hashicorp:bmc/core1" {
		t.Errorf("unresolved ref = %v, want the reference left intact", got)
	}
}

func TestRefreshLeavesARefUnresolvedWhenTheResolverFails(t *testing.T) {
	body := assignmentBody(
		`{"kind": "credential_ref", "data": {"ref": "vault:hashicorp:bmc/core1"}}`)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(body))
	}))
	defer server.Close()

	client := newTestClient(t, server.URL)
	reg := vault.NewRegistry()
	reg.Register("hashicorp", &stubResolver{err: errors.New("vault is sealed")})
	client.SetVaultRegistry(reg)

	if err := client.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh: %v", err)
	}
	got := client.Endpoints()[0].Credential.Data["ref"]
	if got != "vault:hashicorp:bmc/core1" {
		t.Errorf("unresolved ref after a resolver failure = %v, want the "+
			"reference left intact, not cleared", got)
	}
}

func TestRefreshDoesNotTouchAnOrdinaryCredential(t *testing.T) {
	body := assignmentBody(
		`{"kind": "http_basic", "data": {"username": "admin", "password": "plain"}}`)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(body))
	}))
	defer server.Close()

	client := newTestClient(t, server.URL)
	reg := vault.NewRegistry()
	reg.Register("hashicorp", &stubResolver{
		out: map[string]any{"password": "should-never-be-used"}})
	client.SetVaultRegistry(reg)

	if err := client.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh: %v", err)
	}
	if got := client.Endpoints()[0].Credential.Data["password"]; got != "plain" {
		t.Errorf("a non-ref credential's password = %v, want it left as plain", got)
	}
}
