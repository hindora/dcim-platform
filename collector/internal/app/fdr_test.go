package app

import (
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/assign"
	"github.com/hari/dcim-platform/collector/internal/config"
)

func pool(addr string, ttl int, enabled bool) assign.Pool {
	return assign.Pool{ID: "p-" + addr, BBMD: assign.BBMD{
		Enabled: enabled, Addr: addr, TTLSeconds: ttl}}
}

func TestDesiredBBMDsIsEmptyWithNothingConfigured(t *testing.T) {
	cfg := config.Default()
	if got := desiredBBMDs(cfg, nil); len(got) != 0 {
		t.Fatalf("got %v, want none", got)
	}
}

func TestDesiredBBMDsTakesTheConfigsOwnEntry(t *testing.T) {
	cfg := config.Default()
	cfg.Protocols.BACnet.FDR.Enabled = true
	cfg.Protocols.BACnet.FDR.BBMD = "10.52.1.1:47808"
	cfg.Protocols.BACnet.FDR.TTL = 120 * time.Second

	got := desiredBBMDs(cfg, nil)
	if got["10.52.1.1:47808"] != 120*time.Second {
		t.Fatalf("got %v", got)
	}
}

func TestDesiredBBMDsAddsEveryEnabledPoolBBMD(t *testing.T) {
	cfg := config.Default()
	pools := map[string]assign.Pool{
		"a": pool("10.52.1.1:47808", 300, true),
		"b": pool("10.52.2.1:47808", 60, true),
		"c": pool("10.52.3.1:47808", 300, false), // disabled: static unicast
		"d": {ID: "d"},                           // no BBMD at all
	}
	got := desiredBBMDs(cfg, pools)
	if len(got) != 2 {
		t.Fatalf("got %v, want two", got)
	}
	if got["10.52.1.1:47808"] != 300*time.Second || got["10.52.2.1:47808"] != 60*time.Second {
		t.Fatalf("got %v", got)
	}
}

func TestDesiredBBMDsConfigWinsOnTheSameAddress(t *testing.T) {
	cfg := config.Default()
	cfg.Protocols.BACnet.FDR.Enabled = true
	cfg.Protocols.BACnet.FDR.BBMD = "10.52.1.1:47808"
	cfg.Protocols.BACnet.FDR.TTL = 120 * time.Second

	got := desiredBBMDs(cfg, map[string]assign.Pool{"a": pool("10.52.1.1:47808", 300, true)})
	if len(got) != 1 || got["10.52.1.1:47808"] != 120*time.Second {
		t.Fatalf("got %v, want the config's 120s to win", got)
	}
}

func TestDesiredBBMDsDefaultsAZeroTTL(t *testing.T) {
	// A pool row from before the pools API carried {} - the platform
	// defaults ttl_s, but a hand-written assignment might not.
	got := desiredBBMDs(config.Default(), map[string]assign.Pool{"a": pool("10.52.1.1:47808", 0, true)})
	if got["10.52.1.1:47808"] != 300*time.Second {
		t.Fatalf("got %v, want the 300s default", got)
	}
}

func TestReconcileFDRIsANoOpWithoutABACnetAdapterOrBeforeRun(t *testing.T) {
	// Neither a.bacnet nor a.fdrCtx is set: must return without touching
	// a.assign (nil here - a dereference would panic).
	a := &App{cfg: config.Default()}
	a.reconcileFDR()
	if a.fdrLoops != nil {
		t.Fatal("started loops with no adapter and no context")
	}
}
