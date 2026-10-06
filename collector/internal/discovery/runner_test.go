package discovery

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
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

// slowSweep is one silent agent asked with 20 communities in turn: ~4 s of
// timeouts, long enough to see whether a cancel cuts it short.
func slowSweep(t *testing.T, status func(w http.ResponseWriter)) (*Runner, *atomic.Int32) {
	t.Helper()
	silent, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = silent.Close() })
	communities := make([]string, 20)
	for i := range communities {
		communities[i] = fmt.Sprintf("c%d", i)
	}
	var reports atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/claim"):
			_, _ = w.Write([]byte(`{"run":{"id":"r1","method":"sweep","scope":{"subnets":["127.0.0.1/32"]}}}`))
		case strings.HasSuffix(r.URL.Path, "/r1/status"):
			status(w)
		case strings.HasSuffix(r.URL.Path, "/r1/results"):
			reports.Add(1)
			_, _ = w.Write([]byte(`{}`))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	sw := New(slog.Default(), Communities(communities...),
		uint16(silent.LocalAddr().(*net.UDPAddr).Port))
	sw.Timeout, sw.Retries, sw.Concurrency = 200*time.Millisecond, 0, 1
	return &Runner{BaseURL: srv.URL, Token: func() string { return "t" },
		HTTP: srv.Client(), Log: slog.Default(), Sweeper: sw,
		StatusEvery: 50 * time.Millisecond}, &reports
}

func TestACancelledRunStopsItsSweepAndReportsNothing(t *testing.T) {
	r, reports := slowSweep(t, func(w http.ResponseWriter) {
		_, _ = w.Write([]byte(`{"status":"cancelled"}`))
	})
	start := time.Now()
	if err := r.once(context.Background()); err != nil {
		t.Fatal(err)
	}
	if took := time.Since(start); took > 2*time.Second {
		t.Errorf("sweep ran %v after its run was cancelled", took)
	}
	if n := reports.Load(); n != 0 {
		t.Errorf("reported %d times for a cancelled run", n)
	}
}

func TestAnAPIWithoutTheStatusRouteLeavesTheSweepAlone(t *testing.T) {
	// 404 from an API older than the route is not "stop": the sweep goes on
	// and reports, as every sweep did before.
	r, reports := slowSweep(t, func(w http.ResponseWriter) {
		w.WriteHeader(http.StatusNotFound)
	})
	start := time.Now()
	if err := r.once(context.Background()); err != nil {
		t.Fatal(err)
	}
	if took := time.Since(start); took < 3*time.Second {
		t.Errorf("sweep ended after %v; it should have run every community", took)
	}
	if n := reports.Load(); n != 1 {
		t.Errorf("reported %d times, want 1", n)
	}
}
