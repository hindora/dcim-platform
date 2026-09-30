package app

import (
	"context"
	"time"

	"github.com/hari/dcim-platform/collector/internal/adapters/bacnet"
	"github.com/hari/dcim-platform/collector/internal/assign"
	"github.com/hari/dcim-platform/collector/internal/config"
)

// fdrLoop is one running RenewForeignDeviceRegistration goroutine.
type fdrLoop struct {
	cancel context.CancelFunc
	ttl    time.Duration
}

// desiredBBMDs is the set of BBMDs this collector should currently be
// foreign-registered with, by address, with the TTL to register for.
//
// The config's own FDR entry (Phase 9's collector.yaml setting) is kept as
// a first-class source rather than replaced: a collector whose only BACnet
// work is behind a BBMD the platform does not model yet still needs it.
// Pool entries (Phase 5's collector_pool.bbmd_settings, on the wire since
// the pools API) add to it. On the same address the config wins - an
// operator who set a TTL in the file did so on purpose.
//
// Pure so it can be tested against fixed inputs; the goroutine bookkeeping
// that acts on it is reconcileFDR.
func desiredBBMDs(cfg *config.Config, pools map[string]assign.Pool) map[string]time.Duration {
	out := map[string]time.Duration{}
	if f := cfg.Protocols.BACnet.FDR; f.Enabled && f.BBMD != "" {
		out[f.BBMD] = f.TTL
	}
	for _, p := range pools {
		if !p.BBMD.Enabled || p.BBMD.Addr == "" {
			continue
		}
		if _, dup := out[p.BBMD.Addr]; dup {
			continue
		}
		ttl := time.Duration(p.BBMD.TTLSeconds) * time.Second
		if ttl <= 0 {
			ttl = 300 * time.Second
		}
		out[p.BBMD.Addr] = ttl
	}
	return out
}

// reconcileFDR starts a registration loop for every BBMD in desiredBBMDs
// that has none, stops every loop whose BBMD is no longer wanted, and
// restarts one whose TTL changed. A no-op before Run has set fdrCtx (the
// first assignment fetch can precede it) and for a collector with no
// BACnet adapter at all - a BBMD is meaningless without one.
func (a *App) reconcileFDR() {
	if a.bacnet == nil || a.fdrCtx == nil {
		return
	}
	want := desiredBBMDs(a.cfg, a.assign.Pools())

	a.fdrMu.Lock()
	defer a.fdrMu.Unlock()
	if a.fdrLoops == nil {
		a.fdrLoops = map[string]fdrLoop{}
	}
	for addr, loop := range a.fdrLoops {
		ttl, still := want[addr]
		if still && ttl == loop.ttl {
			continue
		}
		loop.cancel()
		delete(a.fdrLoops, addr)
		a.log.Info("stopped BBMD registration", "bbmd", addr)
	}
	for addr, ttl := range want {
		if _, running := a.fdrLoops[addr]; running {
			continue
		}
		ctx, cancel := context.WithCancel(a.fdrCtx)
		a.fdrLoops[addr] = fdrLoop{cancel: cancel, ttl: ttl}
		go bacnet.RenewForeignDeviceRegistration(ctx, addr, ttl,
			a.cfg.Protocols.BACnet.Timeout, a.log)
		a.log.Info("started BBMD registration", "bbmd", addr, "ttl", ttl)
	}
}
