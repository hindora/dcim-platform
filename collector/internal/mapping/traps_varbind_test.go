package mapping

import (
	"path/filepath"
	"testing"
)

// When the OID cannot say which condition it is, the vendor puts it in a
// varbind - and that is the discriminator the vendor intended.
//
// Liebert sends ONE notification, lgpEventConditionAdded, for airflow, dew
// point, frequency, humidity, input voltage and temperature alike, with the
// condition in lgpConditionDescr. Cisco sends one environmental notification
// for a warning and for a critical, with the state in an enumeration. Raritan
// needs two varbinds to say anything: an inlet sensor notification carries the
// sensor type AND the state it moved to, so "current, above upper warning" is
// a load alarm and "current, above upper critical" is the critical one - same
// OID, same PDU.
//
// Nothing above the varbind can separate those. Device type cannot: it is one
// kind of equipment sending both.

const liebertShared = `
version: 1
traps:
  - oid: 1.3.6.1.4.1.476.1.42.3.3.0.1
    event_type: humidity_alert
    severity: MAJOR
    match_varbinds:
      - oid: 1.3.6.1.4.1.476.1.42.3.2.3.1.1
        contains: "Humidity Alert"
  - oid: 1.3.6.1.4.1.476.1.42.3.3.0.1
    event_type: airflow_alert
    severity: MAJOR
    match_varbinds:
      - oid: 1.3.6.1.4.1.476.1.42.3.2.3.1.1
        contains: "Airflow Alert"
  - oid: 1.3.6.1.4.1.476.1.42.3.3.0.1
    event_type: temperature_alert
    severity: MAJOR
`

func TestTheDescriptionVarbindPicksTheCondition(t *testing.T) {
	tbl := tableFrom(t, liebertShared)
	descr := "1.3.6.1.4.1.476.1.42.3.2.3.1.1"

	// The real payload: "<device>: <condition> (<value>)".
	got, ok := tbl.Lookup("1.3.6.1.4.1.476.1.42.3.3.0.1", "crah",
		map[string]string{descr: "CRAH1-DC1-HA-R9-01: Humidity Alert (72.4)"})
	if !ok || got.EventType != "humidity_alert" {
		t.Fatalf("got %q (ok=%v), want humidity_alert", got.EventType, ok)
	}

	got, _ = tbl.Lookup("1.3.6.1.4.1.476.1.42.3.3.0.1", "crah",
		map[string]string{descr: "CRAH1-DC1-HA-R9-01: Airflow Alert"})
	if got.EventType != "airflow_alert" {
		t.Fatalf("got %q, want airflow_alert", got.EventType)
	}
}

func TestAMissingVarbindFallsBackRatherThanGuessing(t *testing.T) {
	// Silence is not agreement. Treating an absent varbind as a match would
	// resolve every shared OID to whichever meaning happened to be listed
	// first, which is the behaviour this replaced.
	tbl := tableFrom(t, liebertShared)
	got, ok := tbl.Lookup("1.3.6.1.4.1.476.1.42.3.3.0.1", "crah", nil)
	if !ok || got.EventType != "temperature_alert" {
		t.Fatalf("got %q (ok=%v), want the general meaning", got.EventType, ok)
	}
}

func TestAnUnrecognisedConditionFallsBackToTheGeneralMeaning(t *testing.T) {
	tbl := tableFrom(t, liebertShared)
	got, _ := tbl.Lookup("1.3.6.1.4.1.476.1.42.3.3.0.1", "crah",
		map[string]string{
			"1.3.6.1.4.1.476.1.42.3.2.3.1.1": "CRAH1: Something Nobody Mapped",
		})
	if got.EventType != "temperature_alert" {
		t.Fatalf("got %q, want the general meaning", got.EventType)
	}
}

const raritanPair = `
version: 1
traps:
  - oid: 1.3.6.1.4.1.13742.6.0.61
    event_type: pdu_load_high
    severity: MAJOR
    match_varbinds:
      - oid: 1.3.6.1.4.1.13742.6.0.0.10
        equals_int: 1
      - oid: 1.3.6.1.4.1.13742.6.5.2.3.1.3
        equals_int: 5
  - oid: 1.3.6.1.4.1.13742.6.0.61
    event_type: pdu_load_critical
    severity: CRITICAL
    match_varbinds:
      - oid: 1.3.6.1.4.1.13742.6.0.0.10
        equals_int: 1
      - oid: 1.3.6.1.4.1.13742.6.5.2.3.1.3
        equals_int: 6
`

func TestBothVarbindsMustHold(t *testing.T) {
	tbl := tableFrom(t, raritanPair)
	const (
		sensorOID = "1.3.6.1.4.1.13742.6.0.0.10"
		stateOID  = "1.3.6.1.4.1.13742.6.5.2.3.1.3"
	)

	got, ok := tbl.Lookup("1.3.6.1.4.1.13742.6.0.61", "pdu",
		map[string]string{sensorOID: "1", stateOID: "5"})
	if !ok || got.EventType != "pdu_load_high" {
		t.Fatalf("warning state: got %q (ok=%v), want pdu_load_high", got.EventType, ok)
	}

	got, ok = tbl.Lookup("1.3.6.1.4.1.13742.6.0.61", "pdu",
		map[string]string{sensorOID: "1", stateOID: "6"})
	if !ok || got.EventType != "pdu_load_critical" {
		t.Fatalf("critical state: got %q (ok=%v), want pdu_load_critical", got.EventType, ok)
	}

	// Right sensor, a state neither entry claims: no match at all rather than
	// half a match. Reporting the raw OID beats inventing a severity.
	if _, ok := tbl.Lookup("1.3.6.1.4.1.13742.6.0.61", "pdu",
		map[string]string{sensorOID: "1", stateOID: "4"}); ok {
		t.Fatal("a state nobody mapped resolved anyway")
	}

	// Right state, wrong sensor - a voltage reading must not become a load
	// alarm because the state number happens to line up.
	if _, ok := tbl.Lookup("1.3.6.1.4.1.13742.6.0.61", "pdu",
		map[string]string{sensorOID: "4", stateOID: "5"}); ok {
		t.Fatal("a voltage sensor resolved to a current alarm")
	}
}

func TestTheShippedMappingSeparatesTheLiebertConditions(t *testing.T) {
	// The generated file, not a fixture. 476.1.42.3.3.0.1 carries seven
	// conditions on this plane and every one of them has to land separately.
	tbl, err := LoadTraps(filepath.Join("..", "..", "..", "contracts", "mappings"))
	if err != nil {
		t.Skipf("shipped mapping not readable from here: %v", err)
	}
	descr := "1.3.6.1.4.1.476.1.42.3.2.3.1.1"
	for phrase, want := range map[string]string{
		"Humidity Alert": "humidity_alert",
		"Airflow Alert":  "airflow_alert",
	} {
		got, ok := tbl.Lookup("1.3.6.1.4.1.476.1.42.3.3.0.1", "crah",
			map[string]string{descr: "CRAH1-DC1-HA-R9-01: " + phrase})
		if !ok || got.EventType != want {
			t.Errorf("%q -> %q (ok=%v), want %q", phrase, got.EventType, ok, want)
		}
	}
}
