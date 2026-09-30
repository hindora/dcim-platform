// Package capacity turns the scheduler's cumulative counters into the
// numbers a platform judges a collector's capacity on (docs/26 Phase 5).
//
// The measure is worker saturation, not a weighted estimate. Real products
// size pollers two ways: by a published weight table (Schneider's Gateway
// tiers put an SNMPv3 device at about four v1/Modbus devices) and by
// measuring how busy the poller actually is (Zabbix's poller busy %,
// Niagara's poll-scheduler busy time, SolarWinds' polling rate against the
// engine's maximum). A weight table is a planning guess about someone
// else's implementation; the busy fraction is this process, on this host,
// against these devices. A v3 poll that costs four times a v2c one already
// shows up as four times the worker-seconds, so the measured cost per poll
// per protocol IS the weight - derived, not asserted.
//
// Only the poll scheduler is measured. gNMI streams and the trap and event
// listeners do not hold poll workers, so they cannot exhaust the pool this
// reports on; their own backpressure (publish queue, drops) is judged
// elsewhere.
package capacity

import (
	"sort"
	"sync"
	"time"

	"github.com/hari/dcim-platform/collector/internal/sched"
)

// Window is how far back a report looks. Five minutes, because a single
// slow ifTable walk can pin a worker for most of a minute: judged over one
// heartbeat, every busy spike would be an alarm, and SolarWinds and Zabbix
// both alert on an average, not an instant.
const Window = 5 * time.Minute

// Points counts samples published per protocol and per pool. A point is one
// sample - one metric value from one device - which is what a BMS
// supervisor's rate budget is written in and what Trellis sizes its engine
// by, rather than polls, which differ tenfold in size between a UPS status
// read and a PDU per-outlet walk.
type Points struct {
	mu      sync.Mutex
	byProto map[string]uint64
	byPool  map[string]uint64
}

func NewPoints() *Points {
	return &Points{byProto: map[string]uint64{}, byPool: map[string]uint64{}}
}

// Add records n samples from one poll. pool is the endpoint's resolved
// pool, empty for an unpooled endpoint.
func (p *Points) Add(proto, pool string, n int) {
	if n <= 0 {
		return
	}
	p.mu.Lock()
	p.byProto[proto] += uint64(n)
	if pool != "" {
		p.byPool[pool] += uint64(n)
	}
	p.mu.Unlock()
}

func (p *Points) read() (map[string]uint64, map[string]uint64) {
	p.mu.Lock()
	defer p.mu.Unlock()
	a := make(map[string]uint64, len(p.byProto))
	for k, v := range p.byProto {
		a[k] = v
	}
	b := make(map[string]uint64, len(p.byPool))
	for k, v := range p.byPool {
		b[k] = v
	}
	return a, b
}

// Reading is one instant: the scheduler's counters plus the points.
type Reading struct {
	Sched      sched.Snapshot
	PointsProt map[string]uint64
	PointsPool map[string]uint64
}

func Read(s *sched.Scheduler, p *Points) Reading {
	a, b := p.read()
	return Reading{Sched: s.Snapshot(), PointsProt: a, PointsPool: b}
}

// Tracker keeps enough readings to report over the trailing Window.
type Tracker struct {
	mu   sync.Mutex
	ring []Reading
}

// Observe records a reading and returns the report over the trailing
// window: from the oldest reading still inside it, or the oldest held at all
// while the process is younger than the window.
func (t *Tracker) Observe(r Reading) *Report {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.ring = append(t.ring, r)
	cut := r.Sched.At.Add(-Window)
	// Keep one reading at or before the cut so the window is never shorter
	// than Window once the process is old enough to have one.
	keep := 0
	for i := len(t.ring) - 1; i >= 0; i-- {
		if !t.ring[i].Sched.At.After(cut) {
			keep = i
			break
		}
	}
	t.ring = t.ring[keep:]
	if len(t.ring) < 2 {
		// One reading is an instant, not a window: nothing to divide by.
		return nil
	}
	return Build(t.ring[0], r)
}

// Report is what the heartbeat carries, as JSON. Rates are per second over
// WindowS; counts are totals within it.
type Report struct {
	WindowS float64 `json:"window_s"`
	Workers int     `json:"workers"`
	// Worker time held over worker time available. The number the 85% line
	// is drawn on.
	BusyPct        float64                `json:"busy_pct"`
	ScheduledPerS  float64                `json:"scheduled_polls_per_s"`
	PollsPerS      float64                `json:"polls_per_s"`
	PointsPerS     float64                `json:"points_per_s"`
	Shed           uint64                 `json:"shed"`
	Overrun        uint64                 `json:"overrun"`
	Late           uint64                 `json:"late"`
	QueueWaitAvgMs float64                `json:"queue_wait_avg_ms"`
	Protocols      map[string]ProtoReport `json:"protocols"`
	Pools          map[string]PoolReport  `json:"pools,omitempty"`
}

