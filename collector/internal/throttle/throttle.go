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
// token bucket. Admission needs a positive balance and RESERVES what the
// endpoint's last poll cost; the difference is settled when this one
// finishes. Charging only afterwards let every poll due in one tick through
// on the same positive balance - the live run sat 4-5% over its budget - and
// a big walk still leaves a debt that holds the next polls back.
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
	// Per endpoint: the points its last poll returned (the next poll's
	// reservation), and what an admitted poll has reserved so far.
	lastPoints map[string]int
	reserved   map[string]int
}

type endpointLimit struct {
	target string
	limit  limit
}

func New() *Limiter {
	return &Limiter{byEndpoint: map[string]endpointLimit{}, limits: map[string]limit{},
		targets: map[string]*target{}, pools: map[string]*bucket{},
		lastPoints: map[string]int{}, reserved: map[string]int{}}
}

// Sync replaces every endpoint's limit with the assignment's - the whole
// set, on every fetched body. An endpoint diff that does not compare the
// limit cannot then lose a change to it: the live run set a pool limit,
// the ETag moved, the body arrived, and nothing was re-tracked.
func (l *Limiter) Sync(eps []*models.Endpoint) {
	if l == nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	keep := make(map[string]bool, len(eps))
	l.byEndpoint = make(map[string]endpointLimit, len(eps))
	for _, ep := range eps {
		keep[ep.ID] = true
		if e, ok := limitOf(ep); ok {
			l.byEndpoint[ep.ID] = e
		}
	}
	for id := range l.lastPoints {
		if !keep[id] {
			delete(l.lastPoints, id)
		}
	}
	l.limits = map[string]limit{}
	seen := map[string]bool{}
	for _, e := range l.byEndpoint {
		if !seen[e.target] {
			seen[e.target] = true
			l.recompute(e.target)
		}
	}
}

func limitOf(ep *models.Endpoint) (endpointLimit, bool) {
	tl := ep.TargetLimit
	if ep.Address == "" || tl == nil || (tl.MaxConcurrent <= 0 && tl.MinIntervalMs <= 0) {
		return endpointLimit{}, false
	}
	return endpointLimit{target: ep.Address, limit: limit{
		maxConcurrent: tl.MaxConcurrent,
		minGap:        time.Duration(tl.MinIntervalMs) * time.Millisecond}}, true
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
	if e, ok := limitOf(ep); ok {
		l.byEndpoint[ep.ID] = e
	} else {
		delete(l.byEndpoint, ep.ID)
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
	var pool *bucket
	if budgeted && ep.PoolID != "" {
		if b := l.pools[ep.PoolID]; b != nil {
			b.refill(now)
			if b.tokens <= 0 {
				return false, "", time.Time{}, now.Add(time.Second), ReasonBudget
			}
			pool = b
		}
	}
	// Reserve only once the poll is surely admitted: a target refusal
	// below must not leave a reservation nobody settles.
	reserve := func() {
		if pool != nil {
			n := l.lastPoints[ep.ID]
			pool.tokens -= float64(n)
			l.reserved[ep.ID] += n
		}
	}
	lim, limited := l.limits[ep.Address]
	if !limited || ep.Address == "" {
		reserve()
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
	reserve()
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

// Charge settles a finished poll against its pool's balance: its real cost
// less what admission reserved for it, and remembers the cost as the next
// poll's reservation. A failed poll is charged 0 and refunds its
// reservation - it put next to nothing on the network.
func (l *Limiter) Charge(ep *models.Endpoint, points int, now time.Time) {
	if l == nil || ep == nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	held := l.reserved[ep.ID]
	delete(l.reserved, ep.ID)
	if points > 0 {
		l.lastPoints[ep.ID] = points
	}
	if b := l.pools[ep.PoolID]; ep.PoolID != "" && b != nil {
		b.refill(now)
		b.tokens -= float64(points - held)
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
