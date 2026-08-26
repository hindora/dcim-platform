package redfish

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// fakeBMC serves the subset of Redfish this adapter reads, shaped like the
// real thing: a session that must be created before anything else answers, and
// sensors that are deliberately awkward - an absent probe, a percentage fan,
// a failed supply.
type fakeBMC struct {
	sessions   int
	rootHits   int
	tokenValid bool
}

func (f *fakeBMC) handler() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("/redfish/v1/SessionService/Sessions", func(w http.ResponseWriter, r *http.Request) {
		f.sessions++
		f.tokenValid = true
		w.Header().Set("X-Auth-Token", "tok-123")
		w.Header().Set("Location", "/redfish/v1/SessionService/Sessions/1")
		w.WriteHeader(http.StatusCreated)
		_, _ = io.WriteString(w, `{"Id":"1"}`)
	})

	guard := func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("X-Auth-Token") != "tok-123" || !f.tokenValid {
				w.WriteHeader(http.StatusUnauthorized)
				return
			}
			next(w, r)
		}
	}

	mux.HandleFunc("/redfish/v1/Chassis", guard(func(w http.ResponseWriter, r *http.Request) {
		f.rootHits++
		_, _ = io.WriteString(w,
			`{"Members":[{"@odata.id":"/redfish/v1/Chassis/1"}]}`)
	}))
	mux.HandleFunc("/redfish/v1/Systems", guard(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w,
			`{"Members":[{"@odata.id":"/redfish/v1/Systems/1"}]}`)
	}))
	mux.HandleFunc("/redfish/v1/Chassis/1", guard(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `{
		  "Thermal":{"@odata.id":"/redfish/v1/Chassis/1/Thermal"},
		  "Power":{"@odata.id":"/redfish/v1/Chassis/1/Power"}}`)
	}))

	mux.HandleFunc("/redfish/v1/Chassis/1/Thermal", guard(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `{
		  "Temperatures":[
		    {"Name":"CPU1 Temp","ReadingCelsius":67.5,"Status":{"State":"Enabled","Health":"OK"}},
		    {"Name":"Inlet Temp","ReadingCelsius":23.4,"Status":{"State":"Enabled"}},
		    {"Name":"Exhaust Temp","ReadingCelsius":41.0,"Status":{"State":"Enabled"}},
		    {"Name":"CPU2 Temp","ReadingCelsius":null,"Status":{"State":"Absent"}}
		  ],
		  "Fans":[
		    {"Name":"Fan1","Reading":8400,"ReadingUnits":"RPM","Status":{"State":"Enabled"}},
		    {"Name":"Fan2","Reading":42,"ReadingUnits":"Percent","Status":{"State":"Enabled"}}
		  ]}`)
	}))

	mux.HandleFunc("/redfish/v1/Chassis/1/Power", guard(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `{
		  "PowerControl":[{"PowerConsumedWatts":812}],
		  "PowerSupplies":[
		    {"MemberId":"0","LineInputVoltage":240,"PowerOutputWatts":410,
		     "Status":{"State":"Enabled","Health":"OK"}},
		    {"MemberId":"1","LineInputVoltage":240,"PowerOutputWatts":0,
		     "Status":{"State":"Enabled","Health":"Critical"}}
		  ]}`)
	}))
	return mux
}

func newAdapter(t *testing.T) *Adapter {
	t.Helper()
	maps, err := mapping.LoadRedfish("../../../../contracts/mappings")
	if err != nil {
		t.Fatalf("load mappings: %v", err)
	}
	return New(maps, slog.New(slog.NewTextHandler(io.Discard, nil)), obs.NewMetrics())
}

func endpointFor(t *testing.T, srv *httptest.Server) *models.Endpoint {
	t.Helper()
	host := strings.TrimPrefix(srv.URL, "https://")
	parts := strings.Split(host, ":")
	port := 0
	_, _ = fmtSscan(parts[1], &port)
	return &models.Endpoint{
		ID: "ep-1", DeviceID: "dev-1", DeviceType: "server", Protocol: "redfish",
		Address: parts[0], Port: port,
		Addressing: map[string]any{"base": "/redfish/v1", "verify_tls": false},
		Credential: &models.Credential{
			Kind: "http_basic",
			Data: map[string]any{"username": "admin", "password": "password"},
		},
		Poll: models.PollProfile{IntervalS: 60, TimeoutMs: 5000},
	}
}

func fmtSscan(s string, out *int) (int, error) {
	v := 0
	for _, c := range s {
		if c < '0' || c > '9' {
			break
		}
		v = v*10 + int(c-'0')
	}
	*out = v
	return 1, nil
}

func collect(out *models.PollOutcome) map[string]models.Telemetry {
	m := map[string]models.Telemetry{}
	for _, s := range out.Samples {
		key := s.Metric
		if s.Instance != "" {
			key += "/" + s.Instance
		}
		m[key] = s
	}
	return m
}

