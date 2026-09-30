package preflight

import (
	"context"
	"fmt"
	"io"
	"net/http"
)

// coreReachable is a GET against the platform's own liveness endpoint
// (GET /api/v1/health, unauthenticated by design) through the exact
// *http.Client the rest of this collector uses - TLS config, client
// certificate if enrolled, all of it - so a pass here means what it looks
// like it means: not just "DNS resolves and TCP connects", but "this
// collector, configured exactly as it is, can actually talk to the core".
func coreReachable(ctx context.Context, client *http.Client, baseURL string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+"/api/v1/health", nil)
	if err != nil {
		return err
	}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("GET %s/api/v1/health: %w", baseURL, err)
	}
	defer func() { _, _ = io.Copy(io.Discard, resp.Body); _ = resp.Body.Close() }()
	if resp.StatusCode >= 500 {
		return fmt.Errorf("GET %s/api/v1/health: %s", baseURL, resp.Status)
	}
	return nil
}
