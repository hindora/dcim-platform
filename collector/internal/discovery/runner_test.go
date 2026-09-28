package discovery

import (
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestAClaimSaysWhoIsAskingAndThatItCanExclude(t *testing.T) {
	// A run assigned to this collector's range must reach this collector, and a
	// run carrying exclusions only one that honours them. Both are decided by
	// what the claim declares.
	var got map[string]string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = map[string]string{
			"path":         r.URL.Path,
			"collector_id": r.URL.Query().Get("collector_id"),
			"features":     r.URL.Query().Get("features"),
		}
		_, _ = w.Write([]byte(`{"run":null}`))
	}))
	defer srv.Close()

	r := &Runner{BaseURL: srv.URL, CollectorID: "dc2-col",
		Token: func() string { return "t" }, HTTP: srv.Client(),
		Log: slog.Default()}
	if _, err := r.claim(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got["path"] != "/api/v1/collector/discovery/claim" ||
		got["collector_id"] != "dc2-col" || got["features"] != "exclude" {
		t.Errorf("claim declared %v", got)
	}
}
