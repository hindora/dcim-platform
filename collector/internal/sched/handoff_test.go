package sched

import (
	"fmt"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// A handed-over endpoint's first poll must land inside the short window, not
// anywhere in its interval: the live failover recovered polling at 4.7 min
// where the record moved at 3 min, the gap being 120 s SNMP intervals.
func TestAddSoonPullsTheFirstPollIntoTheWindow(t *testing.T) {
	s := New(Options{Workers: 1}, nil, quietLog(), obs.NewMetrics())
	before := time.Now()
	for i := 0; i < 200; i++ {
		ep := &models.Endpoint{ID: fmt.Sprintf("ep-%d", i), Protocol: "snmp",
			Poll: models.PollProfile{IntervalS: 600}}
		s.AddSoon(ep, 15*time.Second)
	}
	late := 0
	for _, j := range s.jobs {
		if j.nextRun.Sub(before) > 15*time.Second {
			late++
		}
		if j.Interval != 600*time.Second {
			t.Fatalf("interval = %v, want the profile's 600 s for every later poll", j.Interval)
		}
	}
	if late != 0 {
		t.Fatalf("%d of 200 first polls land after the 15 s window", late)
	}
}

func TestAddStillSpreadsAcrossTheWholeInterval(t *testing.T) {
	// The start-up path: a restart must not poll everything in one burst.
	s := New(Options{Workers: 1}, nil, quietLog(), obs.NewMetrics())
	before := time.Now()
	for i := 0; i < 200; i++ {
		s.Add(&models.Endpoint{ID: fmt.Sprintf("ep-%d", i), Protocol: "snmp",
			Poll: models.PollProfile{IntervalS: 600}})
	}
	beyond := 0
	for _, j := range s.jobs {
		if j.nextRun.Sub(before) > 60*time.Second {
			beyond++
		}
	}
	if beyond < 150 {
		t.Fatalf("only %d of 200 first polls beyond 60 s - Add is no longer spreading", beyond)
	}
}

func TestAWindowWiderThanTheIntervalIsTheInterval(t *testing.T) {
	s := New(Options{Workers: 1}, nil, quietLog(), obs.NewMetrics())
	before := time.Now()
	s.AddSoon(&models.Endpoint{ID: "fast", Protocol: "bacnet",
		Poll: models.PollProfile{IntervalS: 10}}, 60*time.Second)
	if d := s.jobs["fast"].nextRun.Sub(before); d > 10*time.Second {
		t.Fatalf("first poll in %v, beyond its own 10 s interval", d)
	}
}

func TestARestartKeepsEveryEndpointInTheSlotItHad(t *testing.T) {
	interval := 120 * time.Second
	offset := phaseOffset("ep-1", interval)
	at := time.Date(2026, 10, 1, 12, 0, 7, 0, time.UTC)
	before := alignedSlot(at, interval, offset)
	// Restarted 40 s later, and again 3 min later: the same slot on the
	// clock each time, only further along.
	for _, later := range []time.Duration{40 * time.Second, 3 * time.Minute} {
		got := alignedSlot(at.Add(later), interval, offset)
		if d := got.Sub(before) % interval; d != 0 {
			t.Fatalf("after %v the slot moved by %v within the interval", later, d)
		}
		if got.Before(at.Add(later)) || got.Sub(at.Add(later)) >= interval {
			t.Fatalf("after %v the next slot %v is not within one interval", later, got)
		}
	}
}
