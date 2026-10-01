// Package throttle holds the two limits a facility puts on how hard a DCIM
// poller may drive its equipment (docs/26 Phases 5 and 9), enforced rather
// than only measured.
//
// Per TARGET: the address a request actually lands on. A Modbus serial
// gateway forwards one RS-485 transaction at a time for every slave behind
// it (Moxa MGate), a BACnet router fronts a whole MS/TP trunk, and a PDU or
// UPS network card answers SNMP and Modbus on one small CPU - so the key is
// the address, shared by every endpoint and every protocol that reaches it.
// Two knobs, as Kepware exposes them per device: how many polls may be in
// flight at once, and the minimum gap between one poll starting and the
// next (its "inter-request delay").
//
// Per POOL: the points per second the facility agreed this poller may put
// on its network - a Niagara-style supervisor budget. Each collector
// enforces its SHARE, which the platform computes from what it owns, with a
// token bucket settled on the points a poll actually returned: admission
// needs a positive balance, and the real cost is charged afterwards. No
// estimate of what a poll will cost is needed, and a big walk leaves a debt
// that holds the next polls back, which is exactly the point.
//
// Nothing here blocks. A poll the limits will not admit is deferred to a
// later tick, which stretches the cycle the way Niagara's poll scheduler
// does under load, and is counted - instead of holding a worker on a
// semaphore while the queue grows behind it.
package throttle

