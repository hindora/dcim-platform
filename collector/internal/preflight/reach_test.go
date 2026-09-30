package preflight

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestClassifyReach(t *testing.T) {
	cases := []struct {
		ok, total int
		want      string
	}{
		{0, 0, StatusSkipped},
		{3, 3, StatusOK},
		{0, 3, StatusFail},
		{1, 3, StatusWarn},
	}
	for _, c := range cases {
		if got := classifyReach(c.ok, c.total); got != c.want {
			t.Errorf("classifyReach(%d, %d) = %q, want %q", c.ok, c.total, got, c.want)
		}
	}
}

// openTCP returns a listening address and a closed one - a real refusal,
// not a mock of one.
func openTCP(t *testing.T) (open, closed Target) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			c.Close()
		}
	}()
	dead, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	deadPort := dead.Addr().(*net.TCPAddr).Port
	dead.Close()
	return Target{"127.0.0.1", ln.Addr().(*net.TCPAddr).Port},
		Target{"127.0.0.1", deadPort}
}

func TestProbeTCPOpenAndRefused(t *testing.T) {
	open, closed := openTCP(t)
	if err := probeTCP(context.Background(), open, time.Second); err != nil {
		t.Fatalf("open port: %v", err)
	}
	if err := probeTCP(context.Background(), closed, time.Second); err == nil {
		t.Fatal("closed port reported reachable")
	}
}

// fakeBACnetDevice answers any Who-Is with a real I-Am (device 40007) from
// its own socket - the address Identify is waiting on.
func fakeBACnetDevice(t *testing.T) Target {
	t.Helper()
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	iAm := []byte{
		0x81, 0x0A, 0x00, 0x15, // BVLL original-unicast, 21 bytes
		0x01, 0x00, // NPDU
		0x10, 0x00, // unconfirmed-request, I-Am
		0xC4, 0x02, 0x00, 0x9C, 0x47, // device object 40007
		0x22, 0x05, 0xC4, // max APDU 1476
		0x91, 0x03, // no segmentation
		0x22, 0x03, 0xE7, // vendor 999
	}
	go func() {
		buf := make([]byte, 1500)
		for {
			_, from, err := conn.ReadFromUDP(buf)
			if err != nil {
				return
			}
			_, _ = conn.WriteToUDP(iAm, from)
		}
	}()
	return Target{"127.0.0.1", conn.LocalAddr().(*net.UDPAddr).Port}
}

func silentUDP(t *testing.T) Target {
	t.Helper()
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	go func() {
		buf := make([]byte, 1500)
		for {
			if _, _, err := conn.ReadFromUDP(buf); err != nil {
				return
			}
		}
	}()
	return Target{"127.0.0.1", conn.LocalAddr().(*net.UDPAddr).Port}
}

func coreServing(t *testing.T, body any, status int) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/collector/preflight-targets" {
			http.NotFound(w, r)
			return
		}
		if r.URL.Query().Get("collector_id") != "col-1" {
			t.Errorf("collector_id = %q", r.URL.Query().Get("collector_id"))
		}
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(body)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestReachabilityEndToEndAgainstRealSockets(t *testing.T) {
	open, closed := openTCP(t)
	bac := fakeBACnetDevice(t)
	quiet := silentUDP(t)

	srv := coreServing(t, map[string]any{
		"source": "pool",
		"protocols": []map[string]any{
			{"protocol": "bacnet", "total": 2, "targets": []Target{bac, quiet}},
			{"protocol": "modbus", "total": 1, "targets": []Target{open}},
			{"protocol": "redfish", "total": 1, "targets": []Target{closed}},
			{"protocol": "snmp", "total": 40, "targets": []Target{{"10.51.1.1", 161}}},
		},
	}, http.StatusOK)

	cfg := testConfig()
	cfg.Collector.ID = "col-1"
	cfg.DCIM.BaseURL = srv.URL
	cfg.Preflight.Timeout = 500 * time.Millisecond

	got := map[string]Check{}
	for _, c := range checkReachability(context.Background(), cfg, srv.Client()) {
		got[c.Check] = c
	}
	if c := got["reach_bacnet"]; c.Status != StatusWarn || *c.Value != 1 {
		t.Errorf("bacnet: %+v, want warn with 1 answered (one I-Am, one silent)", c)
	}
	if !strings.Contains(got["reach_bacnet"].Detail, "silent: 127.0.0.1:"+strconv.Itoa(quiet.Port)) {
		t.Errorf("bacnet detail does not name the silent device: %s", got["reach_bacnet"].Detail)
	}
	if c := got["reach_modbus"]; c.Status != StatusOK {
		t.Errorf("modbus: %+v, want ok", c)
	}
	if c := got["reach_redfish"]; c.Status != StatusFail ||
		!strings.Contains(c.Detail, "firewall") {
		t.Errorf("redfish: %+v, want fail naming a firewall", c)
	}
	if c := got["reach_snmp"]; c.Status != StatusSkipped {
		t.Errorf("snmp: %+v, want skipped - no credential-free probe", c)
	}
}

func TestReachabilitySkipsWithNothingToProbe(t *testing.T) {
	srv := coreServing(t, map[string]any{"source": "none", "protocols": []any{}}, http.StatusOK)
	cfg := testConfig()
	cfg.Collector.ID = "col-1"
	cfg.DCIM.BaseURL = srv.URL
	checks := checkReachability(context.Background(), cfg, srv.Client())
	if len(checks) != 1 || checks[0].Status != StatusSkipped ||
		!strings.Contains(checks[0].Detail, "nothing to probe") {
		t.Fatalf("got %+v", checks)
	}
}

func TestReachabilitySkipsWhenThePlatformRefuses(t *testing.T) {
	srv := coreServing(t, map[string]string{"detail": "no"}, http.StatusForbidden)
	cfg := testConfig()
	cfg.Collector.ID = "col-1"
	cfg.DCIM.BaseURL = srv.URL
	checks := checkReachability(context.Background(), cfg, srv.Client())
	if len(checks) != 1 || checks[0].Status != StatusSkipped ||
		!strings.Contains(checks[0].Detail, "403") {
		t.Fatalf("got %+v", checks)
	}
}