func TestPollReadsThermalAndPower(t *testing.T) {
	bmc := &fakeBMC{}
	srv := httptest.NewTLSServer(bmc.handler())
	defer srv.Close()

	a := newAdapter(t)
	out, err := a.Poll(context.Background(), endpointFor(t, srv))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	got := collect(out)

	if s, ok := got["cpu_temperature/CPU1 Temp"]; !ok || s.DoubleValue != 67.5 {
		t.Fatalf("cpu_temperature missing or wrong: %+v", got)
	}
	if s, ok := got["inlet_temperature"]; !ok || s.DoubleValue != 23.4 {
		t.Fatalf("inlet_temperature missing or wrong: %+v", got["inlet_temperature"])
	}
	if s, ok := got["exhaust_temperature"]; !ok || s.DoubleValue != 41.0 {
		t.Fatal("exhaust_temperature missing")
	}
	if s, ok := got["power_draw"]; !ok || s.DoubleValue != 812 {
		t.Fatalf("power_draw missing or wrong: %+v", s)
	}
	if s, ok := got["psu_input_voltage/0"]; !ok || s.DoubleValue != 240 {
		t.Fatal("psu_input_voltage for member 0 missing")
	}
}

func TestAbsentSensorIsAMissNotAZero(t *testing.T) {
	// A zeroed absent probe reads as "CPU at 0 C" and raises a false alarm;
	// the gap has to stay visible as a gap.
	bmc := &fakeBMC{}
	srv := httptest.NewTLSServer(bmc.handler())
	defer srv.Close()

	out, err := newAdapter(t).Poll(context.Background(), endpointFor(t, srv))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	if _, present := collect(out)["cpu_temperature/CPU2 Temp"]; present {
		t.Fatal("an Absent sensor was emitted as a sample")
	}
	found := false
	for _, m := range out.Misses {
		if m.Metric == "cpu_temperature" && m.Reason == models.MissNoSuchObject {
			found = true
		}
	}
	if !found {
		t.Fatalf("absent sensor did not produce a miss: %+v", out.Misses)
	}
	if !out.Partial {
		t.Fatal("a poll with misses should be marked partial")
	}
}

func TestFanUnitsDecideTheMetric(t *testing.T) {
	// Recording a percentage as an rpm figure makes a healthy fan look stopped.
	bmc := &fakeBMC{}
	srv := httptest.NewTLSServer(bmc.handler())
	defer srv.Close()

	out, err := newAdapter(t).Poll(context.Background(), endpointFor(t, srv))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	got := collect(out)
	if s, ok := got["fan_speed/Fan1"]; !ok || s.DoubleValue != 8400 || s.Unit != "rpm" {
		t.Fatalf("RPM fan wrong: %+v", s)
	}
	if s, ok := got["fan_speed_pct/Fan2"]; !ok || s.DoubleValue != 42 || s.Unit != "pct" {
		t.Fatalf("percent fan wrong: %+v", s)
	}
}

func TestPsuStateNeedsBothEnabledAndHealthy(t *testing.T) {
	// A supply that is Enabled but Critical is failing. Treating Enabled alone
	// as healthy hides exactly the condition worth knowing before the other
	// supply goes too.
	bmc := &fakeBMC{}
	srv := httptest.NewTLSServer(bmc.handler())
	defer srv.Close()

	out, err := newAdapter(t).Poll(context.Background(), endpointFor(t, srv))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	got := collect(out)
	if s, ok := got["psu_state/0"]; !ok || !s.BoolValue {
		t.Fatal("healthy PSU should report true")
	}
	if s, ok := got["psu_state/1"]; !ok || s.BoolValue {
		t.Fatal("an Enabled-but-Critical PSU must report false")
	}
}

func TestDiscoveryHappensOncePerEndpoint(t *testing.T) {
	// Crawling every cycle is three requests versus thirty, and at a few
	// hundred BMCs that is the collector's dominant cost.
	bmc := &fakeBMC{}
	srv := httptest.NewTLSServer(bmc.handler())
	defer srv.Close()

	a := newAdapter(t)
	ep := endpointFor(t, srv)
	for i := 0; i < 3; i++ {
		if _, err := a.Poll(context.Background(), ep); err != nil {
			t.Fatalf("poll %d: %v", i, err)
		}
	}
	if bmc.rootHits != 1 {
		t.Fatalf("Chassis collection fetched %d times, want 1", bmc.rootHits)
	}
	if bmc.sessions != 1 {
		t.Fatalf("authenticated %d times, want 1", bmc.sessions)
	}
}

func TestARejectedTokenIsRenewedInsideTheSamePoll(t *testing.T) {
	// A Redfish token dies for ordinary reasons: the BMC was reset, the
	// service processor aged the session out, or its session table filled -
	// iDRAC and iLO both cap concurrent sessions in single digits. None of
	// that means the device is unreachable.
	//
	// Failing the poll and re-authenticating on the NEXT one turned a single
	// session invalidation into an alarm per device: 165 MAJOR "no response"
	// alarms raised and cleared inside 51 seconds when the simulator restarted
	// and every in-memory token died with it.
	bmc := &fakeBMC{}
	srv := httptest.NewTLSServer(bmc.handler())
	defer srv.Close()

	a := newAdapter(t)
	ep := endpointFor(t, srv)
	if _, err := a.Poll(context.Background(), ep); err != nil {
		t.Fatalf("first poll: %v", err)
	}

	// The BMC restarts and forgets the session.
	bmc.tokenValid = false
	if _, err := a.Poll(context.Background(), ep); err != nil {
		t.Fatalf("a rejected token should be renewed, not reported: %v", err)
	}
	if bmc.sessions != 2 {
		t.Fatalf("sessions created %d, want 2 (one renewal)", bmc.sessions)
	}

	// And the renewed session is kept, so the next poll costs no extra login.
	if _, err := a.Poll(context.Background(), ep); err != nil {
		t.Fatalf("poll after renewal: %v", err)
	}
	if bmc.sessions != 2 {
		t.Fatalf("sessions created %d after a third poll, want 2", bmc.sessions)
	}
}

