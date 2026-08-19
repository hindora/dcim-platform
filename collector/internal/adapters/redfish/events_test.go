package redfish

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/assign"
	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

func testLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// ------------------------------------------------------------- helpers

type captureSink struct {
	mu     sync.Mutex
	events []models.Event
}

func (c *captureSink) Telemetry(context.Context, []models.Telemetry) error { return nil }
func (c *captureSink) EndpointState(context.Context, models.EndpointState) error {
	return nil
}

func (c *captureSink) Events(_ context.Context, evs []models.Event) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.events = append(c.events, evs...)
	return nil
}

func (c *captureSink) all() []models.Event {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]models.Event, len(c.events))
	copy(out, c.events)
	return out
}

func loadEventMap(t *testing.T) *mapping.RedfishEventMap {
	t.Helper()
	m, err := mapping.LoadRedfishEvents("../../../../contracts/mappings")
	if err != nil {
		t.Fatalf("load event mappings: %v", err)
	}
	return m
}

func newReceiver(t *testing.T, sink models.Sink, resolver *assign.Resolver) *EventReceiver {
	t.Helper()
	a := newAdapter(t)
	return NewEventReceiver(a, loadEventMap(t), resolver, sink, testLog(),
		obs.NewMetrics(), "127.0.0.1:0", "http://10.0.0.9:9143"+EventPath, 1, 1000)
}

// deliver drives one POST through the handler without binding a port.
func deliver(t *testing.T, r *EventReceiver, body string) {
	t.Helper()
	queue := make(chan inboundEvent, 8)
	req := httptest.NewRequest(http.MethodPost, EventPath, strings.NewReader(body))
	req.RemoteAddr = "10.51.11.25:44112"
	w := httptest.NewRecorder()
	r.handleHTTP(w, req, queue)
	if w.Code != http.StatusNoContent && w.Code != http.StatusBadRequest {
		t.Fatalf("handler returned %d", w.Code)
	}
	close(queue)
	for in := range queue {
		r.publish(context.Background(), in)
	}
}

// ------------------------------------------------------ classification

// The simulator - like a great deal of real firmware - sends one generic OEM
// MessageId for every condition. If the text fallback breaks, every BMC event
// collapses onto a single alarm key and the whole event path looks fine while
// being useless.
func TestGenericMessageIDFallsBackToText(t *testing.T) {
	m := loadEventMap(t)
	cases := []struct {
		message  string
		severity string
		wantType string
		wantSev  string
	}{
		{"CPU temperature critical: 91.4 C", "Critical", "cpu_temp_critical", "CRITICAL"},
		{"CPU temperature high: 86.0 C", "Warning", "cpu_temp_high", "WARNING"},
		{"Inlet temperature high: 41.0 C", "Warning", "inlet_temp_high", "WARNING"},
		{"Memory utilization high: 93%", "Warning", "memory_high_usage", "WARNING"},
		{"Disk utilization high: 95%", "Warning", "disk_high_usage", "WARNING"},
	}
	for _, c := range cases {
		got, known := m.Classify("Simulator.1.0.Alert", c.message, c.severity)
		if !known {
			t.Errorf("%q: not classified", c.message)
			continue
		}
		if got.EventType != c.wantType || got.Severity != c.wantSev {
			t.Errorf("%q: got %s/%s, want %s/%s", c.message,
				got.EventType, got.Severity, c.wantType, c.wantSev)
		}
	}
}

// A clear must land on the SAME event_type as its assert. If it does not, the
// clear opens a second alarm and the original never resolves - the failure
// mode is a permanently red dashboard, which is worse than no events at all.
func TestClearMatchesItsAssert(t *testing.T) {
	m := loadEventMap(t)
	assertion, _ := m.Classify("Simulator.1.0.Alert",
		"CPU temperature critical: 91.4 C", "Critical")
	clear, known := m.Classify("Simulator.1.0.StatusChange",
		"CPU temperature critical cleared", "OK")
	if !known {
		t.Fatal("clear was not classified")
	}
	if clear.EventType != assertion.EventType {
		t.Fatalf("clear type %q != assert type %q", clear.EventType, assertion.EventType)
	}
	if !clear.IsClear {
		t.Error("clear not flagged as a clear")
	}
	if clear.Severity != "CLEAR" {
		t.Errorf("clear severity %q, want CLEAR", clear.Severity)
	}
	if assertion.IsClear {
		t.Error("assert wrongly flagged as a clear")
	}
}

