package mapping

import (
	"os"
	"path/filepath"
	"testing"
)

// One wire OID, several meanings, told apart by what sent it.
//
// Vendors reuse OIDs. A Liebert card sends 476.1.42.3.3.0.5 for a fan failure
// and for a charger failure; keyed on the OID alone one of those readings is
// always wrong, and the table used to pick a single winner - so a fan failure
// on a server was filed as a UPS charger fault, and nothing recorded that a
// choice had been made.
//
// The receiver already knows which endpoint a trap arrived from, so the
// sender's device type was available all along.

func tableFrom(t *testing.T, body string) *TrapTable {
	t.Helper()
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "snmp"), 0o755); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "snmp", "traps.yaml")
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	tbl, err := LoadTraps(dir)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	return tbl
}

const shared = `
version: 1
traps:
  - oid: 1.3.6.1.4.1.476.1.42.3.3.0.5
    event_type: fan_failure
    severity: CRITICAL
    device_types: [server]
  - oid: 1.3.6.1.4.1.476.1.42.3.3.0.5
    event_type: charger_failure
    severity: CRITICAL
`

func TestTheSenderPicksTheMeaning(t *testing.T) {
	tbl := tableFrom(t, shared)

	got, ok := tbl.Lookup("1.3.6.1.4.1.476.1.42.3.3.0.5", "server")
	if !ok || got.EventType != "fan_failure" {
		t.Fatalf("server sent it: got %q (ok=%v), want fan_failure", got.EventType, ok)
	}

	got, ok = tbl.Lookup("1.3.6.1.4.1.476.1.42.3.3.0.5", "ups")
	if !ok || got.EventType != "charger_failure" {
		t.Fatalf("ups sent it: got %q (ok=%v), want charger_failure", got.EventType, ok)
	}
}

func TestAnUnattributableTrapFallsBackToTheGeneralMeaning(t *testing.T) {
	// No device type means the trap could not be attributed to an endpoint.
	// Only the unrestricted entry can apply - guessing the server-specific one
	// would invent a fan failure on a device we cannot even name.
	tbl := tableFrom(t, shared)
	got, ok := tbl.Lookup("1.3.6.1.4.1.476.1.42.3.3.0.5", "")
	if !ok || got.EventType != "charger_failure" {
		t.Fatalf("unattributed: got %q (ok=%v), want charger_failure", got.EventType, ok)
	}
}

func TestAnOIDThatBelongsToOtherEquipmentIsUnknownRatherThanGuessed(t *testing.T) {
	// Every candidate names some other kind of device. Reporting unknown puts
	// the raw OID in front of somebody; guessing sends an engineer to the wrong
	// rack for a fault that is not there.
	tbl := tableFrom(t, `
version: 1
traps:
  - oid: 1.3.6.1.4.1.9.9.13.3.0.4
    event_type: fan_failure
    severity: CRITICAL
    device_types: [server]
`)
	if _, ok := tbl.Lookup("1.3.6.1.4.1.9.9.13.3.0.4", "chiller"); ok {
		t.Fatal("a chiller matched a server-only meaning")
	}
	if _, ok := tbl.Lookup("1.3.6.1.4.1.9.9.13.3.0.4", "server"); !ok {
		t.Fatal("the server it does belong to did not match")
	}
}

func TestALeadingDotIsStillTolerated(t *testing.T) {
	// Some agents send .1.3.6... Losing that would drop every trap from them.
	tbl := tableFrom(t, shared)
	if _, ok := tbl.Lookup(".1.3.6.1.4.1.476.1.42.3.3.0.5", "server"); !ok {
		t.Fatal("a leading dot broke the lookup")
	}
}

func TestLenCountsOIDsNotMeanings(t *testing.T) {
	// The startup log reports this number; counting entries would say the
	// mapping grew when only its precision did.
	tbl := tableFrom(t, shared)
	if tbl.Len() != 1 {
		t.Fatalf("Len = %d, want 1 wire OID", tbl.Len())
	}
}

func TestTheShippedMappingResolvesTheLiebertCollision(t *testing.T) {
	// The real file, not a fixture: the generator has to keep emitting both
	// meanings with the device types that separate them.
	root := filepath.Join("..", "..", "..", "contracts", "mappings")
	tbl, err := LoadTraps(root)
	if err != nil {
		t.Skipf("shipped mapping not readable from here: %v", err)
	}
	got, ok := tbl.Lookup("1.3.6.1.4.1.476.1.42.3.3.0.5", "server")
	if !ok || got.EventType != "fan_failure" {
		t.Fatalf("shipped mapping: server got %q (ok=%v), want fan_failure",
			got.EventType, ok)
	}
}
