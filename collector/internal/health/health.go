// Package health tracks per-endpoint communication state.
//
// The rules are asymmetric on purpose: slow to condemn, quick to forgive. One
// dropped UDP packet is normal, and declaring a device down on it produces an
// alarm storm every night. Recovery, by contrast, is immediate on the first
// success.
package health

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

type State struct {
	Status              models.CommStatus
	ConsecutiveFailures int
	LastSuccess         time.Time
	LastFailure         time.Time
	LastError           string
	LastErrorClass      string
	LastLatencyMs       int
	Protocol            string
	DeviceID            string
	Interval            time.Duration

	// Liveness. LastSeen is every poll ATTEMPT, where LastSuccess is only the
	// ones that worked; an endpoint that has been timing out for an hour is
	// still being seen, and telling those apart is the whole point of having
	// both columns.
	LastSeen      time.Time
	PollCount     uint64
	FailCount     uint64
	TimeoutCount  uint64
	AuthFailCount uint64

	// When this endpoint's state was last handed to the sink. Status changes
	// publish immediately; everything else is throttled to refreshInterval so
	// a 10 s BACnet poll does not write a row six times a minute.
	lastPublished time.Time
}

type Tracker struct {
	mu    sync.RWMutex
	state map[string]*State

	offlineThreshold int
	collectorID      string
	sink             models.Sink
	log              *slog.Logger
	mets             *obs.Metrics

	// Upper bound on how stale a persisted liveness row may get. Effective
	// freshness is max(pollInterval, refreshInterval): an endpoint polled
	// every 120 s cannot report more often than that.
	refreshInterval time.Duration

	// When the collector itself is unwell (publish shedding, assignment stale)
	// it must NOT condemn endpoints. "I cannot see it" is not "it is not there"
	// - conflating them turns a management-network blip into hundreds of false
	// alarms.
	selfDegraded bool
}

func NewTracker(offlineThreshold int, collectorID string, sink models.Sink,
	log *slog.Logger, mets *obs.Metrics, refreshInterval time.Duration) *Tracker {
	if offlineThreshold < 1 {
		offlineThreshold = 3
	}
	if refreshInterval <= 0 {
		refreshInterval = 60 * time.Second
	}
	return &Tracker{
		state:            make(map[string]*State),
		offlineThreshold: offlineThreshold,
		collectorID:      collectorID,
		sink:             sink,
		log:              log,
		mets:             mets,
		refreshInterval:  refreshInterval,
	}
}

func (t *Tracker) SetSelfDegraded(degraded bool) {
	t.mu.Lock()
	t.selfDegraded = degraded
	t.mu.Unlock()
}

func (t *Tracker) Register(ep *models.Endpoint) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if _, ok := t.state[ep.ID]; !ok {
		t.state[ep.ID] = &State{
			Status:   models.CommStatusUnknown,
			Protocol: ep.Protocol,
			DeviceID: ep.DeviceID,
			Interval: ep.Poll.Interval(),
		}
	}
}

func (t *Tracker) Forget(endpointID string) {
	t.mu.Lock()
	delete(t.state, endpointID)
	t.mu.Unlock()
}

// Success records a successful poll and returns true when the status changed.
func (t *Tracker) Success(ep *models.Endpoint, latencyMs int) bool {
	now := time.Now().UTC()

	t.mu.Lock()
	st := t.get(ep)
	previous := st.Status
	st.Status = models.CommStatusOnline
	st.ConsecutiveFailures = 0
	st.LastSuccess = now
	st.LastSeen = now
	st.PollCount++
	st.LastError = ""
	st.LastErrorClass = ""
	st.LastLatencyMs = latencyMs
	changed := previous != st.Status
	refresh := !changed && now.Sub(st.lastPublished) >= t.refreshInterval
	if changed || refresh {
		st.lastPublished = now
	}
	snapshot := *st
	t.mu.Unlock()

	switch {
	case changed:
		t.publish(ep, &snapshot, false)
		t.log.Info("endpoint online", "endpoint_id", ep.ID,
			"device", ep.DeviceName, "protocol", ep.Protocol,
			"previous", previous.String())
	case refresh:
		// Same status, but the stored row would otherwise age indefinitely.
		t.publish(ep, &snapshot, true)
	}
	return changed
}

