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
	// DeviceTypes narrows this meaning to the kinds of device that can send
	// it. Empty means any.
	//
	// Vendors reuse OIDs. A Liebert card sends 476.1.42.3.3.0.5 for a charger
	// failure on a UPS and for a fan failure on cooling gear; keyed on the OID
	// alone, one of those two readings is always wrong. The receiver knows
	// which endpoint the trap arrived from, so the sender's device type is the
	// discriminator that was there all along.
	DeviceTypes []string `yaml:"device_types"`
}

type trapFile struct {
	Version int       `yaml:"version"`
	Traps   []TrapDef `yaml:"traps"`
}

// TrapTable resolves a wire OID to its canonical meaning.
//
// One OID may carry SEVERAL meanings, told apart by the device type that sent
// it, so the table holds every candidate rather than the last one parsed.
type TrapTable struct {
	byOID map[string][]TrapDef
}

// LoadTraps reads every trap mapping under <dir>/snmp/traps*.yaml.
func LoadTraps(dir string) (*TrapTable, error) {
	t := &TrapTable{byOID: make(map[string][]TrapDef)}
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
			key := strings.TrimPrefix(d.OID, ".")
			t.byOID[key] = append(t.byOID[key], d)
		}
	}
	return t, nil
}

// Lookup returns the definition for a wire OID sent by a device of
// deviceType. An empty deviceType means the sender is unknown - an
// unattributable trap - and only the unrestricted entries can apply.
//
// Preference, in order:
//
//  1. an entry naming this device type. The sender is the only evidence that
//     tells two meanings of one OID apart.
//  2. an entry naming no device type, which is the general meaning.
//  3. nothing, when every candidate belongs to some other kind of device -
//     reported as unknown rather than guessed, because a wrong reading here
//     files a fan failure as a charger fault and sends somebody to the wrong
//     rack.
func (t *TrapTable) Lookup(oid, deviceType string) (TrapDef, bool) {
	defs := t.byOID[strings.TrimPrefix(oid, ".")]
	if len(defs) == 0 {
		return TrapDef{}, false
	}
	var generic *TrapDef
	for i := range defs {
		if len(defs[i].DeviceTypes) == 0 {
			if generic == nil {
				generic = &defs[i]
			}
			continue
		}
		if deviceType == "" {
			continue
		}
		for _, dt := range defs[i].DeviceTypes {
			if dt == deviceType {
				return defs[i], true
			}
		}
	}
	if generic != nil {
		return *generic, true
	}
	return TrapDef{}, false
}

// Len counts wire OIDs, not entries: an OID with three device-specific
// meanings is still one OID a receiver can resolve.
func (t *TrapTable) Len() int { return len(t.byOID) }
