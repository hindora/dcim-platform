package app

import (
	"testing"

	"github.com/hari/dcim-platform/collector/internal/adapters/gnmi"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// The heartbeat's two endpoint counts must describe the SAME set.
//
// `online` comes from the health tracker, which counts every endpoint it has
// seen - polled and streamed alike. `owned` used to come from the scheduler
// alone, and stream-only gNMI endpoints are deliberately kept out of the
// scheduler because polling them as well would collect the same device twice.
//
// So owned excluded a population online included, and the collector reported
// 1344 online out of 1340 owned. Nothing was wrong with the collector: the
// numbers were counting different things, and the platform monitor - correctly
// - refused to trust either, raising a `collector_degraded` that stood
// permanently and that no operator could do anything about.
//
// An alarm nobody can action is the one failure an alarm system may not have,
// so the invariant is worth pinning: online can be below owned, which is a
// coverage gap and worth saying, and can never be above it.

func streamEndpoint(id string) *models.Endpoint {
	ep := &models.Endpoint{ID: id, Protocol: "gnmi"}
	ep.Poll.PushEnabled = true
	ep.Poll.IntervalS = 0
	return ep
}

func polledEndpoint(id string) *models.Endpoint {
	ep := &models.Endpoint{ID: id, Protocol: "snmp"}
	ep.Poll.IntervalS = 60
	return ep
}

func TestStreamOnlyEndpointsAreOwnedButNotScheduled(t *testing.T) {
	// The classification the count depends on. If this ever stops being true,
	// owned and online drift apart again and the alarm comes back.
	if !gnmi.StreamOnly(streamEndpoint("ep-1")) {
		t.Fatal("a zero-interval push endpoint should be stream-only")
	}
	if gnmi.StreamOnly(polledEndpoint("ep-2")) {
		t.Fatal("a 60s SNMP endpoint should not be stream-only")
	}
}

func TestStreamCountIsZeroWithoutASubscriber(t *testing.T) {
	// A build with gNMI streaming disabled must report the scheduler alone,
	// not a phantom population.
	a := &App{}
	if got := a.streamCount(); got != 0 {
		t.Fatalf("streamCount without a subscriber = %d, want 0", got)
	}
}

func TestStreamCountFollowsTheSubscriber(t *testing.T) {
	subs := gnmi.NewSubscriber(nil, nil, nil, nil, nil, nil, nil, 3)
	a := &App{gnmiSubs: subs}
	// No sessions started, so the two agree at zero - the point is that the
	// count is read from the subscriber rather than assumed.
	if got, want := a.streamCount(), subs.Sessions(); got != want {
		t.Fatalf("streamCount = %d, subscriber reports %d", got, want)
	}
}
