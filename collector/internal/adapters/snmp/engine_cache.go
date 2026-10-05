package snmp

import (
	"strings"
	"sync"
	"time"

	g "github.com/gosnmp/gosnmp"
)

// engineCache remembers each SNMPv3 agent's authoritative engine ID, boots
// and time, so a poll does not open with a discovery round trip.
//
// Without it every v3 poll costs two exchanges: an empty noAuthNoPriv GET
// that only exists to learn the engine parameters, then the real request.
// net-snmp, and every commercial poller built on it, keeps these per engine
// and advances the time locally (RFC 3414 2.3: a non-authoritative engine
// keeps its own notion of the authoritative engine's time). The agent only
// has to be asked again when it says the cached values are wrong:
//
//   - it rebooted: snmpEngineBoots moved, the agent answers
//     usmStatsNotInTimeWindows with its new boots and time;
//   - the card was swapped: a new engine ID, the agent answers
//     usmStatsUnknownEngineIDs with it.
//
// gosnmp handles both reports itself - it stores the agent's values and
// retransmits within the same request - so a stale entry costs one extra
// round trip, the same as discovery would have. Agents that do not report
// cleanly (some answer a stale engine ID with a wrong-digest report instead)
// are caught by firstGet's fallback.
//
// Keyed by agent address and port, not endpoint: probe endpoints share
// their PDU's agent and so its engine.
type engineCache struct {
	mu      sync.Mutex
	entries map[string]engineEntry
}

type engineEntry struct {
	id      string
	boots   uint32
	time    uint32
	learned time.Time
}

func newEngineCache() *engineCache {
	return &engineCache{entries: make(map[string]engineEntry)}
}

// apply fills a session's security parameters from the cache, advancing the
// agent's time by what has elapsed locally since it was learned. It reports
// whether there was an entry to use.
func (c *engineCache) apply(key string, sp *g.UsmSecurityParameters, now time.Time) bool {
	c.mu.Lock()
	e, ok := c.entries[key]
	c.mu.Unlock()
	if !ok || e.id == "" {
		return false
	}
	elapsed := now.Sub(e.learned)
	if elapsed < 0 {
		elapsed = 0
	}
	sp.AuthoritativeEngineID = e.id
	sp.AuthoritativeEngineBoots = e.boots
	sp.AuthoritativeEngineTime = e.time + uint32(elapsed/time.Second)
	return true
}

// learn records what a session ended up with. gosnmp stores the agent's
// boots and time from every response, so after a request the session holds
// the agent's current values. Returns whether the engine differs from the
// cached one (a reboot or a swapped card), and false for a first sighting.
func (c *engineCache) learn(key string, sp *g.UsmSecurityParameters, now time.Time) (changed bool) {
	if sp == nil || sp.AuthoritativeEngineID == "" {
		return false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	prev, seen := c.entries[key]
	c.entries[key] = engineEntry{id: sp.AuthoritativeEngineID,
		boots: sp.AuthoritativeEngineBoots, time: sp.AuthoritativeEngineTime, learned: now}
	return seen && (prev.id != sp.AuthoritativeEngineID || prev.boots != sp.AuthoritativeEngineBoots)
}

func (c *engineCache) forget(key string) {
	c.mu.Lock()
	delete(c.entries, key)
	c.mu.Unlock()
}

// isRequestTimeout reports whether err is gosnmp's "nobody answered". gosnmp
// returns it as a plain string error, so this matches on the text.
func isRequestTimeout(err error) bool {
	return err != nil && strings.Contains(err.Error(), "timeout")
}
