package mapping

import (
	"path/filepath"
	"testing"
)

// A Raritan strip reports its whole sensor port on ONE notification.
//
// A DPX2 probe has no processor and no address: the PX2 polls it over an RJ-12
// lead and publishes it, so a rack probe's temperature, humidity, airflow and
// smoke conditions all arrive as externalSensorStateChange FROM THE PDU.
// Twenty-three conditions on this plane ride that OID.
//
// With nothing to separate them the receiver took whichever entry the table
// listed first, and that was `airflowAlert`: a rack probe settling after a
// restart raised a MAJOR AIRFLOW alarm on a PDU, which reports no airflow at
// all, and the matching clear resolved to the same first entry so it could
// never close. The vendor's own discriminators are the sensor type and the
// state it moved to, and both are in the notification.

const raritanExtOID = "1.3.6.1.4.1.13742.6.0.66"

const (
	typeOfSensor  = "1.3.6.1.4.1.13742.6.0.0.10"
	externalState = "1.3.6.1.4.1.13742.6.5.5.3.1.3"
)

// PDU2-MIB sensor types and states, as the plane sends them.
const (
	sensorTemperature = "10"
	sensorHumidity    = "11"
	sensorAirFlow     = "12"
	sensorSmoke       = "18"

	stateBelowLowerWarning  = "3"
	stateNormal             = "4"
	stateAboveUpperWarning  = "5"
	stateAboveUpperCritical = "6"
	stateAlarmed            = "11"
)

func shippedTable(t *testing.T) *TrapTable {
	t.Helper()
	tbl, err := LoadTraps(filepath.Join("..", "..", "..", "contracts", "mappings"))
	if err != nil {
		t.Skipf("shipped mapping not readable from here: %v", err)
	}
	return tbl
}

func TestTheSensorPortIsResolvedByTypeAndState(t *testing.T) {
	tbl := shippedTable(t)

	cases := []struct {
		name          string
		sensor, state string
		wantEvent     string
		wantClear     bool
	}{
		// Every one of these used to resolve to airflow_alert.
		{"intake temperature high", sensorTemperature, stateAboveUpperWarning,
			"sensor_ambient_temp_high", false},
		{"intake temperature back to normal", sensorTemperature, stateNormal,
			"sensor_ambient_temp_high", true},
		{"intake temperature critical", sensorTemperature, stateAboveUpperCritical,
			"sensor_ambient_temp_critical", false},
		{"humidity high", sensorHumidity, stateAboveUpperWarning,
			"sensor_high_humidity", false},
		{"humidity back to normal", sensorHumidity, stateNormal,
			"sensor_high_humidity", true},
		{"humidity critical", sensorHumidity, stateAboveUpperCritical,
			"sensor_critical_humidity", false},
		{"humidity low", sensorHumidity, stateBelowLowerWarning,
			"sensor_low_humidity", false},
		// Airflow, which is what every condition above used to read as. Only a
		// probe that carries an airflow sensor reports this.
		{"airflow high", sensorAirFlow, stateAboveUpperWarning,
			"sensor_high_airflow", false},
		{"airflow back to normal", sensorAirFlow, stateNormal,
			"sensor_high_airflow", true},
		{"airflow low", sensorAirFlow, stateBelowLowerWarning,
			"sensor_low_airflow", false},
		{"smoke", sensorSmoke, stateAlarmed, "smoke_detected", false},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, ok := tbl.Lookup(raritanExtOID, "pdu", map[string]string{
				typeOfSensor:  c.sensor,
				externalState: c.state,
			})
			if !ok {
				t.Fatalf("%s: resolved nothing", c.name)
			}
			if got.EventType != c.wantEvent {
				t.Fatalf("%s: got %q, want %q", c.name, got.EventType, c.wantEvent)
			}
			if got.IsClear != c.wantClear {
				t.Fatalf("%s: is_clear=%v, want %v", c.name, got.IsClear, c.wantClear)
			}
		})
	}
}

func TestAProbeTemperatureIsNeverReadAsAirflow(t *testing.T) {
	// The reported defect, stated as the thing that must not happen again.
	tbl := shippedTable(t)
	for _, state := range []string{stateNormal, stateAboveUpperWarning, stateAboveUpperCritical} {
		got, ok := tbl.Lookup(raritanExtOID, "pdu", map[string]string{
			typeOfSensor:  sensorTemperature,
			externalState: state,
		})
		if !ok {
			t.Fatalf("state %s resolved nothing", state)
		}
		if got.EventType == "airflow_alert" {
			t.Fatalf("state %s: a temperature resolved to airflow_alert again", state)
		}
	}
}

func TestAClearReachesTheRaiseItEnds(t *testing.T) {
	// A raise and its clear ride the same OID and differ only in the state
	// varbind, so the pair has to resolve to ONE event type. When the intake
	// raise resolved to sensor_ambient_temp_high and the clear to a mid-rack
	// probe, the alarm stayed open with its all-clear already delivered.
	tbl := shippedTable(t)
	for _, sensor := range []string{sensorTemperature, sensorHumidity, sensorAirFlow} {
		raise, ok := tbl.Lookup(raritanExtOID, "pdu", map[string]string{
			typeOfSensor: sensor, externalState: stateAboveUpperWarning,
		})
		if !ok {
			t.Fatalf("sensor %s: the raise resolved nothing", sensor)
		}
		clear, ok := tbl.Lookup(raritanExtOID, "pdu", map[string]string{
			typeOfSensor: sensor, externalState: stateNormal,
		})
		if !ok {
			t.Fatalf("sensor %s: the clear resolved nothing", sensor)
		}
		if !clear.IsClear {
			t.Fatalf("sensor %s: the normal state did not resolve to a clear", sensor)
		}
		found := false
		for _, c := range clear.Clears {
			if c == raise.EventType {
				found = true
			}
		}
		if !found {
			t.Fatalf("sensor %s: raise is %q but its clear resolves %v",
				sensor, raise.EventType, clear.Clears)
		}
	}
}
