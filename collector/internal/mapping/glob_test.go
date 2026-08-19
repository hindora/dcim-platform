package mapping

import "testing"

// A wildcard in the MIDDLE of a pattern was silently unmatchable, which meant a
// 42-circuit power panel yielded its seven panel totals and none of its 210
// branch-circuit points. Nothing errored - the points were discovered, counted
// as unmapped, and never polled.
func TestGlobMatchWildcardInTheMiddle(t *testing.T) {
	cases := []struct {
		pattern, subject string
		want             bool
	}{
		{"Ckt*_Current", "Ckt01_Current", true},
		{"Ckt*_Current", "Ckt42_Current", true},
		{"Ckt*_Current", "Ckt01_kW", false},
		{"Ckt*_kWh", "Ckt07_kWh", true},
		{"Harmonic_*_Current", "Harmonic_3_Current", true},
		{"Harmonic_*_Current", "Harmonic_11_Current", true},
		{"Harmonic_*_Current", "Harmonic_3_Voltage", false},

		// The forms that already worked must keep working.
		{"Alarm_*", "Alarm_HighPressure", true},
		{"Alarm_*", "Unit_Running", false},
		{"*Inlet*", "System Board Inlet Temp", true},
		{"*Temp", "CPU1 Temp", true},
		{"*Temp", "CPU1 Temp Sensor", false},
		{"CPU1 Temp", "cpu1 temp", true},
		{"CPU1 Temp", "CPU2 Temp", false},
		{"*", "anything", true},

		// A wildcard may match nothing at all.
		{"Ckt*_Current", "Ckt_Current", true},
		// Segments must appear in order and must not overlap.
		{"a*b*c", "axxbxxc", true},
		{"a*b*c", "acb", false},
	}
	for _, c := range cases {
		if got := globMatch(c.pattern, c.subject); got != c.want {
			t.Errorf("globMatch(%q, %q) = %v, want %v",
				c.pattern, c.subject, got, c.want)
		}
	}
}
