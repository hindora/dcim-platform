package app

import (
	"context"
	"time"

	"github.com/hari/dcim-platform/collector/internal/adapters/snmp"
	"github.com/hari/dcim-platform/collector/internal/config"
)

// How long to wait for a stopped trap receiver to release its socket.
//
// The old listener has to be closed before the new one can bind the same port,
// and closing is not instant: the receiver drains its queue and waits for its
// workers. Rebinding too early fails with "address already in use" on a change
// that was otherwise fine.
const trapDrainTimeout = 5 * time.Second

// startTraps launches the receiver under its own cancellable context.
//
// Its own, not the app's, so that a configuration change can stop this
// listener without stopping anything else.
func (a *App) startTraps(parent context.Context, cfg config.TrapCfg) {
	if !cfg.Enabled {
		a.log.Info("trap receiver disabled by configuration")
		return
	}
	ctx, cancel := context.WithCancel(parent)
	done := make(chan struct{})

	receiver := snmp.NewTrapReceiver(a.trapTable, a.resolver, a.pub, a.log,
		a.mets, cfg.Listen, cfg.Workers, cfg.RateLimitPerMinute)

	a.trapMu.Lock()
	a.traps, a.trapCfg, a.trapStop, a.trapDone = receiver, cfg, cancel, done
	a.trapMu.Unlock()

	go func() {
		defer close(done)
		// A trap listener that cannot bind must not take the collector down:
		// polling still works, and the operator needs to see which half is
		// broken rather than losing both.
		if err := receiver.Listen(ctx); err != nil {
			a.log.Error("trap receiver stopped", "error", err, "listen", cfg.Listen)
			a.setConfigError("trap receiver on " + cfg.Listen + ": " + err.Error())
		}
	}()
}

// stopTraps closes the current listener and waits for the socket to go.
func (a *App) stopTraps() {
	a.trapMu.Lock()
	stop, done := a.trapStop, a.trapDone
	a.trapStop, a.trapDone = nil, nil
	a.trapMu.Unlock()

	if stop == nil {
		return
	}
	stop()
	if done == nil {
		return
	}
	select {
	case <-done:
	case <-time.After(trapDrainTimeout):
		// Carry on and let the bind fail with a message an operator can read,
		// rather than blocking the config loop forever on a receiver that is
		// wedged.
		a.log.Warn("trap receiver did not stop in time; rebinding anyway")
	}
}

// applyConfig folds a new configuration into the running collector.
//
// Two outcomes, and the difference is the whole reason the heartbeat carries
// three config fields:
//
//   - the trap block is applied HERE, by rebinding the socket;
//   - everything else is recorded as pending, because adapters read their
//     concurrency, timeouts and ports once, when they are built. A page that
//     called those "applied" would be describing an estate that does not
//     exist.
func (a *App) applyConfig(ctx context.Context, version uint32,
	o config.Overrides) {
	a.trapMu.Lock()
	running := a.trapCfg
	a.trapMu.Unlock()

	want := o.TrapConfig(running)
	if want != running {
		a.log.Info("applying trap configuration", "version", version,
			"listen", want.Listen, "enabled", want.Enabled,
			"was", running.Listen)
		a.setConfigError("")
		a.stopTraps()
		a.startTraps(ctx, want)

		// A moved listener is not a self-contained change: every device is
		// still sending to the old address, and nothing anywhere reports that
		// as an error. It is worth a line in the log that says so.
		if want.Listen != running.Listen {
			a.log.Warn("trap listener moved; devices still sending to the old "+
				"address will not be received",
				"from", running.Listen, "to", want.Listen)
		}
	}

	if pending := o.RestartPending(a.cfg); pending {
		a.log.Info("configuration stored that this process cannot apply while "+
			"running; a restart will pick it up", "version", version)
	}
}

// restartPending reports whether the stored config asks for something only a
// restart can deliver.
func (a *App) restartPending() bool {
	if a.cfgClient == nil {
		return false
	}
	return a.cfgClient.Current().RestartPending(a.cfg)
}

func (a *App) setConfigError(msg string) {
	a.cfgErrMu.Lock()
	a.cfgErr = msg
	a.cfgErrMu.Unlock()
}

func (a *App) configError() string {
	a.cfgErrMu.Lock()
	defer a.cfgErrMu.Unlock()
	return a.cfgErr
}

// SetConfigClient attaches the remote configuration client.
//
// Set after construction rather than passed in, because the client has to
// fetch once before the adapters exist - the boot fetch is what makes a stored
// setting effective from the first poll.
func (a *App) SetConfigClient(c *config.RemoteClient) { a.cfgClient = c }
