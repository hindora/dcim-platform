package snmp

import (
	"context"
	"time"
)

// hold parks a trap that arrived before the inventory that explains it.
//
// Returns false when the trap should be published now instead: either the
// inventory is already loaded - in which case an unresolved source is a real
// finding rather than a race - or the buffer is full.
func (t *TrapReceiver) hold(trap *heldTrap) bool {
	// A loaded inventory that does not contain this source means the platform
	// genuinely does not know the device. Holding that would delay a true
	// finding by two minutes and then report it anyway.
	if t.resolver.Len() > 0 {
		return false
	}

	t.holdMu.Lock()
	defer t.holdMu.Unlock()

	if len(t.held) >= t.holdMax {
		// Degraded, not lost. The buffer exists to survive a startup, not to
		// absorb a device plane pointed at the wrong collector.
		t.mets.TrapsTotal.WithLabelValues("hold_overflow").Inc()
		return false
	}
	t.held = append(t.held, trap)
	t.mets.TrapsTotal.WithLabelValues("held").Inc()
	if len(t.held) == 1 {
		t.log.Info("holding traps until the inventory arrives",
			"source", trap.source, "oid", trap.trapOID)
	}
	return true
}

// drainHeld retries held traps until ctx is cancelled.
//
// Runs for the life of the receiver rather than only at startup: an assignment
// fetch can fail for minutes, and a collector that lost its inventory and got
// it back has exactly the same problem as one that has just started.
func (t *TrapReceiver) drainHeld(ctx context.Context) {
	ticker := time.NewTicker(holdRetryEvery)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			// Publish what is still held rather than losing it on shutdown.
			// Unattributed is worse than attributed and far better than gone.
			t.flushHeld(context.WithoutCancel(ctx), true)
			return
		case <-ticker.C:
			t.flushHeld(ctx, false)
		}
	}
}

// flushHeld publishes every held trap that can now be attributed, and every
// one that has waited long enough to stop being a race.
//
// Order is preserved. A cpuHigh and the cpuNormal that followed it can both be
// in the buffer, and publishing them out of order would leave an alarm raised
// by the clear and cleared by the raise - the console would show a fault on a
// device that is fine, which is the exact failure this whole area keeps
// producing.
func (t *TrapReceiver) flushHeld(ctx context.Context, all bool) {
	t.holdMu.Lock()
	if len(t.held) == 0 {
		t.holdMu.Unlock()
		return
	}
	ready := t.resolver.Len() > 0
	now := time.Now()

	var send, keep []*heldTrap
	for _, trap := range t.held {
		switch {
		case all, ready:
			send = append(send, trap)
		case now.Sub(trap.at) >= t.holdFor:
			// Waited out the race. Published unattributed, because a trap this
			// platform cannot explain is itself worth seeing - and two minutes
			// is long past any assignment fetch.
			t.mets.TrapsTotal.WithLabelValues("hold_expired").Inc()
			send = append(send, trap)
		default:
			keep = append(keep, trap)
		}
	}
	t.held = keep
	t.holdMu.Unlock()

	if len(send) == 0 {
		return
	}
	attributed := 0
	for _, trap := range send {
		if _, ok := t.resolver.Resolve(trap.source, trap.community); ok {
			attributed++
		}
		t.emit(ctx, trap)
	}
	t.log.Info("held traps released", "count", len(send),
		"attributed", attributed,
		"oldest_age_seconds", int(now.Sub(send[0].at).Seconds()))
}

// HeldCount is the number of traps waiting for an inventory. Exposed for the
// tests, and for a metric that answers "is anything stuck".
func (t *TrapReceiver) HeldCount() int {
	t.holdMu.Lock()
	defer t.holdMu.Unlock()
	return len(t.held)
}
