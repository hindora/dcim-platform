package provider

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func fakeTokenServer(t *testing.T, expiresIn int, wantClientID string) (*httptest.Server, *int32) {
	t.Helper()
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		if err := r.ParseForm(); err != nil {
			t.Fatalf("parse form: %v", err)
		}
		if got := r.PostForm.Get("grant_type"); got != "client_credentials" {
			t.Errorf("grant_type = %q, want client_credentials", got)
		}
		if wantClientID != "" && r.PostForm.Get("client_id") != wantClientID {
			t.Errorf("client_id = %q, want %q", r.PostForm.Get("client_id"), wantClientID)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"access_token": "tok-" + r.PostForm.Get("client_id"),
			"expires_in":   expiresIn,
			"token_type":   "Bearer",
		})
	}))
	t.Cleanup(srv.Close)
	return srv, &calls
}

func TestTokenFetchesAndCachesUntilExpiry(t *testing.T) {
	srv, calls := fakeTokenServer(t, 3600, "cid")
	ts := NewTokenSource(srv.URL, "cid", "secret", srv.Client())

	tok, err := ts.Token(context.Background())
	if err != nil {
		t.Fatalf("Token: %v", err)
	}
	if tok != "tok-cid" {
		t.Errorf("token = %q, want tok-cid", tok)
	}

	if _, err := ts.Token(context.Background()); err != nil {
		t.Fatalf("second Token: %v", err)
	}
	if got := atomic.LoadInt32(calls); got != 1 {
		t.Fatalf("token endpoint called %d times, want 1 (cached)", got)
	}
}

func TestTokenRefetchesAfterExpiry(t *testing.T) {
	// expires_in=1s, refreshed a tenth early per fetch()'s own margin - so
	// wait past the full second to be past even the margin.
	srv, calls := fakeTokenServer(t, 1, "")
	ts := NewTokenSource(srv.URL, "cid", "secret", srv.Client())

	if _, err := ts.Token(context.Background()); err != nil {
		t.Fatalf("Token: %v", err)
	}
	time.Sleep(1100 * time.Millisecond)
	if _, err := ts.Token(context.Background()); err != nil {
		t.Fatalf("Token after expiry: %v", err)
	}
	if got := atomic.LoadInt32(calls); got != 2 {
		t.Fatalf("token endpoint called %d times, want 2 (one refetch)", got)
	}
}

func TestInvalidateForcesARefetch(t *testing.T) {
	srv, calls := fakeTokenServer(t, 3600, "")
	ts := NewTokenSource(srv.URL, "cid", "secret", srv.Client())

	if _, err := ts.Token(context.Background()); err != nil {
		t.Fatalf("Token: %v", err)
	}
	ts.Invalidate()
	if _, err := ts.Token(context.Background()); err != nil {
		t.Fatalf("Token after invalidate: %v", err)
	}
	if got := atomic.LoadInt32(calls); got != 2 {
		t.Fatalf("token endpoint called %d times, want 2 (invalidated once)", got)
	}
}

func TestTokenFailsOnANonOKStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"error":"invalid_client"}`))
	}))
	defer srv.Close()

	ts := NewTokenSource(srv.URL, "cid", "bad-secret", srv.Client())
	if _, err := ts.Token(context.Background()); err == nil {
		t.Fatal("expected an error for a 401 token response")
	}
}

func TestTokenFailsWhenTheResponseCarriesNoAccessToken(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"expires_in": 3600})
	}))
	defer srv.Close()

	ts := NewTokenSource(srv.URL, "cid", "secret", srv.Client())
	if _, err := ts.Token(context.Background()); err == nil {
		t.Fatal("expected an error for a response with no access_token")
	}
}

func TestTokenSendsCredentialsFormEncoded(t *testing.T) {
	var gotSecret string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if ct := r.Header.Get("Content-Type"); ct != "application/x-www-form-urlencoded" {
			t.Errorf("content-type = %q, want form-urlencoded", ct)
		}
		_ = r.ParseForm()
		gotSecret = r.PostForm.Get("client_secret")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"access_token": "tok", "expires_in": 3600,
		})
	}))
	defer srv.Close()

	ts := NewTokenSource(srv.URL, "cid", "s3cr3t", srv.Client())
	if _, err := ts.Token(context.Background()); err != nil {
		t.Fatalf("Token: %v", err)
	}
	if gotSecret != "s3cr3t" {
		t.Errorf("client_secret sent as %q, want s3cr3t", gotSecret)
	}
}

// Sanity check on the test helper itself: a malformed token URL must fail
// cleanly rather than panic.
func TestTokenFailsOnAnUnreachableTokenURL(t *testing.T) {
	ts := NewTokenSource("http://127.0.0.1:1", "cid", "secret", http.DefaultClient)
	if _, err := ts.Token(context.Background()); err == nil {
		t.Fatal("expected an error against an unreachable token URL")
	}
}
