package mapping

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
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
	// MatchVarbind narrows this meaning by a varbind the notification carries.
	//
	// The last resort of a receiver, and the one real gear leans on hardest: a
	// Liebert card sends ONE OID - lgpEventConditionAdded, 476.1.42.3.3.0.1 -
	// for airflow, dew point, frequency, humidity, input voltage and
	// temperature alike, with the condition itself in lgpConditionDescr. Cisco
	// sends one environmental notification for a warning and for a critical,
	// with the state in ciscoEnvMonTemperatureState. No amount of OID or
	// device-type matching separates those; the varbind does, and it is the
	// discriminator the vendor intended.
	// EVERY condition must hold. Raritan needs two to say anything at all: an
	// inlet sensor notification carries typeOfSensor AND the new state, so
	// "current, above upper warning" is a load alarm while "current, above
	// upper critical" is the critical one - on the same OID, from the same
	// PDU. One condition would resolve half of that.
	MatchVarbinds []VarbindMatch `yaml:"match_varbinds"`
	// Metric names what this notification measured, and ValueVarbind /
	// ThresholdVarbind say which varbinds carry the reading and the limit.
	//
	// A threshold trap nearly always ships both: Cisco sends
	// cpmCPUTotal5minRev beside its rising threshold, this plane sends its
	// reading and limit in adjacent enterprise varbinds, Raritan sends the
	// sensor value. Carrying them through is what lets an alarm raised by a
	// notification be VERIFIED later against polled telemetry - and what puts
	// a number in front of an operator instead of a bare condition name.
	//
	// All three are empty for a notification that measures nothing, which is
	// most state traps: a link is down or it is not.
	Metric           string `yaml:"metric"`
	ValueVarbind     string `yaml:"value_varbind"`
	ThresholdVarbind string `yaml:"threshold_varbind"`
	// ValueScale converts the varbind's units to the metric's. Vendors do not
	// send the unit an operator reads: APC reports rPDULoadStatusLoad in
	// TENTHS of an amp, so 135 means 13.5 A. Publishing the raw number under a
	// metric measured in amps would put "135 A" on a 13.5 A circuit - a
	// plausible reading, wrong by a factor of ten, and indistinguishable from
	// a real overload.
	//
	// Zero means unset and is treated as 1, so every existing entry keeps its
	// value untouched.
	ValueScale float64 `yaml:"value_scale"`
	// DisplayName is what the vendor calls this condition, in words. Used for
	// the message a person reads; the OID stays on the event as
	// raw_identifier, where a machine can still find it.
	DisplayName string `yaml:"display_name"`
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

// VarbindMatch is a condition on one varbind's value.
//
// `contains` for text the vendor writes for people to read - a description
// field whose exact wording carries a device name and sometimes a measured
// value, so equality would never hold. `equals_int` for an enumerated state,
// where the vendor's own numbering is the meaning.
type VarbindMatch struct {
	OID       string `yaml:"oid"`
	Contains  string `yaml:"contains"`
	EqualsInt *int   `yaml:"equals_int"`
}

// matchesAll reports whether every condition holds. No conditions is not a
// match: an entry with no matcher is a general meaning, resolved later and on
// purpose, not something that quietly wins here.
func matchesAll(ms []VarbindMatch, varbinds map[string]string) bool {
	if len(ms) == 0 {
		return false
	}
	for i := range ms {
		if !ms[i].matches(varbinds) {
			return false
		}
	}
	return true
}

// matches reports whether this notification's varbinds satisfy the condition.
// An absent varbind never matches: a receiver that treated silence as
// agreement would resolve every ambiguous OID to whichever meaning happened to
// be first.
func (m *VarbindMatch) matches(varbinds map[string]string) bool {
	if m == nil || m.OID == "" {
		return false
	}
	value, ok := varbinds[strings.TrimPrefix(m.OID, ".")]
	if !ok {
		value, ok = varbinds[m.OID]
	}
	if !ok {
		return false
	}
	if m.EqualsInt != nil {
		n, err := strconv.Atoi(strings.TrimSpace(value))
		return err == nil && n == *m.EqualsInt
	}
	if m.Contains == "" {
		return false
	}
	return strings.Contains(strings.ToLower(value), strings.ToLower(m.Contains))
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
func (t *TrapTable) Lookup(oid, deviceType string,
	varbinds map[string]string) (TrapDef, bool) {

	defs := t.byOID[strings.TrimPrefix(oid, ".")]
	if len(defs) == 0 {
		return TrapDef{}, false
	}

	// 1. The varbind the vendor put the condition in. Most specific, and the
	//    only thing that separates the meanings of a Liebert condition trap.
	// Most specific first: an entry asking for two varbinds beats one asking
	// for a single varbind on the same OID.
	best, bestN := -1, 0
	for i := range defs {
		if n := len(defs[i].MatchVarbinds); n > bestN &&
			matchesAll(defs[i].MatchVarbinds, varbinds) {
			best, bestN = i, n
		}
	}
	if best >= 0 {
		return defs[best], true
	}

	// 2. The sender. Separates meanings that belong to different equipment.
	var generic *TrapDef
	for i := range defs {
		if len(defs[i].DeviceTypes) == 0 && len(defs[i].MatchVarbinds) == 0 {
			if generic == nil {
				generic = &defs[i]
			}
			continue
		}
		if deviceType == "" || len(defs[i].DeviceTypes) == 0 {
			continue
		}
		for _, dt := range defs[i].DeviceTypes {
			if dt == deviceType {
				return defs[i], true
			}
		}
	}

	// 3. The general meaning, if this OID has one.
	if generic != nil {
		return *generic, true
	}
	return TrapDef{}, false
}

// Len counts wire OIDs, not entries: an OID with three device-specific
// meanings is still one OID a receiver can resolve.
func (t *TrapTable) Len() int { return len(t.byOID) }
