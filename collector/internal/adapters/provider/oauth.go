// Package provider polls a colo/facility provider's own API for telemetry
// this collector has no network path to get any other way - a tenant's cage
// has no SNMP, BACnet or Modbus access to the provider's own smart PDUs and
// environmental sensors, only whatever the provider's portal exposes over
// REST (docs/26 Phase 10; Equinix Smart View / API Plus is the concrete
// example the plan names).
package provider

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// TokenSource fetches and caches an OAuth2 client-credentials bearer token
// (RFC 6749 SS4.4) - the grant machine-to-machine provider APIs use, with no
// user in the loop to redirect through an authorization code flow.
type TokenSource struct {
	tokenURL     string
	clientID     string
	clientSecret string
	httpClient   *http.Client

	mu     sync.Mutex
	token  string
	expiry time.Time
}

func NewTokenSource(tokenURL, clientID, clientSecret string, httpClient *http.Client) *TokenSource {
	return &TokenSource{
		tokenURL: tokenURL, clientID: clientID, clientSecret: clientSecret,
		httpClient: httpClient,
	}
}

// Token returns a currently-valid bearer token, fetching or refreshing one as
// needed. A held lock across the fetch is deliberate: two polls racing to
// refresh at once would send the provider two token requests for one
// endpoint, and provider token endpoints are exactly the kind of thing that
// rate-limits.
func (ts *TokenSource) Token(ctx context.Context) (string, error) {
	ts.mu.Lock()
	defer ts.mu.Unlock()
	if ts.token != "" && time.Now().Before(ts.expiry) {
		return ts.token, nil
	}
	return ts.fetch(ctx)
}

// Invalidate drops the cached token, so the next Token call re-authenticates
// instead of retrying a token the provider has already rejected once.
func (ts *TokenSource) Invalidate() {
	ts.mu.Lock()
	ts.token = ""
	ts.mu.Unlock()
}

type tokenResponse struct {
	AccessToken string `json:"access_token"`
	ExpiresIn   int    `json:"expires_in"`
}

func (ts *TokenSource) fetch(ctx context.Context) (string, error) {
	form := url.Values{}
	form.Set("grant_type", "client_credentials")
	form.Set("client_id", ts.clientID)
	form.Set("client_secret", ts.clientSecret)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, ts.tokenURL,
		strings.NewReader(form.Encode()))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")

	resp, err := ts.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("token request to %s: %w", ts.tokenURL, err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("token request to %s: %s: %s",
			ts.tokenURL, resp.Status, strings.TrimSpace(string(body)))
	}
	var tr tokenResponse
	if err := json.Unmarshal(body, &tr); err != nil {
		return "", fmt.Errorf("token response from %s: %w", ts.tokenURL, err)
	}
	if tr.AccessToken == "" {
		return "", fmt.Errorf("token response from %s carried no access_token", ts.tokenURL)
	}

	ttl := time.Duration(tr.ExpiresIn) * time.Second
	if ttl <= 0 {
		ttl = 5 * time.Minute // a provider that omits expires_in; conservative
	}
	// Refreshed a tenth early so a request in flight never races a token that
	// expires mid-poll.
	ts.token = tr.AccessToken
	ts.expiry = time.Now().Add(ttl - ttl/10)
	return ts.token, nil
}
