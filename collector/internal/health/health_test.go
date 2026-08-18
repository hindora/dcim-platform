package health

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

type fakeSink struct {
	mu     sync.Mutex
	states []models.EndpointState
}

func (f *fakeSink) Telemetry(context.Context, []models.Telemetry) error { return nil }
func (f *fakeSink) Events(context.Context, []models.Event) error        { return nil }
func (f *fakeSink) EndpointState(_ context.Context, s models.EndpointState) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.states = append(f.states, s)
	return nil
}
func (f *fakeSink) published() []models.EndpointState {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]models.EndpointState(nil), f.states...)
}

func newTracker(t *testing.T, threshold int) (*Tracker, *fakeSink) {
	t.Helper()
	sink := &fakeSink{}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	return NewTracker(threshold, "col-test", sink, log, obs.NewMetrics()), sink
}

func endpoint() *models.Endpoint {
	return &models.Endpoint{
		ID: "ep-1", DeviceID: "dev-1", DeviceName: "SRV01", Protocol: "snmp",
		Poll: models.PollProfile{IntervalS: 30},
	}
}

func TestSingleFailureIsDegradedNotOffline(t *testing.T) {
	// One dropped UDP packet is normal. Condemning on it produces an alarm
	// storm every night.
	tr, _ := newTracker(t, 3)
	ep := endpoint()

	tr.Failure(ep, errors.New("timeout"))

	tr.mu.RLock()
	got := tr.state[ep.ID].Status
	tr.mu.RUnlock()
	if got != models.CommStatusDegraded {
		t.Fatalf("one failure gave %v, want DEGRADED", got)
	}
}

func TestOfflineNeedsThresholdFailures(t *testing.T) {
	tr, _ := newTracker(t, 3)
	ep := endpoint()

	for i := 0; i < 2; i++ {
		tr.Failure(ep, errors.New("timeout"))
	}
	tr.mu.RLock()
	afterTwo := tr.state[ep.ID].Status
	tr.mu.RUnlock()
	if afterTwo == models.CommStatusOffline {
		t.Fatal("went OFFLINE after two failures with a threshold of three")
	}

	tr.Failure(ep, errors.New("timeout"))
	tr.mu.RLock()
	afterThree := tr.state[ep.ID].Status
	tr.mu.RUnlock()
	if afterThree != models.CommStatusOffline {
		t.Fatalf("after three failures got %v, want OFFLINE", afterThree)
	}
}

func TestRecoveryIsImmediateOnFirstSuccess(t *testing.T) {
	// Asymmetric on purpose: slow to condemn, quick to forgive.
	tr, _ := newTracker(t, 3)
	ep := endpoint()
	for i := 0; i < 5; i++ {
		tr.Failure(ep, errors.New("timeout"))
	}

	changed := tr.Success(ep, 12)
	if !changed {
		t.Fatal("recovery should report a status change")
	}
	tr.mu.RLock()
	st := *tr.state[ep.ID]
	tr.mu.RUnlock()
	if st.Status != models.CommStatusOnline {
		t.Fatalf("got %v after success, want ONLINE", st.Status)
	}
	if st.ConsecutiveFailures != 0 {
		t.Fatalf("failure counter not reset: %d", st.ConsecutiveFailures)
	}
}

func TestStatePublishedOnChangeOnly(t *testing.T) {
	// Publishing every poll would drown the stream in noise and defeat the
	// point of a separate state channel.
	tr, sink := newTracker(t, 3)
	ep := endpoint()

	tr.Success(ep, 5) // UNKNOWN -> ONLINE, one publish
	tr.Success(ep, 5)
	tr.Success(ep, 5)

	if n := len(sink.published()); n != 1 {
		t.Fatalf("three successes published %d state messages, want 1", n)
	}
}

func TestSelfDegradedCollectorDoesNotCondemnEndpoints(t *testing.T) {
	// "I cannot see it" is not "it is not there". Conflating them turns a
	// management-network blip into hundreds of false alarms.
	tr, _ := newTracker(t, 1)
	ep := endpoint()
	tr.SetSelfDegraded(true)

	for i := 0; i < 5; i++ {
		tr.Failure(ep, errors.New("timeout"))
	}

	tr.mu.RLock()
	got := tr.state[ep.ID].Status
	tr.mu.RUnlock()
	if got != models.CommStatusUnknown {
		t.Fatalf("self-degraded collector marked endpoint %v, want UNKNOWN", got)
	}
}

func TestErrorClassIsRecordedForAuthFailures(t *testing.T) {
	// Auth failures must never be retried like timeouts: on real hardware that
	// locks accounts.
	tr, sink := newTracker(t, 1)
	ep := endpoint()

	tr.Failure(ep, models.ErrAuth)

	states := sink.published()
	if len(states) == 0 {
		t.Fatal("no state published")
	}
	if states[len(states)-1].LastErrorClass != models.ErrClassAuth {
		t.Fatalf("error class %q, want %q",
			states[len(states)-1].LastErrorClass, models.ErrClassAuth)
	}
}

func TestCountsAndOnlineCount(t *testing.T) {
	tr, _ := newTracker(t, 3)
	a := endpoint()
	b := endpoint()
	b.ID = "ep-2"

	tr.Success(a, 1)
	tr.Failure(b, errors.New("timeout"))

	if got := tr.OnlineCount(); got != 1 {
		t.Fatalf("OnlineCount = %d, want 1", got)
	}
	counts := tr.Counts()["snmp"]
	if counts["ONLINE"] != 1 || counts["DEGRADED"] != 1 {
		t.Fatalf("counts = %v, want one ONLINE and one DEGRADED", counts)
	}
}

func TestForgetRemovesState(t *testing.T) {
	tr, _ := newTracker(t, 3)
	ep := endpoint()
	tr.Register(ep)
	tr.Forget(ep.ID)

	tr.mu.RLock()
	_, exists := tr.state[ep.ID]
	tr.mu.RUnlock()
	if exists {
		t.Fatal("state survived Forget; a decommissioned endpoint would leak")
	}
}

func TestIntervalDefaultsWhenProfileIsEmpty(t *testing.T) {
	tr, _ := newTracker(t, 3)
	ep := &models.Endpoint{ID: "ep-x", Protocol: "snmp"}
	tr.Register(ep)

	tr.mu.RLock()
	got := tr.state[ep.ID].Interval
	tr.mu.RUnlock()
	if got != 30*time.Second {
		t.Fatalf("interval = %v, want the 30s default", got)
	}
}
