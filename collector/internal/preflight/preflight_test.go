package preflight

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/config"
)

func testConfig() *config.Config {
	cfg := config.Default()
	cfg.Preflight.Timeout = 2 * time.Second
	return cfg
}

func TestCheckSpoolDiskIsSkippedOnTheRedisTransport(t *testing.T) {
	cfg := testConfig()
	cfg.Transport.Mode = "redis"
	got := checkSpoolDisk(cfg)
	if got.Status != StatusSkipped {
		t.Errorf("status = %q, want skipped for transport.mode=redis", got.Status)
	}
}

func TestCheckSpoolDiskRunsSuccessfullyAgainstARealDirectory(t *testing.T) {
	// Free space on whatever machine runs this test is not something a
	// test can assume a value for - see classifyDiskSpace's own tests for
	// the actual threshold logic, fixed-byte-count and deterministic.
	cfg := testConfig()
	cfg.Transport.Mode = "gateway"
	cfg.Transport.Gateway.SpoolDir = t.TempDir()
	got := checkSpoolDisk(cfg)
	if got.Status != StatusOK && got.Status != StatusWarn {
		t.Errorf("status = %q, detail %q, want ok or warn - a real, writable "+
			"temp dir must never come back fail or skipped", got.Status, got.Detail)
	}
	if got.Value == nil {
		t.Error("expected a Value (free GiB) to be reported")
	}
}

func TestClassifyDiskSpace(t *testing.T) {
	cases := []struct {
		free uint64
		want string
	}{
		{free: 1 << 30, want: StatusFail},           // 1 GiB
		{free: diskFailBytes, want: StatusWarn},     // exactly at the fail floor: not under it
		{free: diskFailBytes - 1, want: StatusFail}, // just under it
		{free: 10 << 30, want: StatusWarn},          // 10 GiB
		{free: diskWarnBytes, want: StatusOK},       // exactly at the warn floor: not under it
		{free: diskWarnBytes - 1, want: StatusWarn}, // just under it
		{free: 100 << 30, want: StatusOK},           // 100 GiB
	}
	for _, c := range cases {
		status, _ := classifyDiskSpace(c.free)
		if status != c.want {
			t.Errorf("classifyDiskSpace(%d bytes) = %q, want %q", c.free, status, c.want)
		}
	}
}

func TestCheckSpoolDiskCreatesTheDirectoryIfMissing(t *testing.T) {
	cfg := testConfig()
	cfg.Transport.Mode = "gateway"
	cfg.Transport.Gateway.SpoolDir = t.TempDir() + "/does/not/exist/yet"
	got := checkSpoolDisk(cfg)
	if got.Status == StatusFail {
		t.Errorf("expected the spool dir to be created, got a fail: %s", got.Detail)
	}
}

func TestCheckTrapPortIsSkippedWhenTrapsAreDisabled(t *testing.T) {
	cfg := testConfig()
	cfg.Protocols.SNMPTrap.Enabled = false
	got := checkTrapPort(cfg)
	if got.Status != StatusSkipped {
		t.Errorf("status = %q, want skipped", got.Status)
	}
}

func TestCheckTrapPortPassesOnAFreePort(t *testing.T) {
	cfg := testConfig()
	cfg.Protocols.SNMPTrap.Enabled = true
	cfg.Protocols.SNMPTrap.Listen = "127.0.0.1:0" // :0 = kernel picks a free one
	got := checkTrapPort(cfg)
	if got.Status != StatusOK {
		t.Errorf("status = %q, detail %q, want ok", got.Status, got.Detail)
	}
}

func TestCheckTrapPortFailsWhenSomethingElseHoldsIt(t *testing.T) {
	held, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer held.Close()

	cfg := testConfig()
	cfg.Protocols.SNMPTrap.Enabled = true
	cfg.Protocols.SNMPTrap.Listen = held.LocalAddr().String()
	got := checkTrapPort(cfg)
	if got.Status != StatusFail {
		t.Errorf("status = %q, want fail against an already-bound port", got.Status)
	}
}

func TestCheckCoreReachablePassesAgainstARealServer(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/health" {
			t.Errorf("unexpected path %s", r.URL.Path)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	cfg := testConfig()
	cfg.DCIM.BaseURL = srv.URL
	got := checkCoreReachable(context.Background(), cfg, srv.Client())
	if got.Status != StatusOK {
		t.Errorf("status = %q, detail %q, want ok", got.Status, got.Detail)
	}
}

func TestCheckCoreReachableFailsAgainstAServerError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	cfg := testConfig()
	cfg.DCIM.BaseURL = srv.URL
	got := checkCoreReachable(context.Background(), cfg, srv.Client())
	if got.Status != StatusFail {
		t.Errorf("status = %q, want fail against a 500", got.Status)
	}
}