// Warning and Critical bands are separate keys on purpose: the BMC tracks
// conditions by label, so crossing 90 C clears "high" and asserts "critical".
func TestWarningAndCriticalAreDistinctKeys(t *testing.T) {
	m := loadEventMap(t)
	warn, _ := m.Classify("x", "CPU temperature high: 86.0 C", "Warning")
	crit, _ := m.Classify("x", "CPU temperature critical: 91.0 C", "Critical")
	if warn.EventType == crit.EventType {
		t.Fatalf("both bands mapped to %q; a warning alarm would never clear",
			warn.EventType)
	}
}

// A standard registry id must win over the text, because it is the only
// identifier that is stable across firmware revisions.
func TestStandardMessageIDWins(t *testing.T) {
	m := loadEventMap(t)
	got, known := m.Classify("Alert.1.0.TemperatureAbove",
		"Some vendor phrasing nobody has a pattern for", "Warning")
	if !known || got.EventType != "temperature_alert" {
		t.Fatalf("got %q known=%v, want temperature_alert", got.EventType, known)
	}
}

func TestUnknownEventIsEmittedNotDropped(t *testing.T) {
	m := loadEventMap(t)
	got, known := m.Classify("Oem.1.0.Whatever", "something entirely new", "Warning")
	if known {
		t.Fatal("expected unknown")
	}
	if got.EventType != "unknown_event" {
		t.Fatalf("got %q, want unknown_event", got.EventType)
	}
}

func TestFanInstanceIsExtracted(t *testing.T) {
	m := loadEventMap(t)
	c, _ := m.Classify("x", "Fan failed: FAN3 stopped (0 RPM)", "Critical")
	if got := mapping.InstanceFrom(c.InstanceFrom, "Fan failed: FAN3 stopped (0 RPM)"); got != "FAN3" {
		t.Fatalf("instance %q, want FAN3", got)
	}
}

// ----------------------------------------------------------- receiver

func TestEventIsAttributedByContext(t *testing.T) {
	resolver := assign.NewResolver()
	resolver.Replace([]*models.Endpoint{{
		ID: "ep-bmc-1", DeviceID: "dev-1", Protocol: "redfish",
		Address: "10.51.11.99", // deliberately NOT the delivering address
	}})
	sink := &captureSink{}
	r := newReceiver(t, sink, resolver)

	deliver(t, r, `{"Context":"ep-bmc-1","Events":[
	  {"EventType":"Alert","Severity":"Critical","MessageId":"Simulator.1.0.Alert",
	   "Message":"Fan failed: FAN2 stopped (0 RPM)"}]}`)

	evs := sink.all()
	if len(evs) != 1 {
		t.Fatalf("got %d events, want 1", len(evs))
	}
	e := evs[0]
	if e.EndpointID != "ep-bmc-1" || e.DeviceID != "dev-1" {
		t.Fatalf("attributed to %q/%q", e.EndpointID, e.DeviceID)
	}
	if e.EventType != "fan_failure" || e.Instance != "FAN2" {
		t.Fatalf("got %s/%s, want fan_failure/FAN2", e.EventType, e.Instance)
	}
	if e.Severity != models.SeverityCritical {
		t.Fatalf("severity %v, want CRITICAL", e.Severity)
	}
	if e.SourceIP != "10.51.11.25" {
		t.Fatalf("source ip %q", e.SourceIP)
	}
}

// An event that cannot be attributed is still published. Dropping it is how a
// disagreement between inventory and the device plane becomes invisible.
func TestUnresolvedEventIsStillPublished(t *testing.T) {
	sink := &captureSink{}
	r := newReceiver(t, sink, assign.NewResolver())

	deliver(t, r, `{"Context":"nobody","Events":[
	  {"EventType":"Alert","Severity":"Warning","MessageId":"Simulator.1.0.Alert",
	   "Message":"Memory utilization high: 93%"}]}`)

	evs := sink.all()
	if len(evs) != 1 {
		t.Fatalf("got %d events, want 1", len(evs))
	}
	if evs[0].EndpointID != "" {
		t.Fatalf("unexpectedly attributed to %q", evs[0].EndpointID)
	}
	if evs[0].SourceIP == "" {
		t.Fatal("source ip must always be populated")
	}
}