type ProtoReport struct {
	Jobs          int     `json:"jobs"`
	Limit         int     `json:"limit"`
	ScheduledPerS float64 `json:"scheduled_per_s"`
	PollsPerS     float64 `json:"polls_per_s"`
	PointsPerS    float64 `json:"points_per_s"`
	// This protocol's share of the whole pool's worker time.
	BusyPct float64 `json:"busy_pct"`
	// Worker-seconds one poll costs, measured: the protocol's real weight.
	CostSPerPoll float64 `json:"cost_s_per_poll"`
	// Share of this protocol's worker time spent waiting for a protocol or
	// per-host slot. High means raise the limit, not the pool.
	SemWaitPct float64 `json:"sem_wait_pct"`
	Shed       uint64  `json:"shed"`
	Overrun    uint64  `json:"overrun"`
	Late       uint64  `json:"late"`
}

type PoolReport struct {
	PointsPerS float64 `json:"points_per_s"`
}

// Build is the pure half: two readings in, a report out.
func Build(old, cur Reading) *Report {
	span := cur.Sched.At.Sub(old.Sched.At).Seconds()
	r := &Report{WindowS: round(span, 1), Workers: cur.Sched.Workers,
		Protocols: map[string]ProtoReport{}}
	var busyNs, queueNs, started uint64
	protos := make([]string, 0, len(cur.Sched.Protocols))
	for p := range cur.Sched.Protocols {
		protos = append(protos, p)
	}
	sort.Strings(protos)
	for _, proto := range protos {
		c := cur.Sched.Protocols[proto]
		o := old.Sched.Protocols[proto]
		pr := ProtoReport{Jobs: c.Jobs, Limit: c.Limit,
			ScheduledPerS: round(c.Scheduled, 3),
			Shed:          sub(c.Shed, o.Shed), Overrun: sub(c.Overrun, o.Overrun),
			Late: sub(c.Late, o.Late)}
		done := sub(c.Completed, o.Completed)
		busy := sub(c.BusyNs, o.BusyNs)
		semw := sub(c.SemWaitNs, o.SemWaitNs)
		pts := sub(cur.PointsProt[proto], old.PointsProt[proto])
		if span > 0 {
			pr.PollsPerS = round(float64(done)/span, 3)
			pr.PointsPerS = round(float64(pts)/span, 2)
			if cur.Sched.Workers > 0 {
				pr.BusyPct = round(100*float64(busy)/(span*1e9*float64(cur.Sched.Workers)), 1)
			}
		}
		if done > 0 {
			pr.CostSPerPoll = round(float64(busy)/1e9/float64(done), 3)
		}
		if busy > 0 {
			pr.SemWaitPct = round(100*float64(semw)/float64(busy), 1)
		}
		r.Protocols[proto] = pr
		r.ScheduledPerS += c.Scheduled
		r.PollsPerS += pr.PollsPerS
		r.PointsPerS += pr.PointsPerS
		r.Shed += pr.Shed
		r.Overrun += pr.Overrun
		r.Late += pr.Late
		busyNs += busy
		queueNs += sub(c.QueueWaitNs, o.QueueWaitNs)
		started += done
	}
	r.ScheduledPerS = round(r.ScheduledPerS, 3)
	r.PollsPerS = round(r.PollsPerS, 3)
	r.PointsPerS = round(r.PointsPerS, 2)
	if span > 0 && cur.Sched.Workers > 0 {
		r.BusyPct = round(100*float64(busyNs)/(span*1e9*float64(cur.Sched.Workers)), 1)
	}
	if started > 0 {
		r.QueueWaitAvgMs = round(float64(queueNs)/1e6/float64(started), 1)
	}
	if span > 0 && len(cur.PointsPool) > 0 {
		r.Pools = map[string]PoolReport{}
		for pool, n := range cur.PointsPool {
			r.Pools[pool] = PoolReport{PointsPerS: round(float64(sub(n, old.PointsPool[pool]))/span, 2)}
		}
	}
	return r
}

// sub is a counter delta that cannot wrap: a counter that went backwards
// (it never should inside one process) reads as zero, not as 2^64.
func sub(a, b uint64) uint64 {
	if a < b {
		return 0
	}
	return a - b
}

func round(v float64, places int) float64 {
	p := 1.0
	for i := 0; i < places; i++ {
		p *= 10
	}
	if v < 0 {
		return -float64(int64(-v*p+0.5)) / p
	}
	return float64(int64(v*p+0.5)) / p
}
