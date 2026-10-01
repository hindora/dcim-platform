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
