package vault

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

// fakeVault reproduces just enough of Vault's documented HTTP API (AppRole
// login, response-wrapping unwrap, KV v2 read) to prove HashiCorpResolver
// sends the requests Vault actually expects and parses the responses Vault
// actually sends - not a real Vault server, but a real HTTP contract test
// against real request/response shapes rather than an assumption about them.
type fakeVault struct {
	loginCalls  atomic.Int32
	unwrapCalls atomic.Int32
	readCalls   atomic.Int32

	wantRoleID, wantSecretID string
	wrappingToken            string // if set, secret_id must arrive wrapped
	realSecretID             string // what unwrap reveals

	leaseSeconds int
	kvPath       string // expected /v1/<mount>/data/<path>
	kvData       map[string]any
}

func (f *fakeVault) server(t *testing.T) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/sys/wrapping/unwrap":
			f.unwrapCalls.Add(1)
			if r.Header.Get("X-Vault-Token") != f.wrappingToken {
				http.Error(w, "bad wrapping token", http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"data": map[string]any{"secret_id": f.realSecretID},
			})

		case r.Method == http.MethodPost && r.URL.Path == "/v1/auth/approle/login":
			f.loginCalls.Add(1)
			var body struct {
				RoleID   string `json:"role_id"`
				SecretID string `json:"secret_id"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			if body.RoleID != f.wantRoleID || body.SecretID != f.wantSecretID {
				http.Error(w, "invalid role_id or secret_id", http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"auth": map[string]any{
					"client_token": "s.faketoken", "lease_duration": f.leaseSeconds,
				},
			})

		case r.Method == http.MethodGet && r.URL.Path == "/v1/"+f.kvPath:
			f.readCalls.Add(1)
			if r.Header.Get("X-Vault-Token") == "" {
				http.Error(w, "missing token", http.StatusForbidden)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"data": map[string]any{"data": f.kvData, "metadata": map[string]any{}},
			})

		default:
			http.NotFound(w, r)
		}
	}))
}

func TestHashiCorpResolverReadsAKVv2Secret(t *testing.T) {
	f := &fakeVault{
		wantRoleID: "role-1", wantSecretID: "secret-1", leaseSeconds: 3600,
		kvPath: "secret/data/switches/core1",
		kvData: map[string]any{"username": "admin", "password": "hunter2"},
	}
	srv := f.server(t)
	defer srv.Close()

	r := NewHashiCorpResolver(HashiCorpConfig{
		Addr: srv.URL, RoleID: "role-1", SecretID: "secret-1", MountPath: "secret",
	})
	out, err := r.Resolve(context.Background(), "switches/core1")
	if err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	if out["password"] != "hunter2" || out["username"] != "admin" {
		t.Errorf("Resolve = %+v", out)
	}
	if f.loginCalls.Load() != 1 || f.readCalls.Load() != 1 {
		t.Errorf("login calls = %d, read calls = %d, want 1 each",
			f.loginCalls.Load(), f.readCalls.Load())
	}
}

func TestHashiCorpResolverCachesTheTokenAcrossResolves(t *testing.T) {
	f := &fakeVault{
		wantRoleID: "role-1", wantSecretID: "secret-1", leaseSeconds: 3600,
		kvPath: "secret/data/x", kvData: map[string]any{"k": "v"},
	}
	srv := f.server(t)
	defer srv.Close()

	r := NewHashiCorpResolver(HashiCorpConfig{
		Addr: srv.URL, RoleID: "role-1", SecretID: "secret-1", MountPath: "secret",
	})
	for i := 0; i < 3; i++ {
		if _, err := r.Resolve(context.Background(), "x"); err != nil {
			t.Fatalf("Resolve #%d: %v", i, err)
		}
	}
	if f.loginCalls.Load() != 1 {
		t.Errorf("login calls = %d over 3 resolves, want 1 (the token should be cached)",
			f.loginCalls.Load())
	}
	if f.readCalls.Load() != 3 {
		t.Errorf("read calls = %d, want 3", f.readCalls.Load())
	}
}

func TestHashiCorpResolverReLoginsAfterTheLeaseExpires(t *testing.T) {
	f := &fakeVault{
		wantRoleID: "role-1", wantSecretID: "secret-1",
		leaseSeconds: 0, // already inside the 60s margin the moment it is issued
		kvPath:       "secret/data/x", kvData: map[string]any{"k": "v"},
	}
	srv := f.server(t)
	defer srv.Close()

	r := NewHashiCorpResolver(HashiCorpConfig{
		Addr: srv.URL, RoleID: "role-1", SecretID: "secret-1", MountPath: "secret",
	})
	if _, err := r.Resolve(context.Background(), "x"); err != nil {
		t.Fatalf("Resolve #1: %v", err)
	}
	if _, err := r.Resolve(context.Background(), "x"); err != nil {
		t.Fatalf("Resolve #2: %v", err)
	}
	if f.loginCalls.Load() != 2 {
		t.Errorf("login calls = %d, want 2 (a zero-lease token is never reused)",
			f.loginCalls.Load())
	}
}

func TestHashiCorpResolverUnwrapsAWrappedSecretIDExactlyOnce(t *testing.T) {
	f := &fakeVault{
		wantRoleID: "role-1", wantSecretID: "real-secret-id",
		wrappingToken: "wrap-token-xyz", realSecretID: "real-secret-id",
		leaseSeconds: 3600, kvPath: "secret/data/x", kvData: map[string]any{"k": "v"},
	}
	srv := f.server(t)
	defer srv.Close()

	r := NewHashiCorpResolver(HashiCorpConfig{
		Addr: srv.URL, RoleID: "role-1", SecretID: "wrap-token-xyz",
		SecretIDWrapped: true, MountPath: "secret",
	})
	if _, err := r.Resolve(context.Background(), "x"); err != nil {
		t.Fatalf("Resolve #1: %v", err)
	}
	if _, err := r.Resolve(context.Background(), "x"); err != nil {
		t.Fatalf("Resolve #2: %v", err)
	}
	if f.unwrapCalls.Load() != 1 {
		t.Errorf("unwrap calls = %d, want exactly 1 - a wrapping token is single-use "+
			"and the second Resolve reused the cached login token", f.unwrapCalls.Load())
	}
	if f.loginCalls.Load() != 1 {
		t.Errorf("login calls = %d, want 1", f.loginCalls.Load())
	}
}

func TestHashiCorpResolverFailsLoudlyOnBadCredentials(t *testing.T) {
	f := &fakeVault{wantRoleID: "role-1", wantSecretID: "the-real-one", leaseSeconds: 3600,
		kvPath: "secret/data/x"}
	srv := f.server(t)
	defer srv.Close()

	r := NewHashiCorpResolver(HashiCorpConfig{
		Addr: srv.URL, RoleID: "role-1", SecretID: "wrong-secret", MountPath: "secret",
	})
	if _, err := r.Resolve(context.Background(), "x"); err == nil {
		t.Fatal("Resolve succeeded with a wrong secret_id")
	}
}

func TestHashiCorpResolverTimeoutIsConfigurable(t *testing.T) {
	r := NewHashiCorpResolver(HashiCorpConfig{Addr: "http://127.0.0.1:1", Timeout: 5 * time.Millisecond})
	if _, err := r.Resolve(context.Background(), "x"); err == nil {
		t.Fatal("expected a connection failure against a closed port")
	}
}
