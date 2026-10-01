package sched

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/internal/throttle"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

func behindGateway(id string, tl *models.TargetLimit) *models.Endpoint {
	ep := hourly(id, "modbus")
	ep.Address = "10.52.1.200" // every slave reaches the same Moxa gateway
	ep.TargetLimit = tl
	return ep
}

func TestABusyTargetDefersPollsInsteadOfHoldingWorkers(t *testing.T) {
	lim := throttle.New()
	release := make(chan struct{})
	var started atomic.Int32
	s := New(Options{Workers: 4, Limiter: lim, Budgeted: true},
		func(ctx context.Context, ep *models.Endpoint) { started.Add(1); <-release },
		quietLog(), obs.NewMetrics())
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s.Start(ctx)
	for _, id := range []string{"1", "2", "3"} {
		ep := behindGateway(id, &models.TargetLimit{MaxConcurrent: 1})
		lim.Track(ep)
		s.Add(ep)
	}
	now := time.Now().Add(2 * time.Hour)
	s.dispatch(now)
	waitFor(t, "one poll at the gateway", func() bool { return started.Load() == 1 })

	pc := s.Snapshot().Protocols["modbus"]
	if pc.Dispatched != 1 || pc.ThrottledTarget != 2 || pc.Shed != 0 {
		t.Fatalf("dispatched/throttled/shed = %d/%d/%d, want 1/2/0",
			pc.Dispatched, pc.ThrottledTarget, pc.Shed)
	}
	// Retried every tick while the gateway is busy, but counted once.
	s.dispatch(now.Add(time.Second))
	s.dispatch(now.Add(2 * time.Second))
	if pc := s.Snapshot().Protocols["modbus"]; pc.ThrottledTarget != 2 {
		t.Fatalf("throttled = %d after retries, want 2 (once per cycle)", pc.ThrottledTarget)
	}
	close(release)
	// The slot is released; the deferred ones go one at a time.
	for i := 3; i <= 4; i++ {
		waitFor(t, "the gateway slot to free", func() bool {
			return s.Snapshot().Protocols["modbus"].Completed >= uint64(i-2)
		})
		s.dispatch(now.Add(time.Duration(i) * time.Second))
	}
	waitFor(t, "all three to run", func() bool { return started.Load() == 3 })
}

func TestAGapInsideTheTickIsHeldByATimerNotAWorker(t *testing.T) {
	lim := throttle.New()
	var starts [2]atomic.Int64
	var n atomic.Int32
	s := New(Options{Workers: 1, Limiter: lim},
		func(ctx context.Context, ep *models.Endpoint) {
			starts[n.Add(1)-1].Store(time.Now().UnixNano())
		}, quietLog(), obs.NewMetrics())
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s.Start(ctx)
	for _, id := range []string{"1", "2"} {
		ep := behindGateway(id, &models.TargetLimit{MinIntervalMs: 300})
		lim.Track(ep)
		s.Add(ep)
	}
	s.dispatch(time.Now().Add(2 * time.Hour))
	waitFor(t, "both polls", func() bool { return n.Load() == 2 })
	gap := time.Duration(starts[1].Load() - starts[0].Load())
	if gap < 250*time.Millisecond {
		t.Fatalf("second poll started %v after the first, want the 300 ms gap", gap)
	}
}

func TestAPoolOverBudgetIsDeferredAndLivenessIsNot(t *testing.T) {
	lim := throttle.New()
	now := time.Now().Add(2 * time.Hour)
	lim.SetBudgets(map[string]float64{"bms": 10})
	lim.Charge("bms", 1000, now) // deep in debt
	run := func(ctx context.Context, ep *models.Endpoint) {}
	polls := New(Options{Workers: 1, Limiter: lim, Budgeted: true}, run, quietLog(), obs.NewMetrics())
	probes := New(Options{Workers: 1, Limiter: lim}, run, quietLog(), obs.NewMetrics())
	ep := hourly("9", "bacnet")
	ep.PoolID = "bms"
	polls.Add(ep)
	probes.Add(ep)
	polls.dispatch(now)
	probes.dispatch(now)
	if pc := polls.Snapshot().Protocols["bacnet"]; pc.ThrottledBudget != 1 || pc.Dispatched != 0 {
		t.Fatalf("poll scheduler throttled/dispatched = %d/%d, want 1/0",
			pc.ThrottledBudget, pc.Dispatched)
	}
	if pc := probes.Snapshot().Protocols["bacnet"]; pc.Dispatched != 1 {
		t.Fatal("a liveness probe was held by the pool budget - the pool would look dead")
	}
}