// A malformed body must not hang or panic the handler; the BMC gets an answer
// either way.
func TestMalformedBodyIsRejectedNotFatal(t *testing.T) {
	sink := &captureSink{}
	r := newReceiver(t, sink, assign.NewResolver())
	queue := make(chan inboundEvent, 4)
	req := httptest.NewRequest(http.MethodPost, EventPath, strings.NewReader("{not json"))
	req.RemoteAddr = "10.51.11.25:1"
	w := httptest.NewRecorder()
	r.handleHTTP(w, req, queue)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("code %d, want 400", w.Code)
	}
	if len(queue) != 0 {
		t.Fatal("malformed body was queued")
	}
}

func TestRateLimitPerSource(t *testing.T) {
	sink := &captureSink{}
	r := NewEventReceiver(newAdapter(t), loadEventMap(t), assign.NewResolver(), sink,
		testLog(), obs.NewMetrics(), "127.0.0.1:0", "http://x"+EventPath, 1, 3)
	for i := 0; i < 3; i++ {
		if !r.allow("10.0.0.1") {
			t.Fatalf("event %d rejected under the limit", i)
		}
	}
	if r.allow("10.0.0.1") {
		t.Fatal("limit not enforced")
	}
	if !r.allow("10.0.0.2") {
		t.Fatal("one noisy source starved another")
	}
}

func TestObservedAtFallsBackToArrival(t *testing.T) {
	arrival := time.Date(2026, 8, 19, 10, 0, 0, 0, time.UTC)
	if got := observedAt("", arrival); got != arrival.UnixMicro() {
		t.Fatalf("got %d, want %d", got, arrival.UnixMicro())
	}
	if got := observedAt("not a timestamp", arrival); got != arrival.UnixMicro() {
		t.Fatalf("unparseable timestamp must fall back, got %d", got)
	}
	ts := "2026-08-19T09:00:00Z"
	want, _ := time.Parse(time.RFC3339, ts)
	if got := observedAt(ts, arrival); got != want.UnixMicro() {
		t.Fatalf("got %d, want %d", got, want.UnixMicro())
	}
}

// ------------------------------------------------------ reconciliation

// subsBMC is a BMC whose subscription collection can be inspected.
type subsBMC struct {
	mu      sync.Mutex
	subs    map[string]string // uri -> destination
	seq     int
	deleted []string
	posted  []map[string]any
}

func newSubsBMC(existing map[string]string) *subsBMC {
	b := &subsBMC{subs: map[string]string{}}
	for uri, dest := range existing {
		b.subs[uri] = dest
	}
	return b
}

func (b *subsBMC) server(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/redfish/v1/SessionService/Sessions",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("X-Auth-Token", "tok")
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{}`))
		})
	mux.HandleFunc("/redfish/v1/Chassis", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"Members":[{"@odata.id":"/redfish/v1/Chassis/1"}]}`))
	})
	mux.HandleFunc("/redfish/v1/Chassis/1", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"Thermal":{"@odata.id":"/redfish/v1/Chassis/1/Thermal"}}`))
	})
	mux.HandleFunc("/redfish/v1/Systems", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"Members":[]}`))
	})
	mux.HandleFunc("/redfish/v1/EventService/Subscriptions",
		func(w http.ResponseWriter, req *http.Request) {
			b.mu.Lock()
			defer b.mu.Unlock()
			switch req.Method {
			case http.MethodGet:
				members := make([]map[string]string, 0, len(b.subs))
				for uri := range b.subs {
					members = append(members, map[string]string{"@odata.id": uri})
				}
				_ = json.NewEncoder(w).Encode(map[string]any{"Members": members})
			case http.MethodPost:
				var body map[string]any
				_ = json.NewDecoder(req.Body).Decode(&body)
				b.posted = append(b.posted, body)
				b.seq++
				uri := fmt.Sprintf("/redfish/v1/EventService/Subscriptions/%d", b.seq)
				dest, _ := body["Destination"].(string)
				b.subs[uri] = dest
				w.Header().Set("Location", uri)
				w.WriteHeader(http.StatusCreated)
			default:
				w.WriteHeader(http.StatusMethodNotAllowed)
			}
		})
	mux.HandleFunc("/redfish/v1/EventService/Subscriptions/",
		func(w http.ResponseWriter, req *http.Request) {
			b.mu.Lock()
			defer b.mu.Unlock()
			switch req.Method {
			case http.MethodGet:
				dest, ok := b.subs[req.URL.Path]
				if !ok {
					w.WriteHeader(http.StatusNotFound)
					return
				}
				_ = json.NewEncoder(w).Encode(map[string]any{
					"@odata.id": req.URL.Path, "Destination": dest,
				})
			case http.MethodDelete:
				delete(b.subs, req.URL.Path)
				b.deleted = append(b.deleted, req.URL.Path)
				w.WriteHeader(http.StatusNoContent)
			}
		})
	srv := httptest.NewTLSServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

