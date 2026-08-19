package integration

import (
	"context"
	"fmt"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/adapters/redfish"
	"github.com/hari/dcim-platform/collector/internal/assign"
	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

func redfishAdapter(t *testing.T) *redfish.Adapter {
	t.Helper()
	maps, err := mapping.LoadRedfish("../../../contracts/mappings")
	if err != nil {
		t.Fatalf("load redfish mappings: %v", err)
	}
	return redfish.New(maps, TestLogger(), TestMetrics())
}

func TestRedfishThermalAndPower(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "server")
	ep := sim.RedfishEndpoint(t, dev)

	a := redfishAdapter(t)
	out, err := a.Poll(Ctx(t, 30*time.Second), ep)
	if err != nil {
		t.Fatalf("poll %s: %v", dev.Name, err)
	}

	AssertRegistryContract(t, out)
	AssertNoRawIdentifiers(t, out)
	AssertHasMetric(t, out, "inlet_temperature", -20, 80)
	AssertHasMetric(t, out, "power_draw", 0, 20_000_000)
	AssertInstances(t, out, "cpu_temperature", 1)
	t.Logf("%s: %d samples across %d metrics", dev.Name, len(out.Samples),
		len(MetricNames(out)))

	// The session is reused. A BMC is slow to authenticate and some firmware
	// rate-limits repeated basic auth, so a second poll that re-authenticates
	// is a real cost at three hundred servers.
	second, err := a.Poll(Ctx(t, 30*time.Second), ep)
	if err != nil {
		t.Fatalf("second poll: %v", err)
	}
	if len(second.Samples) == 0 {
		t.Fatal("the second poll produced nothing")
	}
}

// A PSU that reports Enabled but not OK is a failing supply. The state has to
// come from BOTH fields, because firmware reports a dead PSU as present.
func TestRedfishPSUStateNeedsBothFields(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "server")

	out, err := redfishAdapter(t).Poll(Ctx(t, 30*time.Second),
		sim.RedfishEndpoint(t, dev))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	instances := AssertInstances(t, out, "psu_state", 1)
	for _, inst := range instances {
		s, _ := Sample(out, "psu_state", inst)
		if s.ValueType != models.ValueTypeBool {
			t.Errorf("psu_state{%s} is %v, want a bool", inst, s.ValueType)
		}
	}
	t.Logf("PSU states: %v", instances)
}

// ------------------------------------------------------------- events
//
// This test MUTATES the simulator: it creates a subscription on one BMC
// pointing at a receiver the test owns, fires a test event, and deletes the
// subscription afterwards. No other BMC is touched.

type eventCapture struct {
	mu     sync.Mutex
	events []models.Event
}

func (c *eventCapture) Telemetry(context.Context, []models.Telemetry) error { return nil }
func (c *eventCapture) EndpointState(context.Context, models.EndpointState) error {
	return nil
}

func (c *eventCapture) Events(_ context.Context, evs []models.Event) error {
	c.mu.Lock()
	c.events = append(c.events, evs...)
	c.mu.Unlock()
	return nil
}

func (c *eventCapture) all() []models.Event {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]models.Event, len(c.events))
	copy(out, c.events)
	return out
}

func freeTCPPort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("reserve port: %v", err)
	}
	port := l.Addr().(*net.TCPAddr).Port
	_ = l.Close()
	return port
}

