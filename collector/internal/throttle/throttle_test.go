package throttle

import (
	"fmt"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

var t0 = time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)

func ep(id, addr, pool string, tl *models.TargetLimit) *models.Endpoint {
	return &models.Endpoint{ID: id, Address: addr, PoolID: pool, TargetLimit: tl}
}

func TestATargetAdmitsNoMoreThanItsConcurrencyAndReleasesOnDone(t *testing.T) {
	l := New()
	a := ep("a", "10.52.1.10", "", &models.TargetLimit{MaxConcurrent: 1})
	b := ep("b", "10.52.1.10", "", nil) // a slave behind the same gateway
	l.Track(a)
	l.Track(b)
	ok, key, _, _, _ := l.Admit(a, t0, false)
	if !ok || key != "10.52.1.10" {
		t.Fatalf("first poll refused: ok=%v key=%q", ok, key)
	}
	if ok, _, _, retry, why := l.Admit(b, t0, false); ok || why != ReasonTarget || !retry.After(t0) {
		t.Fatalf("a second poll at a gateway limited to one in flight was admitted (why=%q)", why)
	}
	l.Done(key)
	if ok, _, _, _, _ := l.Admit(b, t0, false); !ok {
		t.Fatal("the slot was not released")
	}
}

func TestTheGapBetweenStartsIsKeptWithoutBlocking(t *testing.T) {
	l := New()
	lim := &models.TargetLimit{MinIntervalMs: 400}
	eps := []*models.Endpoint{ep("a", "gw", "", lim), ep("b", "gw", "", lim),
		ep("c", "gw", "", lim)}
	for _, e := range eps {
		l.Track(e)
	}
	var starts []time.Time
	for _, e := range eps {
		ok, key, start, _, _ := l.Admit(e, t0, false)
		if !ok {
			// Third start would be 800 ms out - still inside the tick.
			t.Fatalf("%s refused", e.ID)
		}
		starts = append(starts, start)
		l.Done(key)
	}
	if starts[1].Sub(starts[0]) != 400*time.Millisecond || starts[2].Sub(starts[1]) != 400*time.Millisecond {
		t.Fatalf("starts not 400 ms apart: %v", starts)
	}
	d := ep("d", "gw", "", lim)
	l.Track(d)
	ok, _, _, retry, why := l.Admit(d, t0, false)
	if ok || why != ReasonTarget || !retry.Equal(t0.Add(1200*time.Millisecond)) {
		t.Fatalf("a start 1.2 s out should be deferred to it, got ok=%v retry=%v", ok, retry)
	}
}

func TestTheStrictestLimitAtAnAddressWinsAndRelaxesWhenItLeaves(t *testing.T) {
	l := New()
	strict := ep("s", "gw", "", &models.TargetLimit{MaxConcurrent: 1, MinIntervalMs: 100})
	loose := ep("l", "gw", "", &models.TargetLimit{MaxConcurrent: 4})
	l.Track(strict)
	l.Track(loose)
	ok, k, _, _, _ := l.Admit(loose, t0, false)
	if !ok {
		t.Fatal("first refused")
	}
	if ok, _, _, _, _ := l.Admit(loose, t0, false); ok {
		t.Fatal("the stricter max_concurrent 1 was not applied")
	}
	l.Done(k)
	l.Forget("s")
	later := t0.Add(time.Second)
	for i := 0; i < 4; i++ {
		if ok, _, _, _, _ := l.Admit(loose, later, false); !ok {
			t.Fatalf("poll %d refused after the strict endpoint left", i)
		}
	}
}

func TestAnAddressWithNoLimitIsNeverTouched(t *testing.T) {
	l := New()
	free := ep("f", "10.51.1.1", "", nil)
	l.Track(free)
	for i := 0; i < 100; i++ {
		if ok, key, _, _, _ := l.Admit(free, t0, false); !ok || key != "" {
			t.Fatal("an unlimited address was throttled or given a slot")
		}
	}
	if l.Targets() != 0 {
		t.Fatal("an unlimited endpoint created a target")
	}
}

