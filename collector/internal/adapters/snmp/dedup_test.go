package snmp

import (
	"testing"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

func TestTwoCopiesOfOneTrapShareAKey(t *testing.T) {
	// A device sending to two receivers lands the same notification on two
	// collectors, which stamp arrival times up to a second apart and may not
	// even agree on the source address.
	a := models.Event{DeviceID: "dev-1", EndpointID: "ep-1", SourceIP: "10.51.21.40",
		RawIdentifier: "1.3.6.1.4.1.13742.6.0.1", EventType: "pdu_overload",
		ObservedAt: 1_000_999_000}
	b := a
	b.SourceIP = "127.0.0.1"
	b.ObservedAt = 1_001_000_500

	if dedupKey(&a, "4711") != dedupKey(&b, "4711") {
		t.Fatal("two copies of one notification were kept as two")
	}
	if dedupKey(&a, "4711") == dedupKey(&a, "4712") {
		t.Fatal("the next notification collided with the last")
	}
}

func TestATrapWithoutUptimeKeepsTheArrivalKey(t *testing.T) {
	ev := models.Event{EndpointID: "ep-1", SourceIP: "10.51.21.40",
		EventType: "unknown_trap", ObservedAt: 5_000_000}
	later := ev
	later.ObservedAt = 7_000_000
	if dedupKey(&ev, "") == dedupKey(&later, "") {
		t.Fatal("without an uptime, arrival time must still tell traps apart")
	}
}
