package snmp

import (
	"context"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/assign"
	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

type trapSink struct {
	mu     sync.Mutex
	events []models.Event
}

func (s *trapSink) Telemetry(context.Context, []models.Telemetry) error { return nil }
func (s *trapSink) EndpointState(context.Context, models.EndpointState) error {
	return nil
}

func (s *trapSink) Events(_ context.Context, evs []models.Event) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.events = append(s.events, evs...)
	return nil
}

func (s *trapSink) all() []models.Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]models.Event, len(s.events))
	copy(out, s.events)
	return out
}

func newHoldReceiver(t *testing.T) (*TrapReceiver, *trapSink, *assign.Resolver) {
	t.Helper()
	table, err := mapping.LoadTraps("../../../../contracts/mappings")
	if err != nil {
		t.Fatalf("load trap mappings: %v", err)
	}
	sink := &trapSink{}
	resolver := assign.NewResolver()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	r := NewTrapReceiver(table, resolver, sink, log, obs.NewMetrics(),
		"127.0.0.1:0", 1, 1000)
	return r, sink, resolver
}

func trap(source, oid string, at time.Time) *heldTrap {
	return &heldTrap{source: source, community: source, trapOID: oid,
		varbinds: map[string]string{}, at: at}
}

func endpoint(id, device, addr string) *models.Endpoint {
	return &models.Endpoint{ID: id, DeviceID: device, Address: addr,
		DeviceType: "server", Protocol: "snmp_trap"}
}

// The race this exists for.
//
// The socket binds in milliseconds; the first assignment lands twenty seconds
// later. A trap in that window resolves to nothing, and an event with no
// device on it raises no alarm - measured live, not theorised: a CPU trap
// fired 22 seconds after a restart was logged as "trap from an unknown source"
// and the fault never reached the console.
func TestATrapArrivingBeforeTheInventoryIsHeldNotPublished(t *testing.T) {
	r, sink, _ := newHoldReceiver(t)

	if !r.hold(trap("10.50.21.26", "1.3.6.1.4.1.99999.1.1", time.Now())) {
		t.Fatal("trap was not held while the inventory was empty")
	}
	if got := len(sink.all()); got != 0 {
		t.Fatalf("published %d events while holding; nothing should be out yet", got)
	}
	if r.HeldCount() != 1 {
		t.Fatalf("held count = %d, want 1", r.HeldCount())
	}
}

func TestItIsPublishedWithItsDeviceOnceTheInventoryArrives(t *testing.T) {
	r, sink, resolver := newHoldReceiver(t)
	arrived := time.Now().Add(-8 * time.Second)
	r.hold(trap("10.50.21.26", "1.3.6.1.4.1.99999.1.1", arrived))

	resolver.Replace([]*models.Endpoint{
		endpoint("ep-1", "dev-1", "10.50.21.26"),
	})
	r.flushHeld(context.Background(), false)

	events := sink.all()
	if len(events) != 1 {
		t.Fatalf("published %d events, want 1", len(events))
	}
	if events[0].DeviceID != "dev-1" {
		t.Errorf("device = %q, want dev-1 - the whole point is attribution",
			events[0].DeviceID)
	}
	if r.HeldCount() != 0 {
		t.Errorf("still holding %d after a successful flush", r.HeldCount())
	}
}

// The replay carries the ARRIVAL time, not the replay time.
//
// Stamping it with the replay would put the alarm's first_seen a minute after
// the condition started, and - worse - could make a raise look newer than the
// clear that actually followed it, leaving an alarm standing on a device that
// is fine.
func TestTheOriginalArrivalTimeSurvivesTheHold(t *testing.T) {
	r, sink, resolver := newHoldReceiver(t)
	arrived := time.Now().Add(-45 * time.Second)
	r.hold(trap("10.50.21.26", "1.3.6.1.4.1.99999.1.1", arrived))

	resolver.Replace([]*models.Endpoint{endpoint("ep-1", "dev-1", "10.50.21.26")})
	r.flushHeld(context.Background(), false)

	got := sink.all()[0].ObservedAt
	if got != arrived.UnixMicro() {
		t.Fatalf("observed_at = %d, want the arrival time %d",
			got, arrived.UnixMicro())
	}
}

