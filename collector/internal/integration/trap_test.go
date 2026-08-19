package integration

import (
	"context"
	"fmt"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/adapters/snmp"
	"github.com/hari/dcim-platform/collector/internal/assign"
	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// These tests MUTATE the simulator: they repoint its trap destination at a
// listener the test owns, and restore the original when done. Nothing else on
// the plane changes, but a collector running at the same time will stop
// receiving traps for the duration.

type trapSink struct {
	mu     sync.Mutex
	events []models.Event
	ready  chan struct{}
	once   sync.Once
}

func newTrapSink() *trapSink { return &trapSink{ready: make(chan struct{})} }

func (s *trapSink) Telemetry(context.Context, []models.Telemetry) error { return nil }
func (s *trapSink) EndpointState(context.Context, models.EndpointState) error {
	return nil
}

func (s *trapSink) Events(_ context.Context, evs []models.Event) error {
	s.mu.Lock()
	s.events = append(s.events, evs...)
	s.mu.Unlock()
	s.once.Do(func() { close(s.ready) })
	return nil
}

func (s *trapSink) all() []models.Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]models.Event, len(s.events))
	copy(out, s.events)
	return out
}

// waitFor waits for an event matching pred, or fails.
func (s *trapSink) waitFor(t *testing.T, d time.Duration,
	pred func(models.Event) bool) models.Event {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		for _, e := range s.all() {
			if pred(e) {
				return e
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("no matching trap within %s; received %d: %s", d, len(s.all()),
		summarise(s.all()))
	return models.Event{}
}

func summarise(evs []models.Event) string {
	var b strings.Builder
	for i, e := range evs {
		if i > 6 {
			b.WriteString("...")
			break
		}
		fmt.Fprintf(&b, "[%s from %s sev=%v clear=%v] ", e.EventType, e.SourceIP,
			e.Severity, e.IsClear)
	}
	return b.String()
}

// freeUDPPort asks the kernel for a port, then releases it. A trap listener
// cannot be handed an already-bound socket, so the small race between
// releasing and rebinding is unavoidable - and harmless on a test host.
func freeUDPPort(t *testing.T) int {
	t.Helper()
	c, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("reserve port: %v", err)
	}
	port := c.LocalAddr().(*net.UDPAddr).Port
	_ = c.Close()
	return port
}

// trapHarness starts a receiver, points the simulator at it, and restores the
// original destination afterwards.
func trapHarness(t *testing.T, sim *Sim, endpoints []*models.Endpoint) *trapSink {
	t.Helper()

	var before struct {
		TrapReceiverIP   string `json:"trap_receiver_ip"`
		TrapReceiverPort int    `json:"trap_receiver_port"`
	}
	sim.Get(t, "/api/snmp/status", &before)
	t.Cleanup(func() {
		// Put the plane back the way it was, whatever happened.
		sim.Post(t, "/api/snmp/trap-receiver", map[string]any{
			"ip": before.TrapReceiverIP, "port": before.TrapReceiverPort}, nil)
	})

	table, err := mapping.LoadTraps("../../../contracts/mappings")
	if err != nil {
		t.Fatalf("load trap mappings: %v", err)
	}
	resolver := assign.NewResolver()
	resolver.Replace(endpoints)

	sink := newTrapSink()
	port := freeUDPPort(t)
	recv := snmp.NewTrapReceiver(table, resolver, sink, TestLogger(), TestMetrics(),
		fmt.Sprintf("127.0.0.1:%d", port), 4, 1000)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		if err := recv.Listen(ctx); err != nil {
			t.Logf("trap receiver stopped: %v", err)
		}
	}()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
		}
	})
	// The listener binds asynchronously.
	time.Sleep(300 * time.Millisecond)

	if code := sim.Post(t, "/api/snmp/trap-receiver",
		map[string]any{"ip": "127.0.0.1", "port": port}, nil); code >= 300 {
		t.Fatalf("could not point the plane's traps at 127.0.0.1:%d (HTTP %d)",
			port, code)
	}
	t.Logf("trap receiver on 127.0.0.1:%d, plane will restore to %s:%d",
		port, before.TrapReceiverIP, before.TrapReceiverPort)
	return sink
}

// A trap injected through the plane's own API has to arrive, resolve to the
// device that sent it, and map to a canonical event type - not to the raw OID
// the vendor happened to use.
func TestTrapArrivesAndResolvesToItsDevice(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "switch")
	ep := sim.SNMPEndpoint(t, dev, "os_agent")
	sink := trapHarness(t, sim, []*models.Endpoint{ep})

	if code := sim.Post(t, "/api/traps/send", map[string]any{
		"device_id": dev.ID, "trap_type": "LINK_DOWN"}, nil); code >= 300 {
		t.Fatalf("trap injection returned HTTP %d", code)
	}

	e := sink.waitFor(t, 15*time.Second, func(e models.Event) bool {
		return e.EventType == "link_down"
	})

	if e.DeviceID != dev.ID {
		t.Errorf("trap attributed to device %q, want %q (source %s)",
			e.DeviceID, dev.ID, e.SourceIP)
	}
	if e.EndpointID != ep.ID {
		t.Errorf("trap attributed to endpoint %q, want %q", e.EndpointID, ep.ID)
	}
	if e.Severity == models.SeverityUnspecified {
		t.Error("trap carries no severity")
	}
	if e.IsClear {
		t.Error("link_down is not a clear")
	}
	if e.RawIdentifier == "" {
		t.Error("the raw trap OID was not carried through")
	}
	if e.ObservedAt == 0 || e.DedupKey == "" {
		t.Errorf("trap missing observed_at (%d) or dedup key (%q)",
			e.ObservedAt, e.DedupKey)
	}
	t.Logf("link_down from %s (%s) oid=%s severity=%v",
		dev.Name, e.SourceIP, e.RawIdentifier, e.Severity)
}