func TestCheckCoreReachableFailsAgainstAnUnreachableHost(t *testing.T) {
	cfg := testConfig()
	cfg.DCIM.BaseURL = "http://127.0.0.1:1" // port 1: nothing listens, connection refused
	cfg.Preflight.Timeout = 500 * time.Millisecond
	got := checkCoreReachable(context.Background(), cfg, &http.Client{Timeout: 500 * time.Millisecond})
	if got.Status != StatusFail {
		t.Errorf("status = %q, want fail against an unreachable host", got.Status)
	}
}

func TestCheckNTPWarnsOnAModerateOffset(t *testing.T) {
	addr := fakeSNTPServer(t, 30*time.Second) // between warn (15s) and fail (75s)
	cfg := testConfig()
	cfg.Preflight.NTPServer = addr
	got := checkNTP(context.Background(), cfg)
	if got.Status != StatusWarn {
		t.Errorf("status = %q, detail %q, want warn", got.Status, got.Detail)
	}
}

func TestCheckNTPFailsOnALargeOffset(t *testing.T) {
	addr := fakeSNTPServer(t, 120*time.Second)
	cfg := testConfig()
	cfg.Preflight.NTPServer = addr
	got := checkNTP(context.Background(), cfg)
	if got.Status != StatusFail {
		t.Errorf("status = %q, want fail", got.Status)
	}
}

func TestCheckNTPPassesWhenInSync(t *testing.T) {
	addr := fakeSNTPServer(t, 0)
	cfg := testConfig()
	cfg.Preflight.NTPServer = addr
	got := checkNTP(context.Background(), cfg)
	if got.Status != StatusOK {
		t.Errorf("status = %q, detail %q, want ok", got.Status, got.Detail)
	}
}

func TestCheckNTPWarnsRatherThanFailsWhenTheServerIsUnreachable(t *testing.T) {
	// A closed UDP socket - nothing there to reply, so the timeout path in
	// ntpOffset fires. Unlike a genuine large clock offset, "could not
	// reach the NTP server at all" is a warn, not a fail: the collector's
	// own clock may be perfectly fine, only unreachable NTP is the actual
	// finding, and that alone must not block an otherwise-clean preflight.
	conn, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	addr := conn.LocalAddr().String()
	conn.Close() // closed immediately - nothing will ever answer on this address

	cfg := testConfig()
	cfg.Preflight.NTPServer = addr
	cfg.Preflight.Timeout = 300 * time.Millisecond
	got := checkNTP(context.Background(), cfg)
	if got.Status != StatusWarn {
		t.Errorf("status = %q, detail %q, want warn for an unreachable NTP server",
			got.Status, got.Detail)
	}
}

func TestRunReturnsTheFourHostChecksFirstThenReachability(t *testing.T) {
	cfg := testConfig()
	cfg.Preflight.NTPServer = fakeSNTPServer(t, 0)
	cfg.Transport.Mode = "redis"           // spool_disk skipped
	cfg.Protocols.SNMPTrap.Enabled = false // trap_port skipped

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	cfg.DCIM.BaseURL = srv.URL

	checks := Run(context.Background(), cfg, srv.Client())
	// Four host checks, then reachability - a single skipped check here,
	// since this fake core answers the targets path with an empty body.
	if len(checks) != 5 {
		t.Fatalf("got %d checks, want 5", len(checks))
	}
	if checks[4].Check != "reachability" || checks[4].Status != StatusSkipped {
		t.Errorf("checks[4] = %+v, want a skipped reachability check", checks[4])
	}
	names := []string{checks[0].Check, checks[1].Check, checks[2].Check, checks[3].Check}
	want := []string{"ntp_offset", "spool_disk", "trap_port", "core_tls"}
	for i, n := range names {
		if n != want[i] {
			t.Errorf("checks[%d] = %q, want %q", i, n, want[i])
		}
	}
}

func TestPostSendsTheChecksAsJSON(t *testing.T) {
	var gotBody map[string]any
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		_ = json.NewDecoder(r.Body).Decode(&gotBody)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	err := Post(context.Background(), srv.Client(), srv.URL, "test-token",
		[]Check{{Check: "core_tls", Status: StatusOK}})
	if err != nil {
		t.Fatalf("Post: %v", err)
	}
	if gotAuth != "Bearer test-token" {
		t.Errorf("Authorization = %q", gotAuth)
	}
	checks, _ := gotBody["checks"].([]any)
	if len(checks) != 1 {
		t.Fatalf("posted checks = %v", gotBody["checks"])
	}
}

func TestPostFailsLoudlyOnAnUnexpectedStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()
	err := Post(context.Background(), srv.Client(), srv.URL, "bad-token", nil)
	if err == nil {
		t.Fatal("expected an error for a 401 response")
	}
}
