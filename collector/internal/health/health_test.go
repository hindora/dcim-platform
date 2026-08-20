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
	return NewTracker(threshold, "col-test", sink, log, obs.NewMetrics(), 0), sink
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

func newTrackerRefresh(t *testing.T, refresh time.Duration) (*Tracker, *fakeSink) {
	t.Helper()
	sink := &fakeSink{}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	return NewTracker(3, "col-test", sink, log, obs.NewMetrics(), refresh), sink
}

// Steady-state polling must keep refreshing the stored row. With change-only
// publishing, 894 healthy endpoints sat at last_success eleven hours old while
// polling perfectly - the UI would have shown a live fleet as long dead.
func TestSteadyStateRepublishesLivenessAsRefresh(t *testing.T) {
	tr, sink := newTrackerRefresh(t, 50*time.Millisecond)
	ep := endpoint()

	tr.Success(ep, 5) // first success: a real transition, UNKNOWN -> ONLINE
	if got := len(sink.published()); got != 1 {
		t.Fatalf("first success published %d messages, want 1", got)
	}
	if sink.published()[0].IsRefresh {
		t.Error("the initial transition was marked as a refresh")
	}

	// Inside the interval: no further writes, or a 10 s BACnet poll would
	// write the same row six times a minute.
	tr.Success(ep, 5)
	if got := len(sink.published()); got != 1 {
		t.Fatalf("published %d messages within the refresh interval, want 1", got)
	}

	time.Sleep(60 * time.Millisecond)
	tr.Success(ep, 7)

	msgs := sink.published()
	if len(msgs) != 2 {
		t.Fatalf("published %d messages after the interval elapsed, want 2", len(msgs))
	}
	last := msgs[1]
	if !last.IsRefresh {
		t.Error("the periodic republish is not marked is_refresh; the worker " +
			"would treat it as a transition and re-run alarms and fanout")
	}
	if last.Status != models.CommStatusOnline {
		t.Errorf("refresh carries status %v, want ONLINE", last.Status)
	}
	if last.LastSeen == 0 || last.LastSuccess == 0 {
		t.Errorf("refresh carries last_seen=%d last_success=%d, want both set",
			last.LastSeen, last.LastSuccess)
	}
	if last.PollCount != 3 {
		t.Errorf("poll_count = %d after three polls, want 3", last.PollCount)
	}
}

// last_seen counts attempts and last_success only the ones that worked. An
// endpoint timing out for an hour is still being seen, and the two columns
// exist precisely to tell those apart.
func TestFailureCountersSeparateSeenFromSucceeded(t *testing.T) {
	tr, sink := newTrackerRefresh(t, time.Hour) // never refresh; only transitions
	ep := endpoint()

	tr.Success(ep, 3)
	// The sentinel, not a message that happens to read "timeout":
	// ClassifyError matches on errors.Is, so a bare string is class "".
	tr.Failure(ep, models.ErrTimeout)

	msgs := sink.published()
	last := msgs[len(msgs)-1]
	if last.PollCount != 2 {
		t.Errorf("poll_count = %d, want 2 (one success, one failure)", last.PollCount)
	}
	if last.FailCount != 1 {
		t.Errorf("fail_count = %d, want 1", last.FailCount)
	}
	if last.TimeoutCount != 1 {
		t.Errorf("timeout_count = %d, want 1 for a classified timeout", last.TimeoutCount)
	}
	if last.AuthFailCount != 0 {
		t.Errorf("auth_fail_count = %d, want 0", last.AuthFailCount)
	}
	if last.LastSuccess == 0 {
		t.Error("last_success was cleared by a later failure; it must hold the " +
			"last time the endpoint actually answered")
	}
	if last.LastSeen < last.LastSuccess {
		t.Error("last_seen is older than last_success; the failed attempt " +
			"should still count as having seen the endpoint")
	}
}

// A status change must never be throttled, however recently we refreshed.
func TestTransitionPublishesEvenInsideTheRefreshWindow(t *testing.T) {
	tr, sink := newTrackerRefresh(t, time.Hour)
	ep := endpoint()

	tr.Success(ep, 1)
	before := len(sink.published())
	tr.Failure(ep, errors.New("timeout")) // ONLINE -> DEGRADED

	msgs := sink.published()
	if len(msgs) != before+1 {
		t.Fatalf("a transition inside the refresh window published %d messages, want %d",
			len(msgs)-before, 1)
	}
	if msgs[len(msgs)-1].IsRefresh {
		t.Error("a real status change was marked is_refresh")
	}
}