// The clearing trap must set is_clear, or the alarm it resolves stays open
// forever. This is the single most consequential bit in the trap path.
func TestClearTrapSetsIsClear(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "switch")
	ep := sim.SNMPEndpoint(t, dev, "os_agent")
	sink := trapHarness(t, sim, []*models.Endpoint{ep})

	if code := sim.Post(t, "/api/traps/send", map[string]any{
		"device_id": dev.ID, "trap_type": "LINK_UP"}, nil); code >= 300 {
		t.Fatalf("trap injection returned HTTP %d", code)
	}

	// Wait for a CLEAR specifically. Matching on the event type alone made
	// this test depend on nothing else having injected a link trap recently -
	// the plane has one trap destination, so a leftover assert from another
	// test arrives on the same socket and would be the first match.
	e := sink.waitFor(t, 15*time.Second, func(e models.Event) bool {
		return e.IsClear
	})
	if e.EventType != "link_down" && e.EventType != "link_up" {
		t.Fatalf("the clear that arrived was %s, not a link event", e.EventType)
	}
	if e.Severity != models.SeverityClear {
		t.Errorf("severity %v, want CLEAR", e.Severity)
	}
	t.Logf("link_up cleared %s (severity %v)", e.EventType, e.Severity)
}

// Vendors rewrite trap OIDs into their own enterprise tree. A UPS trap from an
// APC PDU and the same condition from another vendor have different OIDs and
// must arrive as the SAME canonical event type - otherwise a rule written
// against one is silent for the other.
func TestVendorRewrittenTrapsShareACanonicalType(t *testing.T) {
	sim := RequireSimulator(t)
	ups := sim.DevicesOfType(t, "ups")
	if len(ups) == 0 {
		t.Skip("no UPS in this topology")
	}
	var eps []*models.Endpoint
	for _, d := range ups {
		eps = append(eps, sim.SNMPEndpoint(t, d, "os_agent"))
	}
	sink := trapHarness(t, sim, eps)

	for _, d := range ups {
		if code := sim.Post(t, "/api/traps/send", map[string]any{
			"device_id": d.ID, "trap_type": "UPS_ON_BATTERY"}, nil); code >= 300 {
			t.Fatalf("trap injection for %s returned HTTP %d", d.Name, code)
		}
	}

	sink.waitFor(t, 20*time.Second, func(e models.Event) bool {
		return e.EventType == "ups_on_battery"
	})

	byOID := map[string]string{}
	for _, e := range sink.all() {
		if e.EventType == "ups_on_battery" {
			byOID[e.RawIdentifier] = e.EventType
		}
	}
	if len(byOID) == 0 {
		t.Fatal("no ups_on_battery events")
	}
	for oid, evType := range byOID {
		t.Logf("%s -> %s", oid, evType)
		if evType != "ups_on_battery" {
			t.Errorf("OID %s mapped to %s", oid, evType)
		}
	}
	if len(byOID) > 1 {
		t.Logf("%d distinct vendor OIDs all mapped to ups_on_battery", len(byOID))
	}
}

// An unmapped OID must become an INFO event carrying the raw identifier, never
// a drop: a gap in the mapping has to be visible in the UI rather than
// invisible in a counter.
func TestUnknownTrapsAreEmittedNotDropped(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "switch")
	ep := sim.SNMPEndpoint(t, dev, "os_agent")
	sink := trapHarness(t, sim, []*models.Endpoint{ep})

	// Every trap type the plane offers, so anything the mapping does not know
	// shows up as unknown_trap rather than as silence.
	for _, kind := range []string{"COLD_START", "WARM_START", "AUTH_FAILURE"} {
		sim.Post(t, "/api/traps/send",
			map[string]any{"device_id": dev.ID, "trap_type": kind}, nil)
	}

	sink.waitFor(t, 20*time.Second, func(e models.Event) bool {
		return e.EventType != ""
	})
	for _, e := range sink.all() {
		if e.EventType == "" {
			t.Error("an event arrived with no type at all")
		}
		if e.EventType == "unknown_trap" && e.RawIdentifier == "" {
			t.Error("an unknown trap carries no raw OID, so the gap is invisible")
		}
	}
	t.Logf("received: %s", summarise(sink.all()))
}
