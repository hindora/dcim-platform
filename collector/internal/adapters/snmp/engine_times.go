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
// for. The state is in memory: after a collector restart, the first authentic
// message from each engine is accepted and sets the baseline again.
type EngineTimes struct {
	mu      sync.Mutex
	engines map[string]engineClock
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
	}
	if boots < c.boots {
		return false
	}
	local := int64(c.time) + int64(now.Sub(c.learned)/time.Second)
	return int64(engineTime) >= local-timeWindow
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