// A raise and the clear that followed it can both be in the buffer. Publishing
// them out of order would leave the alarm raised by the clear and cleared by
// the raise: a fault on the console for a device that recovered.
func TestOrderIsPreservedAcrossTheHold(t *testing.T) {
	r, sink, resolver := newHoldReceiver(t)
	base := time.Now().Add(-30 * time.Second)
	r.hold(trap("10.50.21.26", "1.3.6.1.4.1.99999.1.1", base))                    // cpuHigh
	r.hold(trap("10.50.21.26", "1.3.6.1.4.1.99999.1.3", base.Add(2*time.Second))) // cpuNormal

	resolver.Replace([]*models.Endpoint{endpoint("ep-1", "dev-1", "10.50.21.26")})
	r.flushHeld(context.Background(), false)

	events := sink.all()
	if len(events) != 2 {
		t.Fatalf("published %d events, want 2", len(events))
	}
	if events[0].ObservedAt >= events[1].ObservedAt {
		t.Fatalf("out of order: %d then %d", events[0].ObservedAt,
			events[1].ObservedAt)
	}
	if events[0].RawIdentifier != "1.3.6.1.4.1.99999.1.1" {
		t.Errorf("first event is %q; the raise must come before the clear",
			events[0].RawIdentifier)
	}
}

// A loaded inventory that does not contain the source is a real finding, not a
// race. Holding it would delay the truth by two minutes and then report it
// anyway.
func TestAnUnknownSourceIsNotHeldOnceTheInventoryIsLoaded(t *testing.T) {
	r, _, resolver := newHoldReceiver(t)
	resolver.Replace([]*models.Endpoint{endpoint("ep-1", "dev-1", "10.50.21.26")})

	if r.hold(trap("192.0.2.99", "1.3.6.1.4.1.99999.1.1", time.Now())) {
		t.Fatal("held a trap from an unknown source against a loaded inventory")
	}
}

// The buffer survives a startup; it does not absorb a device plane pointed at
// the wrong collector.
func TestTheBufferHasACeilingAndOverflowIsPublishedNotLost(t *testing.T) {
	r, _, _ := newHoldReceiver(t)
	r.holdMax = 3
	for i := 0; i < 3; i++ {
		if !r.hold(trap("10.50.21.26", "1.3.6.1.4.1.99999.1.1", time.Now())) {
			t.Fatalf("trap %d was not held below the ceiling", i)
		}
	}
	// Refused, which sends the caller down the publish path rather than
	// discarding anything.
	if r.hold(trap("10.50.21.26", "1.3.6.1.4.1.99999.1.1", time.Now())) {
		t.Fatal("held past the ceiling")
	}
}

// Two minutes is long past any assignment fetch, so a trap still unattributable
// then is not waiting on one. It goes out unattributed rather than being kept
// for ever or thrown away.
func TestAnUnattributableTrapIsEventuallyPublishedAnyway(t *testing.T) {
	r, sink, _ := newHoldReceiver(t)
	r.holdFor = 50 * time.Millisecond
	r.hold(trap("192.0.2.99", "1.3.6.1.4.1.99999.1.1", time.Now()))

	r.flushHeld(context.Background(), false)
	if len(sink.all()) != 0 {
		t.Fatal("published before the hold expired")
	}

	time.Sleep(60 * time.Millisecond)
	r.flushHeld(context.Background(), false)

	events := sink.all()
	if len(events) != 1 {
		t.Fatalf("published %d events after expiry, want 1", len(events))
	}
	if events[0].DeviceID != "" {
		t.Errorf("device = %q, want empty - it never resolved", events[0].DeviceID)
	}
	if r.HeldCount() != 0 {
		t.Errorf("still holding %d after expiry", r.HeldCount())
	}
}

// Shutdown publishes what is still held. Unattributed is worse than
// attributed and far better than gone.
func TestShutdownFlushesRatherThanDiscards(t *testing.T) {
	r, sink, _ := newHoldReceiver(t)
	r.hold(trap("10.50.21.26", "1.3.6.1.4.1.99999.1.1", time.Now()))

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); r.drainHeld(ctx) }()
	cancel()
	<-done

	if got := len(sink.all()); got != 1 {
		t.Fatalf("published %d events on shutdown, want the held one", got)
	}
}
