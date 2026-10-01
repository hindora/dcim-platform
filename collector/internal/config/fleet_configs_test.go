package config

import (
	"path/filepath"
	"testing"
)

// Every generated fleet file must load, and no two may collide on anything
// that binds a port or owns a directory on a shared host.
func TestFleetConfigsLoadAndDoNotCollide(t *testing.T) {
	files, _ := filepath.Glob("../../configs/fleet/*.yaml")
	if len(files) == 0 {
		t.Skip("no fleet configs")
	}
	t.Setenv("DCIM_COLLECTOR_TOKEN", "test-only")
	t.Setenv("DCIM_COLLECTOR_ID", "")
	seen := map[string]string{}
	claim := func(what, v, f string) {
		if prev, ok := seen[what+v]; ok {
			t.Errorf("%s %s used by both %s and %s", what, v, prev, f)
		}
		seen[what+v] = f
	}
	for _, f := range files {
		c, err := Load(f)
		if err != nil {
			t.Fatalf("%s: %v", f, err)
		}
		claim("id", c.Collector.ID, f)
		claim("state_dir", c.Collector.StateDir, f)
		claim("trap", c.Protocols.SNMPTrap.Listen, f)
		claim("metrics", c.Observability.MetricsListen, f)
		claim("health", c.Observability.HealthListen, f)
		if !c.Update.Enabled || len(c.Update.TrustedKeys) == 0 ||
			c.Update.VerifyWindow <= c.Update.ConfirmAfter {
			t.Errorf("%s: update block not parsed as intended: %+v", f, c.Update)
		}
	}
}