import (
	"sync"
	"time"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

// Reason says which limit deferred a poll.
type Reason string

const (
	ReasonNone   Reason = ""
	ReasonTarget Reason = "target"
	ReasonBudget Reason = "budget"
)

// BurstSeconds is how much of a pool's rate a quiet spell may bank. Five
// seconds lets the one-second wheel's per-tick batch through without
// letting a long lull be spent as one spike at the supervisor.
const BurstSeconds = 5.0

type limit struct {
	maxConcurrent int
	minGap        time.Duration
}

type target struct {
	inflight  int
	nextStart time.Time
}

type bucket struct {
	rate   float64
	tokens float64
	last   time.Time
}

func (b *bucket) refill(now time.Time) {
	if !b.last.IsZero() {
		b.tokens += b.rate * now.Sub(b.last).Seconds()
	}
	if max := b.rate * BurstSeconds; b.tokens > max {
		b.tokens = max
	}
	b.last = now
}

// Limiter is shared by every scheduler that reaches the same equipment: a
// liveness probe and a poll at one gateway are two requests to one device.
type Limiter struct {
	mu sync.Mutex
	// Per endpoint, the limit the platform resolved for it and its target.
	byEndpoint map[string]endpointLimit
	// Per target, the strictest limit any endpoint reaching it carries.
	limits  map[string]limit
	targets map[string]*target
	pools   map[string]*bucket
}

type endpointLimit struct {
	target string
	limit  limit
}

func New() *Limiter {
	return &Limiter{byEndpoint: map[string]endpointLimit{}, limits: map[string]limit{},
		targets: map[string]*target{}, pools: map[string]*bucket{}}
}

// Track records an endpoint's target limit (nil: none). Endpoints reaching
// one target may disagree; the strictest wins - the fewest in flight, the
// longest gap - because the device has one limit, whatever each row says.
func (l *Limiter) Track(ep *models.Endpoint) {
	if l == nil || ep == nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	old, had := l.byEndpoint[ep.ID]
	tl := ep.TargetLimit
	if ep.Address == "" || tl == nil || (tl.MaxConcurrent <= 0 && tl.MinIntervalMs <= 0) {
		delete(l.byEndpoint, ep.ID)
	} else {
		l.byEndpoint[ep.ID] = endpointLimit{target: ep.Address, limit: limit{
			maxConcurrent: tl.MaxConcurrent,
			minGap:        time.Duration(tl.MinIntervalMs) * time.Millisecond}}
	}
	if had {
		l.recompute(old.target)
	}
	if e, ok := l.byEndpoint[ep.ID]; ok && (!had || e.target != old.target) {
		l.recompute(e.target)
	}
}

// Forget drops an endpoint this collector no longer owns.
func (l *Limiter) Forget(endpointID string) {
	if l == nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if old, ok := l.byEndpoint[endpointID]; ok {
		delete(l.byEndpoint, endpointID)
		l.recompute(old.target)
	}
}

// recompute is O(endpoints): limits change on assignment, not per poll.
func (l *Limiter) recompute(key string) {
	var out limit
	found := false
	for _, e := range l.byEndpoint {
		if e.target != key {
			continue
		}
		found = true
		if e.limit.maxConcurrent > 0 &&
			(out.maxConcurrent == 0 || e.limit.maxConcurrent < out.maxConcurrent) {
			out.maxConcurrent = e.limit.maxConcurrent
		}
		if e.limit.minGap > out.minGap {
			out.minGap = e.limit.minGap
		}
	}
	if found {
		l.limits[key] = out
	} else {
		delete(l.limits, key)
	}
}

// SetBudgets replaces the pool budgets, in points per second, with this
// collector's share of each. A pool missing from the map has no budget. A
// pool keeps its balance across a change, so a refresh is not a free burst.
func (l *Limiter) SetBudgets(rates map[string]float64) {
	if l == nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	for id := range l.pools {
		if _, ok := rates[id]; !ok {
			delete(l.pools, id)
		}
	}
	for id, rate := range rates {
		if rate <= 0 {
			delete(l.pools, id)
			continue
		}
		if b, ok := l.pools[id]; ok {
			b.rate = rate
			continue
		}
		l.pools[id] = &bucket{rate: rate, tokens: rate * BurstSeconds}
	}
}

// Admit decides whether a poll of ep may start. ok with a key means it holds
// a slot on that target until Done(key); startAt may be up to a second away,
// to honour the target's gap. Not ok: retry no earlier than retryAt.
func (l *Limiter) Admit(ep *models.Endpoint, now time.Time, budgeted bool) (
	ok bool, key string, startAt, retryAt time.Time, why Reason) {
	if l == nil {
		return true, "", now, time.Time{}, ReasonNone
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if budgeted && ep.PoolID != "" {
		if b := l.pools[ep.PoolID]; b != nil {
			b.refill(now)
			if b.tokens <= 0 {
				return false, "", time.Time{}, now.Add(time.Second), ReasonBudget
			}
		}
	}
	lim, limited := l.limits[ep.Address]
	if !limited || ep.Address == "" {
		return true, "", now, time.Time{}, ReasonNone
	}
	t := l.targets[ep.Address]
	if t == nil {
		t = &target{}
		l.targets[ep.Address] = t
	}
	if lim.maxConcurrent > 0 && t.inflight >= lim.maxConcurrent {
		return false, "", time.Time{}, now.Add(time.Second), ReasonTarget
	}
	start := now
	if t.nextStart.After(start) {
		start = t.nextStart
	}
	if start.Sub(now) >= time.Second {
		return false, "", time.Time{}, start, ReasonTarget
	}
	t.inflight++
	t.nextStart = start.Add(lim.minGap)
	return true, ep.Address, start, time.Time{}, ReasonNone
}

// Done releases a slot Admit handed out. An empty key is a no-op.
func (l *Limiter) Done(key string) {
	if l == nil || key == "" {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if t := l.targets[key]; t != nil && t.inflight > 0 {
		t.inflight--
	}
}

// Charge settles a finished poll's real cost against its pool's balance.
func (l *Limiter) Charge(pool string, points int, now time.Time) {
	if l == nil || pool == "" || points <= 0 {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if b := l.pools[pool]; b != nil {
		b.refill(now)
		b.tokens -= float64(points)
	}
}

// Targets is how many addresses carry a limit right now.
func (l *Limiter) Targets() int {
	if l == nil {
		return 0
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.limits)
}