func TestACredentialThatIsActuallyWrongStillFails(t *testing.T) {
	// The renewal is ONCE, not a loop. A password the BMC refuses has to
	// surface as an auth error - retrying it forever would be a login storm
	// against a service processor that is already saying no, and on real gear
	// that locks the account out.
	bmc := &rejectingBMC{}
	srv := httptest.NewTLSServer(bmc.handler())
	defer srv.Close()

	a := newAdapter(t)
	ep := endpointFor(t, srv)
	_, err := a.Poll(context.Background(), ep)
	if err == nil {
		t.Fatal("a rejected credential should fail the poll")
	}
	if models.ClassifyError(err) != models.ErrClassAuth {
		t.Fatalf("error class %q, want auth", models.ClassifyError(err))
	}
	if bmc.logins > 2 {
		t.Fatalf("%d login attempts in one poll - the renewal is looping",
			bmc.logins)
	}
}

// rejectingBMC answers every session request with 401, the way a BMC with a
// wrong password does.
type rejectingBMC struct{ logins int }

func (f *rejectingBMC) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/redfish/v1/SessionService/Sessions",
		func(w http.ResponseWriter, r *http.Request) {
			f.logins++
			w.WriteHeader(http.StatusUnauthorized)
		})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	})
	return mux
}

func TestMissingCredentialIsAnAuthError(t *testing.T) {
	a := newAdapter(t)
	ep := &models.Endpoint{ID: "ep-x", Protocol: "redfish", Address: "127.0.0.1", Port: 1}
	_, err := a.Poll(context.Background(), ep)
	if err == nil || models.ClassifyError(err) != models.ErrClassAuth {
		t.Fatalf("want an auth error, got %v", err)
	}
}

func TestGlobMatch(t *testing.T) {
	cases := []struct {
		pattern, name string
		want          bool
	}{
		{"CPU*", "CPU1 Temp", true},
		{"CPU*", "Inlet Temp", false},
		{"*Inlet*", "System Inlet Temp", true},
		{"*Exhaust*", "Exhaust Temp", true},
		{"*", "anything", true},
		{"Inlet", "inlet", true},
	}
	for _, c := range cases {
		if got := mapping.GlobMatchForTest(c.pattern, c.name); got != c.want {
			t.Fatalf("glob(%q,%q)=%v want %v", c.pattern, c.name, got, c.want)
		}
	}
}

var _ = json.Marshal

// The simulator serves Redfish as plain HTTP on 8443 despite the port, so the
// scheme has to be per-endpoint data rather than a constant.
func TestPlainHTTPSchemeIsHonoured(t *testing.T) {
	srv := httptest.NewServer((&fakeBMC{}).handler())
	defer srv.Close()

	ep := endpointFor(t, &httptest.Server{
		URL: strings.Replace(srv.URL, "http://", "https://", 1)})
	ep.Addressing["scheme"] = "http"

	out, err := newAdapter(t).Poll(context.Background(), ep)
	if err != nil {
		t.Fatalf("poll over http: %v", err)
	}
	if len(out.Samples) == 0 {
		t.Fatal("no samples over http")
	}
}

// A TLS endpoint must NOT silently fall back to plaintext when the handshake
// fails: the session request carries the BMC password, and an attacker who can
// answer the port could force the downgrade.
func TestNoSilentDowngradeToHTTP(t *testing.T) {
	srv := httptest.NewServer((&fakeBMC{}).handler()) // plaintext server
	defer srv.Close()

	// Addressed as https, which is the default.
	ep := endpointFor(t, &httptest.Server{
		URL: strings.Replace(srv.URL, "http://", "https://", 1)})

	if _, err := newAdapter(t).Poll(context.Background(), ep); err == nil {
		t.Fatal("an https endpoint answered by a plaintext server must fail")
	}
}

func TestUnsupportedSchemeIsConfig(t *testing.T) {
	srv := httptest.NewTLSServer((&fakeBMC{}).handler())
	defer srv.Close()
	ep := endpointFor(t, srv)
	ep.Addressing["scheme"] = "ftp"

	_, err := newAdapter(t).Poll(context.Background(), ep)
	if err == nil {
		t.Fatal("accepted an unsupported scheme")
	}
	if class := models.ClassifyError(err); class != models.ErrClassConfig {
		t.Fatalf("error class %q, want config", class)
	}
}
