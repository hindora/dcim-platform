package sched

import (
	"context"
	"io"
	"log/slog"
	"sync/atomic"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

func quietLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// hourly is an endpoint the real one-second wheel never makes due during a
// test, so only the test's own dispatch calls do.
func hourly(id, proto string) *models.Endpoint {
	return &models.Endpoint{ID: id, Protocol: proto, Address: "10.0.0." + id,
		Poll: models.PollProfile{IntervalS: 3600, TimeoutMs: 1000}}
}

func waitFor(t *testing.T, what string, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !ok() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestSnapshotCountsWorkAndMeasuresBusyAndSlotWaits(t *testing.T) {
	s := New(Options{Workers: 2, ProtoLimits: map[string]int{"snmp": 1}},
		func(ctx context.Context, ep *models.Endpoint) { time.Sleep(150 * time.Millisecond) },
		quietLog(), obs.NewMetrics())
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s.Start(ctx)
	for _, id := range []string{"1", "2", "3"} {
		s.Add(hourly(id, "snmp"))
	}
	s.dispatch(time.Now().Add(2 * time.Hour))
	waitFor(t, "three completed polls", func() bool {
		return s.Snapshot().Protocols["snmp"].Completed == 3
	})

	pc := s.Snapshot().Protocols["snmp"]
	if pc.Jobs != 3 || pc.Dispatched != 3 || pc.Limit != 1 {
		t.Fatalf("jobs/dispatched/limit = %d/%d/%d, want 3/3/1", pc.Jobs, pc.Dispatched, pc.Limit)
	}
	if want := 3.0 / 3600; pc.Scheduled < want*0.99 || pc.Scheduled > want*1.01 {
		t.Fatalf("scheduled = %v/s, want %v/s (three jobs an hour apart)", pc.Scheduled, want)
	}
	if pc.BusyNs < uint64(3*150*time.Millisecond) {
		t.Fatalf("busy = %v, want at least the 450ms the three polls slept", time.Duration(pc.BusyNs))
	}
	// Two workers, a protocol limit of one: one poll always waits for the
	// slot while the other runs, and that wait is worker time held.
	if pc.SemWaitNs < uint64(100*time.Millisecond) {
		t.Fatalf("slot wait = %v, want the time a worker sat behind limit 1",
			time.Duration(pc.SemWaitNs))
	}
}

func TestADueEndpointStillRunningCountsAsAnOverrun(t *testing.T) {
	release := make(chan struct{})
	var started atomic.Int32
	s := New(Options{Workers: 1},
		func(ctx context.Context, ep *models.Endpoint) { started.Add(1); <-release },
		quietLog(), obs.NewMetrics())
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s.Start(ctx)
	s.Add(hourly("1", "modbus"))
	s.dispatch(time.Now().Add(2 * time.Hour))
	waitFor(t, "the first poll to start", func() bool { return started.Load() == 1 })
	s.dispatch(time.Now().Add(4 * time.Hour))
	close(release)

	pc := s.Snapshot().Protocols["modbus"]
	if pc.Overrun != 1 || pc.Dispatched != 1 {
		t.Fatalf("overrun/dispatched = %d/%d, want 1/1", pc.Overrun, pc.Dispatched)
	}
}

func TestAFullQueueShedsAndSaysSo(t *testing.T) {
	// Not started: nothing drains the queue, which holds one.
	s := New(Options{Workers: 1, QueueSize: 1},
		func(ctx context.Context, ep *models.Endpoint) {}, quietLog(), obs.NewMetrics())
	for _, id := range []string{"1", "2", "3"} {
		s.Add(hourly(id, "redfish"))
	}
	s.dispatch(time.Now().Add(2 * time.Hour))
	pc := s.Snapshot().Protocols["redfish"]
	if pc.Dispatched != 1 || pc.Shed != 2 {
		t.Fatalf("dispatched/shed = %d/%d, want 1/2", pc.Dispatched, pc.Shed)
	}
}

func TestAPollQueuedLongerThanLateAfterIsLate(t *testing.T) {
	s := New(Options{Workers: 1},
		func(ctx context.Context, ep *models.Endpoint) {}, quietLog(), obs.NewMetrics())
	job := &Job{Endpoint: hourly("1", "bacnet"), Interval: time.Hour,
		enqueued: time.Now().Add(-2 * LateAfter)}
	s.execute(context.Background(), job)
	pc := s.Snapshot().Protocols["bacnet"]
	if pc.Late != 1 || pc.Completed != 1 {
		t.Fatalf("late/completed = %d/%d, want 1/1", pc.Late, pc.Completed)
	}
	if pc.QueueWaitNs < uint64(2*LateAfter) {
		t.Fatalf("queue wait = %v, want at least %v", time.Duration(pc.QueueWaitNs), 2*LateAfter)
	}
}
