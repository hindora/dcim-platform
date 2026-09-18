package app

import (
	"context"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/health"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/internal/sched"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

type nullSink struct{}

func (nullSink) Telemetry(context.Context, []models.Telemetry) error       { return nil }
func (nullSink) Events(context.Context, []models.Event) error              { return nil }
func (nullSink) EndpointState(context.Context, models.EndpointState) error { return nil }

type plainAdapter struct{ proto string }

func (p plainAdapter) Protocol() string          { return p.proto }
func (plainAdapter) Init(context.Context) error  { return nil }
func (plainAdapter) Close(context.Context) error { return nil }
func (plainAdapter) Poll(context.Context, *models.Endpoint) (*models.PollOutcome, error) {
	return &models.PollOutcome{}, nil
}

type pingAdapter struct{ plainAdapter }

func (pingAdapter) Ping(context.Context, *models.Endpoint) error { return nil }

func availabilityApp(t *testing.T) *App {
	t.Helper()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	mets := obs.NewMetrics()
	a := &App{
		log:        log,
		mets:       mets,
		tracker:    health.NewTracker(3, "col-test", nullSink{}, log, mets, 0),
		availEvery: 30 * time.Second,
		adapters: map[string]models.Adapter{
			"snmp":   pingAdapter{plainAdapter{"snmp"}},
			"bacnet": plainAdapter{"bacnet"},
		},
	}
	a.avail = sched.New(sched.Options{Workers: 1}, a.ping, log, mets)
	return a
}

func endpointEvery(id, proto string, seconds int) *models.Endpoint {
	ep := &models.Endpoint{ID: id, Protocol: proto}
	ep.Poll.IntervalS = seconds
	return ep
}

// The leaf switch polled every 600 s is exactly who needs the probe.
func TestASlowPolledEndpointIsWatched(t *testing.T) {
	a := availabilityApp(t)
	a.watchAvailability(endpointEvery("leaf", "snmp", 600))
	if a.avail.Count() != 1 {
		t.Fatalf("watched = %d, want 1", a.avail.Count())
	}
}

// Polled at least as often as the probe would run: the poll already is the
// liveness check, and probing as well only doubles the load on the agent.
func TestAFastPolledEndpointIsNotProbedAsWell(t *testing.T) {
	a := availabilityApp(t)
	a.watchAvailability(endpointEvery("ahu", "snmp", 30))
	if a.avail.Count() != 0 {
		t.Fatalf("watched = %d, want 0", a.avail.Count())
	}
}

func TestAnAdapterThatCannotProbeIsLeftToItsPoll(t *testing.T) {
	a := availabilityApp(t)
	a.watchAvailability(endpointEvery("vav", "bacnet", 600))
	if a.avail.Count() != 0 {
		t.Fatalf("watched = %d, want 0", a.avail.Count())
	}
}

// A profile change that speeds the poll up takes the endpoint off the wheel.
func TestSpeedingUpThePollStopsTheProbe(t *testing.T) {
	a := availabilityApp(t)
	a.watchAvailability(endpointEvery("leaf", "snmp", 600))
	a.watchAvailability(endpointEvery("leaf", "snmp", 15))
	if a.avail.Count() != 0 {
		t.Fatalf("watched = %d, want 0 after the poll got faster", a.avail.Count())
	}
}
