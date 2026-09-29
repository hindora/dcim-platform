package config

import (
	"os"
	"path/filepath"
	"testing"
)

// A minimal config file: Default() fills everything else in, so this only
// has to exist and parse as YAML.
func writeMinimalConfig(t *testing.T) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "collector.yaml")
	if err := os.WriteFile(p, []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestEnvOverridesLetOneImageServeEveryCollectorInAFleet(t *testing.T) {
	// A container image or a .deb ships one config file; what differs between
	// collectors in a fleet has to be settable from the environment - a
	// systemd unit's Environment= line, not a per-host edit of the YAML.
	t.Setenv("DCIM_COLLECTOR_TOKEN", "test-token")
	t.Setenv("DCIM_COLLECTOR_ID", "col-dc2-oob")
	t.Setenv("DCIM_BASE_URL", "https://dcim.example.com")
	t.Setenv("DCIM_MAPPINGS_DIR", "/opt/dcim/mappings")

	cfg, err := Load(writeMinimalConfig(t))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.Collector.ID != "col-dc2-oob" {
		t.Errorf("collector id: got %q", cfg.Collector.ID)
	}
	if cfg.DCIM.BaseURL != "https://dcim.example.com" {
		t.Errorf("base url: got %q", cfg.DCIM.BaseURL)
	}
	if cfg.Mappings.Dir != "/opt/dcim/mappings" {
		t.Errorf("mappings dir: got %q", cfg.Mappings.Dir)
	}
}

func TestEnvOverridesLeaveTheFileAloneWhenUnset(t *testing.T) {
	t.Setenv("DCIM_COLLECTOR_TOKEN", "test-token")
	// Deliberately not setting DCIM_COLLECTOR_ID / DCIM_BASE_URL /
	// DCIM_MAPPINGS_DIR: the single-collector deployment that predates all
	// three must build exactly the config it always built.
	cfg, err := Load(writeMinimalConfig(t))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.Collector.ID != "col-1" {
		t.Errorf("default id should survive an unset override, got %q", cfg.Collector.ID)
	}
	if cfg.DCIM.BaseURL != "http://127.0.0.1:8000" {
		t.Errorf("default base url should survive an unset override, got %q",
			cfg.DCIM.BaseURL)
	}
	if cfg.Mappings.Dir != "../contracts/mappings" {
		t.Errorf("default mappings dir should survive an unset override, got %q",
			cfg.Mappings.Dir)
	}
}

func TestAnIDWithADotIsRejectedEvenFromTheEnvironment(t *testing.T) {
	// The same rule that guards the file: a dot in the id breaks the
	// "<id>.<generation>.<mac>" token format, and the environment is not a
	// looser path around it.
	t.Setenv("DCIM_COLLECTOR_TOKEN", "test-token")
	t.Setenv("DCIM_COLLECTOR_ID", "col.dc2")
	if _, err := Load(writeMinimalConfig(t)); err == nil {
		t.Fatal("an id containing '.' from the environment was accepted")
	}
}