// Failure records a failed poll. One failure is DEGRADED, never OFFLINE.
func (t *Tracker) Failure(ep *models.Endpoint, err error) bool {
	class := models.ClassifyError(err)

	now := time.Now().UTC()

	t.mu.Lock()
	st := t.get(ep)
	previous := st.Status
	st.ConsecutiveFailures++
	st.LastFailure = now
	st.LastSeen = now
	st.PollCount++
	st.FailCount++
	switch class {
	case models.ErrClassTimeout:
		st.TimeoutCount++
	case models.ErrClassAuth:
		st.AuthFailCount++
	}
	st.LastError = err.Error()
	st.LastErrorClass = class
	selfDegraded := t.selfDegraded

	switch {
	case selfDegraded:
		// Do not condemn while we are the sick one.
		st.Status = models.CommStatusUnknown
	case st.ConsecutiveFailures >= t.offlineThreshold &&
		time.Since(st.LastSuccess) > 2*st.Interval:
		// Both conditions: a long interval with two quick failures is not
		// enough evidence to call a device down.
		st.Status = models.CommStatusOffline
	default:
		st.Status = models.CommStatusDegraded
	}
	changed := previous != st.Status
	refresh := !changed && now.Sub(st.lastPublished) >= t.refreshInterval
	if changed || refresh {
		st.lastPublished = now
	}
	snapshot := *st
	t.mu.Unlock()

	t.mets.FailuresTotal.WithLabelValues(ep.Protocol, class).Inc()

	switch {
	case changed:
		t.publish(ep, &snapshot, false)
		level := slog.LevelWarn
		if snapshot.Status == models.CommStatusOffline {
			level = slog.LevelError
		}
		t.log.Log(context.Background(), level, "endpoint status changed",
			"endpoint_id", ep.ID, "device", ep.DeviceName,
			"protocol", ep.Protocol, "status", snapshot.Status.String(),
			"previous", previous.String(), "error_class", class,
			"consecutive_failures", snapshot.ConsecutiveFailures)
	case refresh:
		// Still failing the same way. The row still needs to say "seen".
		t.publish(ep, &snapshot, true)
	}
	return changed
}

func (t *Tracker) get(ep *models.Endpoint) *State {
	st, ok := t.state[ep.ID]
	if !ok {
		st = &State{Status: models.CommStatusUnknown, Protocol: ep.Protocol,
			DeviceID: ep.DeviceID, Interval: ep.Poll.Interval()}
		t.state[ep.ID] = st
	}
	if st.Interval == 0 {
		st.Interval = ep.Poll.Interval()
	}
	return st
}

func (t *Tracker) publish(ep *models.Endpoint, st *State, isRefresh bool) {
	msg := models.EndpointState{
		EndpointID:          ep.ID,
		DeviceID:            ep.DeviceID,
		CollectorID:         t.collectorID,
		Status:              st.Status,
		ConsecutiveFailures: uint32(st.ConsecutiveFailures),
		LastError:           st.LastError,
		LastErrorClass:      st.LastErrorClass,
		LatencyMs:           uint32(st.LastLatencyMs),
		ChangedAt:           models.NowMicros(),
		IsRefresh:           isRefresh,
		PollCount:           st.PollCount,
		FailCount:           st.FailCount,
		TimeoutCount:        st.TimeoutCount,
		AuthFailCount:       st.AuthFailCount,
	}
	if !st.LastSeen.IsZero() {
		msg.LastSeen = st.LastSeen.UnixMicro()
	}
	if !st.LastSuccess.IsZero() {
		msg.LastSuccess = st.LastSuccess.UnixMicro()
	}
	if !st.LastFailure.IsZero() {
		msg.LastFailure = st.LastFailure.UnixMicro()
	}
	// Best effort: a fan-out failure must not fail a poll.
	if err := t.sink.EndpointState(context.Background(), msg); err != nil {
		t.log.Warn("endpoint state publish failed", "error", err, "endpoint_id", ep.ID)
	}
}

// Counts returns endpoints per (protocol, status) for the gauge and heartbeat.
func (t *Tracker) Counts() map[string]map[string]int {
	t.mu.RLock()
	defer t.mu.RUnlock()
	out := map[string]map[string]int{}
	for _, st := range t.state {
		byStatus, ok := out[st.Protocol]
		if !ok {
			byStatus = map[string]int{}
			out[st.Protocol] = byStatus
		}
		byStatus[st.Status.String()]++
	}
	return out
}

func (t *Tracker) OnlineCount() int {
	t.mu.RLock()
	defer t.mu.RUnlock()
	n := 0
	for _, st := range t.state {
		if st.Status == models.CommStatusOnline {
			n++
		}
	}
	return n
}
