package snmp

import (
	"sync"
	"time"
)

// timeWindow is USM's timeliness window (RFC 3414 2.2.3): a message more than
// 150 seconds away from the receiver's notion of the sender's clock is a
// replay, or a clock nobody should trust.
const timeWindow = 150

// maxBoots is snmpEngineBoots' latch value: an engine that reaches it can no
// longer be trusted to be monotonic, and every message from it is outside the
// window (RFC 3414 2.2.2).
const maxBoots = 2147483647

// EngineTimes is a receiver's notion of each sending engine's clock - the
// RFC 3414 3.2.7b check a non-authoritative receiver makes on a v3 TRAP,
// where the device's engine is authoritative.
//
// Without it, gosnmp accepts any authentic message whatever its time, so a
// TRAP captured once could be replayed for ever and raise its alarm again
// (and its ticket, where a policy opens one).
//
// An engine is learned from its first authentic message - trust on first
// use. net-snmp's snmptrapd instead expects each sender's engine ID to be
// configured in advance, which a DCIM with thousands of devices cannot ask
// for.
//
// The table outlives the process (Export/Import, saved by the app to the
// state dir). In memory only, a restart made the first authentic message from
// each engine the new baseline, so a TRAP captured at any time in the past
// could be replayed once per engine per restart. net-snmp's snmptrapd keeps
// this cache in memory too; persisting it is stricter than common practice.
// Learned times are wall-clock, so the notion of a sender's clock keeps
// advancing while the collector is down.
type EngineTimes struct {
	mu      sync.Mutex
	engines map[string]engineClock
	dirty   bool
}

type engineClock struct {
	boots   uint32
	time    uint32    // snmpEngineTime when last advanced
	latest  uint32    // latestReceivedEngineTime
	learned time.Time // local time of that advance
}

func NewEngineTimes() *EngineTimes {
	return &EngineTimes{engines: make(map[string]engineClock)}
}

// Accept applies RFC 3414 3.2.7b to an AUTHENTIC message from engineID:
// advance the notion of that engine's clock if the message is newer, then
// report whether the message falls inside the window. Call it only after
// the message has been authenticated, or a forger could move the clock.
func (e *EngineTimes) Accept(engineID string, boots, engineTime uint32, now time.Time) bool {
	if boots >= maxBoots {
		return false
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	c, known := e.engines[engineID]
	if !known || boots > c.boots || (boots == c.boots && engineTime > c.latest) {
		c = engineClock{boots: boots, time: engineTime, latest: engineTime, learned: now}
		e.engines[engineID] = c
		e.dirty = true
	}
	if boots < c.boots {
		return false
	}
	// A clock stepped backwards (or a learned time restored from a host whose
	// clock ran ahead) would make the elapsed time negative and the window
	// stricter than the sender deserves; count it as no time passed.
	elapsed := now.Sub(c.learned)
	if elapsed < 0 {
		elapsed = 0
	}
	local := int64(c.time) + int64(elapsed/time.Second)
	return int64(engineTime) >= local-timeWindow
}

// EngineClockState is one engine's clock as stored: what Export writes and
// Import reads. Learned is wall-clock.
type EngineClockState struct {
	Boots   uint32    `json:"boots"`
	Time    uint32    `json:"time"`
	Latest  uint32    `json:"latest"`
	Learned time.Time `json:"learned"`
}

// Export returns every engine's clock, keyed by engine ID, and clears the
// dirty mark: the caller is saving it.
func (e *EngineTimes) Export() map[string]EngineClockState {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := make(map[string]EngineClockState, len(e.engines))
	for id, c := range e.engines {
		out[id] = EngineClockState{Boots: c.boots, Time: c.time, Latest: c.latest,
			Learned: c.learned.Round(0)}
	}
	e.dirty = false
	return out
}

// Import restores saved clocks. An engine already learned in this process
// keeps whichever notion is newer, so a late Import never moves a clock back.
func (e *EngineTimes) Import(saved map[string]EngineClockState) {
	e.mu.Lock()
	defer e.mu.Unlock()
	for id, st := range saved {
		c, known := e.engines[id]
		if known && (c.boots > st.Boots || (c.boots == st.Boots && c.latest >= st.Latest)) {
			continue
		}
		e.engines[id] = engineClock{boots: st.Boots, time: st.Time, latest: st.Latest,
			learned: st.Learned.Round(0)}
	}
}

// Dirty is whether anything changed since the last Export.
func (e *EngineTimes) Dirty() bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.dirty
}

// Forget drops one engine, so its next authentic message is trusted as a new
// baseline. The way out for a device whose boots went back - a factory reset
// that kept its engine ID - which RFC 3414 otherwise refuses until its boots
// catch up. Reports whether the engine was known.
func (e *EngineTimes) Forget(engineID string) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	_, known := e.engines[engineID]
	delete(e.engines, engineID)
	if known {
		e.dirty = true
	}
	return known
}

// Len is how many engines have been seen.
func (e *EngineTimes) Len() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return len(e.engines)
}

func absDiff(x, y uint32) uint32 {
	if x > y {
		return x - y
	}
	return y - x
}
