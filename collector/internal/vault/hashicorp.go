package vault

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"
)

// HashiCorpConfig configures the AppRole resolver.
type HashiCorpConfig struct {
	// Addr is the Vault server's base URL, e.g. https://vault.internal:8200.
	Addr string
	// RoleID and SecretID are this collector's AppRole credentials -
	// themselves secrets this process is trusted with directly, the same
	// as the mTLS/sealedbox key material it already holds on disk.
	RoleID   string
	SecretID string
	// SecretIDWrapped means SecretID is a Vault response-wrapping token,
	// not the secret_id itself - Vault's "push" delivery model for
	// AppRole's secret-zero problem: an operator generates a one-time
	// wrapped token out of band and this collector unwraps it exactly
	// once, on first use, rather than ever holding the raw secret_id in
	// its own configuration file.
	SecretIDWrapped bool
	// Namespace is a Vault Enterprise namespace header; empty for OSS
	// Vault or the root namespace.
	Namespace string
	// MountPath is the KV v2 secrets engine's mount point. A ref's path
	// (everything after "vault:hashicorp:") is relative to this mount,
	// e.g. mount "secret" and ref path "switches/core1" reads
	// secret/data/switches/core1 - the /data/ segment KV v2's API needs is
	// this resolver's job to insert, not something a ref has to spell out.
	MountPath string
	Timeout   time.Duration
}

// HashiCorpResolver reads AppRole-authenticated KV v2 secrets from Vault.
//
// NOTE: built against Vault's documented HTTP API (AppRole auth, response
// wrapping unwrap, KV v2 read) and covered by tests against a fake HTTP
// server reproducing that exact contract - it has not been exercised
// against a real Vault server in this environment. Treat the request/
// response shapes as reviewed, not as field-proven.
type HashiCorpResolver struct {
	cfg  HashiCorpConfig
	http *http.Client

	mu          sync.Mutex
	token       string
	tokenExpiry time.Time
}

func NewHashiCorpResolver(cfg HashiCorpConfig) *HashiCorpResolver {
	if cfg.MountPath == "" {
		cfg.MountPath = "secret"
	}
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	return &HashiCorpResolver{cfg: cfg, http: &http.Client{Timeout: timeout}}
}

func (r *HashiCorpResolver) Resolve(ctx context.Context, rest string) (map[string]any, error) {
	token, err := r.clientToken(ctx)
	if err != nil {
		return nil, fmt.Errorf("vault hashicorp: authenticate: %w", err)
	}
	path := strings.Trim(r.cfg.MountPath, "/") + "/data/" + strings.TrimPrefix(rest, "/")

	var out struct {
		Data struct {
			Data map[string]any `json:"data"`
		} `json:"data"`
	}
	if err := r.doJSON(ctx, http.MethodGet, "/v1/"+path, nil, token, &out); err != nil {
		return nil, fmt.Errorf("vault hashicorp: read %s: %w", path, err)
	}
	if out.Data.Data == nil {
		return nil, fmt.Errorf("vault hashicorp: %s has no data (wrong path, or KV v1 mount "+
			"configured as v2)", path)
	}
	return out.Data.Data, nil
}

// clientToken returns a cached, still-live AppRole login token, or performs
// a fresh login (unwrapping the secret_id first if configured to).
//
// A 60s margin against the lease's own expiry means a token is never
// presented so close to expiring that the read request racing the clock
// could land after Vault has already revoked it.
func (r *HashiCorpResolver) clientToken(ctx context.Context) (string, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.token != "" && time.Now().Add(60*time.Second).Before(r.tokenExpiry) {
		return r.token, nil
	}

	secretID := r.cfg.SecretID
	if r.cfg.SecretIDWrapped {
		unwrapped, err := r.unwrapSecretID(ctx)
		if err != nil {
			return "", fmt.Errorf("unwrap secret_id: %w", err)
		}
		secretID = unwrapped
		// The wrapping token is single-use by design - once unwrapped, the
		// raw secret_id above is what every subsequent login uses instead.
		r.cfg.SecretIDWrapped = false
		r.cfg.SecretID = secretID
	}

	var loginResp struct {
		Auth struct {
			ClientToken   string `json:"client_token"`
			LeaseDuration int    `json:"lease_duration"`
		} `json:"auth"`
	}
	body := map[string]string{"role_id": r.cfg.RoleID, "secret_id": secretID}
	if err := r.doJSON(ctx, http.MethodPost, "/v1/auth/approle/login", body, "", &loginResp); err != nil {
		return "", err
	}
	if loginResp.Auth.ClientToken == "" {
		return "", fmt.Errorf("approle login returned no client_token")
	}
	r.token = loginResp.Auth.ClientToken
	r.tokenExpiry = time.Now().Add(time.Duration(loginResp.Auth.LeaseDuration) * time.Second)
	return r.token, nil
}

func (r *HashiCorpResolver) unwrapSecretID(ctx context.Context) (string, error) {
	var out struct {
		Data struct {
			SecretID string `json:"secret_id"`
		} `json:"data"`
	}
	// The wrapping token IS the auth for this one call - Vault's unwrap
	// endpoint reads it from X-Vault-Token, not the request body.
	if err := r.doJSON(ctx, http.MethodPost, "/v1/sys/wrapping/unwrap", map[string]string{},
		r.cfg.SecretID, &out); err != nil {
		return "", err
	}
	if out.Data.SecretID == "" {
		return "", fmt.Errorf("unwrap response carried no secret_id")
	}
	return out.Data.SecretID, nil
}

func (r *HashiCorpResolver) doJSON(ctx context.Context, method, path string, body any,
	token string, out any) error {
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, strings.TrimRight(r.cfg.Addr, "/")+path, reader)
	if err != nil {
		return err
	}
	if token != "" {
		req.Header.Set("X-Vault-Token", token)
	}
	if r.cfg.Namespace != "" {
		req.Header.Set("X-Vault-Namespace", r.cfg.Namespace)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := r.http.Do(req)
	if err != nil {
		return err
	}
	defer func() { _, _ = io.Copy(io.Discard, resp.Body); _ = resp.Body.Close() }()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		raw, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("vault %s %s: HTTP %d: %s", method, path, resp.StatusCode, string(raw))
	}
	return json.NewDecoder(resp.Body).Decode(out)
}
