package mapping

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

// TrapDef is one wire OID's meaning.
//
// Keyed by the OID that ARRIVES, because that is all a receiver sees. Real gear
// keys notifications off the vendor rather than off the condition, so the same
// physical event reaches us as different OIDs from different vendors and the
// table has to be keyed accordingly.
type TrapDef struct {
	OID       string `yaml:"oid"`
	EventType string `yaml:"event_type"`
	Severity  string `yaml:"severity"`
	IsClear   bool   `yaml:"is_clear"`
	// Clears lists the event types this notification resolves. A clear carries
	// the event type of the condition it ends, never one of its own, which is
	// what lets it reach the same alarm key as the raise without the backend
	// ever parsing an OID.
	Clears []string `yaml:"clears"`
	// InstanceFromVarbind names a varbind OID whose value identifies WHICH
	// interface, sensor or phase the notification is about.
	InstanceFromVarbind string `yaml:"instance_from_varbind"`
}

type trapFile struct {
	Version int       `yaml:"version"`
	Traps   []TrapDef `yaml:"traps"`
}

// TrapTable resolves a wire OID to its canonical meaning.
type TrapTable struct {
	byOID map[string]TrapDef
}

// LoadTraps reads every trap mapping under <dir>/snmp/traps*.yaml.
func LoadTraps(dir string) (*TrapTable, error) {
	t := &TrapTable{byOID: make(map[string]TrapDef)}
	paths, err := filepath.Glob(filepath.Join(dir, "snmp", "traps*.yaml"))
	if err != nil {
		return nil, err
	}
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read %s: %w", path, err)
		}
		var f trapFile
		if err := yaml.Unmarshal(raw, &f); err != nil {
			return nil, fmt.Errorf("parse %s: %w", path, err)
		}
		for _, d := range f.Traps {
			if d.OID == "" || d.EventType == "" {
				return nil, fmt.Errorf("%s: trap entry missing oid or event_type", path)
			}
			if d.IsClear && len(d.Clears) == 0 {
				// A clear that resolves nothing would raise a fresh alarm
				// instead of ending one - the opposite of its purpose.
				return nil, fmt.Errorf("%s: %s is_clear but lists no `clears`",
					path, d.OID)
			}
			t.byOID[strings.TrimPrefix(d.OID, ".")] = d
		}
	}
	return t, nil
}

// Lookup returns the definition for a wire OID.
func (t *TrapTable) Lookup(oid string) (TrapDef, bool) {
	d, ok := t.byOID[strings.TrimPrefix(oid, ".")]
	return d, ok
}

func (t *TrapTable) Len() int { return len(t.byOID) }