// A subscription is created by reconciliation, a test event is fired through
// the plane's own API, and it has to arrive at the receiver classified as a
// real event rather than as an unknown one.
func TestRedfishSubscriptionAndTestEvent(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "server")
	ep := sim.RedfishEndpoint(t, dev)

	evMaps, err := mapping.LoadRedfishEvents("../../../contracts/mappings")
	if err != nil {
		t.Fatalf("load redfish event mappings: %v", err)
	}
	resolver := assign.NewResolver()
	resolver.Replace([]*models.Endpoint{ep})

	port := freeTCPPort(t)
	// The path is the standard one; the port is this test's, so reconciliation
	// treats any OTHER port on the same path as a stale copy of itself. That
	// is the behaviour under test, and it is why only this one BMC is touched.
	dest := fmt.Sprintf("http://127.0.0.1:%d%s", port, redfish.EventPath)

	sink := &eventCapture{}
	recv := redfish.NewEventReceiver(redfishAdapter(t), evMaps, resolver, sink,
		TestLogger(), TestMetrics(), fmt.Sprintf("127.0.0.1:%d", port), dest, 2, 500)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		if err := recv.Listen(ctx); err != nil {
			t.Logf("event receiver stopped: %v", err)
		}
	}()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
		}
	})
	time.Sleep(300 * time.Millisecond)

	created, deleted, err := recv.Reconcile(Ctx(t, 30*time.Second), ep)
	if err != nil {
		t.Fatalf("reconcile %s: %v", dev.MgmtIP, err)
	}
	t.Logf("reconcile on %s: created %d, deleted %d stale", dev.MgmtIP, created, deleted)
	if created != 1 {
		t.Fatalf("expected to create one subscription, created %d", created)
	}

	// Clean up whatever we put on the BMC, whichever way this ends.
	t.Cleanup(func() {
		var subs struct {
			Subscriptions []struct {
				IP          string `json:"ip"`
				ID          string `json:"id"`
				Destination string `json:"destination"`
			} `json:"subscriptions"`
		}
		sim.Get(t, "/api/redfish/subscriptions", &subs)
		for _, s := range subs.Subscriptions {
			if s.Destination == dest {
				sim.Post(t, "/api/redfish/unsubscribe",
					map[string]any{"ip": s.IP, "id": s.ID}, nil)
				t.Logf("removed test subscription %s from %s", s.ID, s.IP)
			}
		}
	})

	// Reconciliation is idempotent: running it again must not add a second
	// subscription, because a duplicate delivers every event twice and the
	// copy is indistinguishable from a real repeat.
	created2, _, err := recv.Reconcile(Ctx(t, 30*time.Second), ep)
	if err != nil {
		t.Fatalf("second reconcile: %v", err)
	}
	if created2 != 0 {
		t.Errorf("a second reconcile created %d more subscriptions", created2)
	}

	if code := sim.Post(t, "/api/redfish/test-event", map[string]any{
		"ip": dev.MgmtIP, "message": "CPU temperature critical: 93.5 C",
		"severity": "Critical", "event_type": "Alert"}, nil); code >= 300 {
		t.Fatalf("test event returned HTTP %d", code)
	}

	deadline := time.Now().Add(20 * time.Second)
	var got models.Event
	for time.Now().Before(deadline) {
		for _, e := range sink.all() {
			if e.EventType != "" {
				got = e
				break
			}
		}
		if got.EventType != "" {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if got.EventType == "" {
		t.Fatalf("no event arrived at %s within 20 s", dest)
	}

	if got.EventType != "cpu_temp_critical" {
		t.Errorf("event type %q, want cpu_temp_critical - the message text "+
			"fallback is what classifies a generic MessageId", got.EventType)
	}
	if got.Severity != models.SeverityCritical {
		t.Errorf("severity %v, want CRITICAL", got.Severity)
	}
	if got.DeviceID != dev.ID {
		t.Errorf("event attributed to %q, want %q (context carries the "+
			"endpoint id precisely so this works)", got.DeviceID, dev.ID)
	}
	t.Logf("event: %s severity=%v device=%s message=%q",
		got.EventType, got.Severity, dev.Name, got.Message)
}

// A BMC reset drops every subscription silently and orphans from a previous
// collector address accumulate until the per-BMC cap is hit, after which new
// subscriptions fail with no symptom other than events stopping. Removing
// stale copies of our own destination is what prevents that.
func TestRedfishReconciliationRemovesStaleSubscriptions(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "server")
	ep := sim.RedfishEndpoint(t, dev)

	evMaps, err := mapping.LoadRedfishEvents("../../../contracts/mappings")
	if err != nil {
		t.Fatalf("load redfish event mappings: %v", err)
	}

	// A subscription from a previous life: same path, different address.
	stale := "http://198.51.100.9:9143" + redfish.EventPath
	if code := sim.Post(t, "/api/redfish/subscribe", map[string]any{
		"ip": dev.MgmtIP, "destination": stale, "context": "old-endpoint-id"},
		nil); code >= 300 {
		t.Fatalf("could not plant a stale subscription (HTTP %d)", code)
	}

	port := freeTCPPort(t)
	dest := fmt.Sprintf("http://127.0.0.1:%d%s", port, redfish.EventPath)
	recv := redfish.NewEventReceiver(redfishAdapter(t), evMaps, assign.NewResolver(),
		&eventCapture{}, TestLogger(), TestMetrics(),
		fmt.Sprintf("127.0.0.1:%d", port), dest, 1, 100)

	t.Cleanup(func() {
		var subs struct {
			Subscriptions []struct {
				IP          string `json:"ip"`
				ID          string `json:"id"`
				Destination string `json:"destination"`
			} `json:"subscriptions"`
		}
		sim.Get(t, "/api/redfish/subscriptions", &subs)
		for _, s := range subs.Subscriptions {
			if s.Destination == dest || s.Destination == stale {
				sim.Post(t, "/api/redfish/unsubscribe",
					map[string]any{"ip": s.IP, "id": s.ID}, nil)
			}
		}
	})

	created, deleted, err := recv.Reconcile(Ctx(t, 30*time.Second), ep)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if deleted < 1 {
		t.Errorf("the stale subscription at %s was not removed", stale)
	}
	if created != 1 {
		t.Errorf("created %d subscriptions, want 1", created)
	}

	var subs struct {
		Subscriptions []struct {
			IP          string `json:"ip"`
			Destination string `json:"destination"`
		} `json:"subscriptions"`
	}
	sim.Get(t, "/api/redfish/subscriptions", &subs)
	for _, s := range subs.Subscriptions {
		if s.Destination == stale {
			t.Errorf("%s still holds the stale subscription", s.IP)
		}
	}
	t.Logf("reconcile removed %d stale, created %d", deleted, created)
}