func (b *subsBMC) destinations() []string {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([]string, 0, len(b.subs))
	for _, d := range b.subs {
		out = append(out, d)
	}
	return out
}

func reconcileAgainst(t *testing.T, b *subsBMC, want string) (*EventReceiver, *models.Endpoint, int, int, error) {
	t.Helper()
	srv := b.server(t)
	ep := endpointFor(t, srv)
	r := NewEventReceiver(newAdapter(t), loadEventMap(t), assign.NewResolver(),
		&captureSink{}, testLog(), obs.NewMetrics(), "127.0.0.1:0", want, 1, 100)
	created, deleted, err := r.Reconcile(context.Background(), ep)
	return r, ep, created, deleted, err
}

func TestReconcileCreatesWhenMissing(t *testing.T) {
	b := newSubsBMC(nil)
	want := "http://10.0.0.9:9143" + EventPath
	_, ep, created, deleted, err := reconcileAgainst(t, b, want)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if created != 1 || deleted != 0 {
		t.Fatalf("created=%d deleted=%d, want 1/0", created, deleted)
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(b.posted) != 1 {
		t.Fatalf("posted %d subscriptions", len(b.posted))
	}
	// Context must carry the endpoint id: it is what attributes a delivered
	// event when the BMC posts from an address we do not poll.
	if b.posted[0]["Context"] != ep.ID {
		t.Fatalf("Context %v, want %q", b.posted[0]["Context"], ep.ID)
	}
}

// The whole point of reconciliation: orphans from a previous collector
// address fill the per-BMC subscription cap, after which new subscriptions
// fail and events simply stop with no other symptom.
func TestReconcileDeletesStaleOrphans(t *testing.T) {
	want := "http://10.0.0.9:9143" + EventPath
	b := newSubsBMC(map[string]string{
		"/redfish/v1/EventService/Subscriptions/old1": "http://10.0.0.5:9143" + EventPath,
		"/redfish/v1/EventService/Subscriptions/old2": "http://10.0.0.9:9999" + EventPath,
		"/redfish/v1/EventService/Subscriptions/them": "http://someone-else/hooks",
	})
	_, _, created, deleted, err := reconcileAgainst(t, b, want)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if deleted != 2 {
		t.Fatalf("deleted %d, want 2", deleted)
	}
	if created != 1 {
		t.Fatalf("created %d, want 1", created)
	}
	dests := b.destinations()
	for _, d := range dests {
		if strings.Contains(d, "10.0.0.5") || strings.Contains(d, ":9999") {
			t.Fatalf("stale subscription survived: %s", d)
		}
	}
	// Another consumer's subscription is none of our business.
	found := false
	for _, d := range dests {
		if d == "http://someone-else/hooks" {
			found = true
		}
	}
	if !found {
		t.Fatal("deleted a subscription belonging to another consumer")
	}
}

func TestReconcileIsIdempotent(t *testing.T) {
	want := "http://10.0.0.9:9143" + EventPath
	b := newSubsBMC(map[string]string{
		"/redfish/v1/EventService/Subscriptions/1": want,
	})
	_, _, created, deleted, err := reconcileAgainst(t, b, want)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if created != 0 || deleted != 0 {
		t.Fatalf("created=%d deleted=%d, want 0/0", created, deleted)
	}
}

// A duplicate of our own destination delivers every event twice, and the
// second copy is indistinguishable from a genuine repeat.
func TestReconcileCollapsesDuplicates(t *testing.T) {
	want := "http://10.0.0.9:9143" + EventPath
	b := newSubsBMC(map[string]string{
		"/redfish/v1/EventService/Subscriptions/1": want,
		"/redfish/v1/EventService/Subscriptions/2": want,
	})
	_, _, created, deleted, err := reconcileAgainst(t, b, want)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if created != 0 || deleted != 1 {
		t.Fatalf("created=%d deleted=%d, want 0/1", created, deleted)
	}
}

func TestDefaultDestinationIsPlainHTTP(t *testing.T) {
	if got := DefaultDestination("10.0.0.9:9143", false); got != "http://10.0.0.9:9143"+EventPath {
		t.Fatalf("got %s", got)
	}
	if got := DefaultDestination("collector:9143", true); !strings.HasPrefix(got, "https://") {
		t.Fatalf("got %s", got)
	}
}