func TestAPoolBudgetIsSettledOnRealPointsAndRefills(t *testing.T) {
	l := New()
	l.SetBudgets(map[string]float64{"p": 100}) // 500 banked
	e := ep("e", "x", "p", nil)
	if ok, _, _, _, _ := l.Admit(e, t0, true); !ok {
		t.Fatal("a fresh budget refused")
	}
	l.Charge(e, 800, t0) // a big walk: 300 in debt
	if ok, _, _, _, why := l.Admit(e, t0, true); ok || why != ReasonBudget {
		t.Fatal("a pool in debt admitted another poll")
	}
	if ok, _, _, _, _ := l.Admit(e, t0, false); !ok {
		t.Fatal("an unbudgeted scheduler (liveness) was held by the budget")
	}
	if ok, _, _, _, _ := l.Admit(e, t0.Add(2*time.Second), true); ok {
		t.Fatal("still 100 in debt after 2 s, yet admitted")
	}
	if ok, _, _, _, _ := l.Admit(e, t0.Add(4*time.Second), true); !ok {
		t.Fatal("the debt is paid after 3 s at 100/s, yet refused")
	}
}

func TestABudgetChangeKeepsTheBalanceAndRemovalLiftsIt(t *testing.T) {
	l := New()
	l.SetBudgets(map[string]float64{"p": 10})
	e := ep("e", "x", "p", nil)
	l.Charge(e, 1000, t0)
	l.SetBudgets(map[string]float64{"p": 20})
	if ok, _, _, _, _ := l.Admit(e, t0, true); ok {
		t.Fatal("a refresh wiped the debt - a free burst at the supervisor")
	}
	l.SetBudgets(map[string]float64{})
	if ok, _, _, _, _ := l.Admit(e, t0, true); !ok {
		t.Fatal("a pool with no budget is still throttled")
	}
}

func TestAdmissionReservesTheLastCostSoOneTickCannotOvershoot(t *testing.T) {
	l := New()
	l.SetBudgets(map[string]float64{"p": 20})
	eps := make([]*models.Endpoint, 5)
	for i := range eps {
		eps[i] = ep(fmt.Sprintf("e%d", i), "x", "p", nil)
		l.Charge(eps[i], 40, t0) // learn: each poll costs 40; 100 banked - 200 = -100
	}
	now := t0.Add(6 * time.Second) // +120: a balance of 20
	admitted := 0
	for _, e := range eps {
		if ok, _, _, _, _ := l.Admit(e, now, true); ok {
			admitted++
		}
	}
	if admitted != 1 {
		t.Fatalf("%d of five 40-point polls admitted on a 20-point balance, want 1", admitted)
	}
}

func TestSettlingRefundsTheReservationOfAFailedPoll(t *testing.T) {
	l := New()
	l.SetBudgets(map[string]float64{"p": 10}) // 50 banked
	e := ep("e", "x", "p", nil)
	l.Charge(e, 40, t0)                              // learned 40; balance 10
	if ok, _, _, _, _ := l.Admit(e, t0, true); !ok { // reserves 40: -30
		t.Fatal("refused on a positive balance")
	}
	l.Charge(e, 0, t0) // it failed: refund 40, balance 10
	if ok, _, _, _, _ := l.Admit(e, t0, true); !ok {
		t.Fatal("a failed poll's reservation was never refunded")
	}
}

func TestSyncPicksUpALimitChangeNoDiffReported(t *testing.T) {
	l := New()
	l.Sync([]*models.Endpoint{ep("a", "gw", "", nil), ep("b", "gw", "", nil)})
	if l.Targets() != 0 {
		t.Fatal("no limits yet")
	}
	// The pool gained a limit: same endpoints, same everything else.
	lim := &models.TargetLimit{MaxConcurrent: 1}
	a2, b2 := ep("a", "gw", "", lim), ep("b", "gw", "", lim)
	l.Sync([]*models.Endpoint{a2, b2})
	ok, key, _, _, _ := l.Admit(a2, t0, false)
	if !ok || key == "" {
		t.Fatal("first refused")
	}
	if ok, _, _, _, _ := l.Admit(b2, t0, false); ok {
		t.Fatal("the synced limit was not applied")
	}
	l.Done(key)
	l.Sync([]*models.Endpoint{ep("a", "gw", "", nil)})
	if l.Targets() != 0 {
		t.Fatal("a limit removed from the pool was kept")
	}
}
