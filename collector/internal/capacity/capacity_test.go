package capacity

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/sched"
)

var t0 = time.Date(2026, 9, 30, 12, 0, 0, 0, time.UTC)

func reading(at time.Duration, workers int, protos map[string]sched.ProtoCounters,
	pts map[string]uint64, pools map[string]uint64) Reading {
	return Reading{Sched: sched.Snapshot{At: t0.Add(at), Workers: workers, Protocols: protos},
		PointsProt: pts, PointsPool: pools}
}

func TestBuildTurnsCountersIntoRatesOverTheWindow(t *testing.T) {
	old := reading(0, 4, map[string]sched.ProtoCounters{
		"snmp": {Completed: 100, BusyNs: 10e9, SemWaitNs: 1e9},
	}, map[string]uint64{"snmp": 1000}, map[string]uint64{"p1": 400})
	// 100 s later: 200 more snmp polls costing 240 worker-seconds, 60 of
	// them waiting for a slot; 3 shed; 4000 more points, 1500 in pool p1.
	cur := reading(100*time.Second, 4, map[string]sched.ProtoCounters{
		"snmp": {Jobs: 50, Limit: 8, Scheduled: 2.5, Completed: 300,
			BusyNs: 250e9, SemWaitNs: 61e9, Shed: 3, QueueWaitNs: 400e6},
	}, map[string]uint64{"snmp": 5000}, map[string]uint64{"p1": 1900})

	r := Build(old, cur)
	if r.WindowS != 100 {
		t.Fatalf("window = %v, want 100", r.WindowS)
	}
	// 240 worker-seconds of 400 available (100 s x 4 workers).
	if r.BusyPct != 60 {
		t.Fatalf("busy = %v%%, want 60", r.BusyPct)
	}
	p := r.Protocols["snmp"]
	if p.PollsPerS != 2 || p.PointsPerS != 40 || p.CostSPerPoll != 1.2 {
		t.Fatalf("polls/s, points/s, cost = %v, %v, %v; want 2, 40, 1.2",
			p.PollsPerS, p.PointsPerS, p.CostSPerPoll)
	}
	if p.SemWaitPct != 25 {
		t.Fatalf("slot wait = %v%%, want 25 (60 of 240 worker-seconds)", p.SemWaitPct)
	}
	if r.Shed != 3 || r.ScheduledPerS != 2.5 || p.Limit != 8 || p.Jobs != 50 {
		t.Fatalf("shed/scheduled/limit/jobs = %d/%v/%d/%d", r.Shed, r.ScheduledPerS, p.Limit, p.Jobs)
	}
	if r.QueueWaitAvgMs != 2 {
		t.Fatalf("queue wait avg = %vms, want 2 (400ms over 200 polls)", r.QueueWaitAvgMs)
	}
	if r.Pools["p1"].PointsPerS != 15 {
		t.Fatalf("pool p1 = %v points/s, want 15", r.Pools["p1"].PointsPerS)
	}
}

func TestAProtocolNewInTheWindowCountsFromZero(t *testing.T) {
	old := reading(0, 2, map[string]sched.ProtoCounters{}, nil, nil)
	cur := reading(10*time.Second, 2, map[string]sched.ProtoCounters{
		"bacnet": {Completed: 10, BusyNs: 5e9},
	}, map[string]uint64{"bacnet": 100}, nil)
	p := Build(old, cur).Protocols["bacnet"]
	if p.PollsPerS != 1 || p.PointsPerS != 10 || p.BusyPct != 25 {
		t.Fatalf("got %+v", p)
	}
}

func TestACounterThatWentBackwardsReadsAsZeroNotAWrap(t *testing.T) {
	old := reading(0, 1, map[string]sched.ProtoCounters{"snmp": {Completed: 50, BusyNs: 9e9}}, nil, nil)
	cur := reading(10*time.Second, 1, map[string]sched.ProtoCounters{"snmp": {Completed: 10, BusyNs: 1e9}}, nil, nil)
	r := Build(old, cur)
	if r.BusyPct != 0 || r.PollsPerS != 0 {
		t.Fatalf("busy %v, polls/s %v; want 0, 0", r.BusyPct, r.PollsPerS)
	}
}

func TestTrackerReportsNothingFromOneReadingAndTrimsToTheWindow(t *testing.T) {
	var tr Tracker
	mk := func(at time.Duration, done uint64) Reading {
		return reading(at, 1, map[string]sched.ProtoCounters{"snmp": {Completed: done}}, nil, nil)
	}
	if r := tr.Observe(mk(0, 0)); r != nil {
		t.Fatalf("one reading gave a report: %+v", r)
	}
	// A reading every minute for ten minutes, one poll a second throughout.
	var r *Report
	for m := 1; m <= 10; m++ {
		r = tr.Observe(mk(time.Duration(m)*time.Minute, uint64(m*60)))
	}
	if r.WindowS != Window.Seconds() {
		t.Fatalf("window = %vs, want the trailing %v", r.WindowS, Window)
	}
	if r.PollsPerS != 1 {
		t.Fatalf("polls/s = %v, want 1", r.PollsPerS)
	}
	if len(tr.ring) != 6 {
		t.Fatalf("ring holds %d readings, want 6 (the window plus its opening edge)", len(tr.ring))
	}
}

// The platform reads these exact keys (backend app/alarms/platform_monitor.py
// _capacity_fields, repositories/pools.py pool_points and members). A rename
// here silently disarms collector_capacity_high and pool_rate_budget_exceeded.
func TestReportJSONCarriesTheKeysThePlatformReads(t *testing.T) {
	old := reading(0, 2, map[string]sched.ProtoCounters{}, nil, nil)
	cur := reading(60*time.Second, 2, map[string]sched.ProtoCounters{
		"snmp": {Completed: 6, BusyNs: 12e9, Shed: 1, Late: 2},
	}, map[string]uint64{"snmp": 60}, map[string]uint64{"pool-a": 30})
	raw, err := json.Marshal(Build(old, cur))
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"window_s", "busy_pct", "shed", "late", "workers",
		"polls_per_s", "points_per_s", "scheduled_polls_per_s", "protocols", "pools"} {
		if _, ok := got[k]; !ok {
			t.Errorf("report JSON lacks %q: %s", k, raw)
		}
	}
	pools := got["pools"].(map[string]any)
	if pools["pool-a"].(map[string]any)["points_per_s"] != 0.5 {
		t.Errorf("pools.pool-a.points_per_s = %v, want 0.5", pools["pool-a"])
	}
}
