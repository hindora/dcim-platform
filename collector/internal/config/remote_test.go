package config

import (
	"testing"
	"time"
)

func base() *Config {
	c := &Config{}
	c.Protocols.SNMP.Enabled = true
	c.Protocols.SNMP.MaxConcurrent = 48
	c.Protocols.SNMP.PerHost = 2
	c.Protocols.SNMP.Timeout = 6 * time.Second
	c.Protocols.SNMP.Retries = 2
	c.Protocols.BACnet.Enabled = true
	c.Protocols.BACnet.LocalPort = 0
	c.Protocols.SNMPTrap.Enabled = true
	c.Protocols.SNMPTrap.Listen = "0.0.0.0:1162"
	c.Protocols.SNMPTrap.Workers = 8
	c.Protocols.SNMPTrap.RateLimitPerMinute = 100
	return c
}

func ptrI(v int) *int       { return &v }
func ptrB(v bool) *bool     { return &v }
func ptrS(v string) *string { return &v }

// An absent override leaves the file alone. This is what lets a default change
// in a release reach every collector that never overrode it - the alternative
// is a stored copy of every default, frozen at the moment somebody first
// opened the settings page.
func TestAbsentFieldsLeaveTheFileAlone(t *testing.T) {
	cfg := base()
	o := Overrides{SNMP: &ProtocolOverride{MaxConcurrent: ptrI(96)}}
	o.Apply(cfg)

	if got := cfg.Protocols.SNMP.MaxConcurrent; got != 96 {
		t.Fatalf("max_concurrent = %d, want 96", got)
	}
	if got := cfg.Protocols.SNMP.PerHost; got != 2 {
		t.Errorf("per_host = %d, want the file's 2", got)
	}
	if got := cfg.Protocols.SNMP.Timeout; got != 6*time.Second {
		t.Errorf("timeout = %s, want the file's 6s", got)
	}
}

// Zero is a real setting, and it is the sensible BACnet default: an ephemeral
// local port is enough for replies, and 47808 is only needed to RECEIVE
// broadcasts. If absent and zero were the same thing here, "go back to
// ephemeral" would be unexpressible.
func TestZeroIsAValueNotAnAbsence(t *testing.T) {
	cfg := base()
	cfg.Protocols.BACnet.LocalPort = 47808
	o := Overrides{BACnet: &BACnetOverride{LocalPort: ptrI(0)}}
	o.Apply(cfg)

	if got := cfg.Protocols.BACnet.LocalPort; got != 0 {
		t.Fatalf("local_port = %d, want 0 - an explicit zero must apply", got)
	}
}

// False, likewise: "disabled" is the whole point of an enable switch.
func TestFalseDisables(t *testing.T) {
	cfg := base()
	o := Overrides{SNMP: &ProtocolOverride{Enabled: ptrB(false)}}
	o.Apply(cfg)

	if cfg.Protocols.SNMP.Enabled {
		t.Fatal("snmp still enabled after an explicit false")
	}
}

func TestSecondsBecomeDurations(t *testing.T) {
	cfg := base()
	o := Overrides{SNMP: &ProtocolOverride{TimeoutS: ptrI(12)}}
	o.Apply(cfg)

	if got := cfg.Protocols.SNMP.Timeout; got != 12*time.Second {
		t.Fatalf("timeout = %s, want 12s", got)
	}
}

// The trap block is the one thing that can move without a restart, so the
// apply loop has to be able to tell whether it actually moved.
func TestTrapChangeIsDetected(t *testing.T) {
	running := base().Protocols.SNMPTrap

	same := Overrides{SNMPTrap: &TrapOverride{Listen: ptrS("0.0.0.0:1162")}}
	if same.TrapChanged(running) {
		t.Error("identical trap config reported as changed; it would rebind the " +
			"socket on every config fetch")
	}

	moved := Overrides{SNMPTrap: &TrapOverride{Listen: ptrS("0.0.0.0:162")}}
	if !moved.TrapChanged(running) {
		t.Error("moved listener not detected")
	}
	if got := moved.TrapConfig(running); got.Listen != "0.0.0.0:162" ||
		got.Workers != 8 {
		t.Errorf("merged trap config = %+v, want the new listen and the file's "+
			"workers", got)
	}

	none := Overrides{}
	if none.TrapChanged(running) {
		t.Error("an empty document must not be read as a change")
	}
}

// Everything except the trap block is read once, when the adapters are built.
// Reporting those as applied would describe an estate that does not exist.
func TestRestartPendingCoversWhatCannotBeAppliedLive(t *testing.T) {
	booted := base()

	trapOnly := Overrides{SNMPTrap: &TrapOverride{Listen: ptrS("0.0.0.0:162")}}
	if trapOnly.RestartPending(booted) {
		t.Error("a trap move needs no restart; it rebinds in place")
	}

	concurrency := Overrides{SNMP: &ProtocolOverride{MaxConcurrent: ptrI(96)}}
	if !concurrency.RestartPending(booted) {
		t.Error("a concurrency change does need a restart and must say so")
	}

	unchanged := Overrides{SNMP: &ProtocolOverride{MaxConcurrent: ptrI(48)}}
	if unchanged.RestartPending(booted) {
		t.Error("a value equal to the running one is not pending anything")
	}
}

// After a restart the process boots WITH the stored config applied, so nothing
// is pending any more. Getting this wrong leaves a banner on screen for ever.
func TestNothingIsPendingOnceTheProcessBootedWithIt(t *testing.T) {
	cfg := base()
	o := Overrides{
		SNMP:   &ProtocolOverride{MaxConcurrent: ptrI(96)},
		BACnet: &BACnetOverride{LocalPort: ptrI(47808)},
	}
	o.Apply(cfg) // what main does before the adapters are built

	if o.RestartPending(cfg) {
		t.Fatal("still pending after the process booted with the stored config")
	}
}
