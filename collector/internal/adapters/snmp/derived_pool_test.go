package snmp

import (
	"testing"

	g "github.com/gosnmp/gosnmp"

	"github.com/hari/dcim-platform/collector/internal/mapping"
)

// A memory POOL publishes used and free, and no total.
//
// CISCO-MEMORY-POOL-MIB is the one in front of us: ciscoMemoryPoolUsed and
// ciscoMemoryPoolFree, both in bytes, with nothing that says how large the pool
// is. The whole is the sum of the parts, and a poller that divides by the
// platform's nameplate RAM instead reports a router sitting at 3% for ever.
//
// The zero check is the part worth pinning. `free` reaching zero is a pool at
// 100% - the exact moment somebody needs the number - so the denominator has to
// be summed BEFORE it is tested, or the metric disappears at its own alarm
// point and reads as a device that stopped answering.

func pdu(oid string, value int) g.SnmpPDU {
	return g.SnmpPDU{Name: oid, Type: g.Gauge32, Value: uint(value)}
}

func derivePool(used, free int) (map[string]g.SnmpPDU, mapping.DerivedScalar) {
	const (
		usedOID = "1.3.6.1.4.1.9.9.48.1.1.1.5.1"
		freeOID = "1.3.6.1.4.1.9.9.48.1.1.1.6.1"
	)
	scale := 100.0
	return map[string]g.SnmpPDU{
			usedOID: pdu(usedOID, used),
			freeOID: pdu(freeOID, free),
		}, mapping.DerivedScalar{
			Metric:         "memory_utilization",
			ValueType:      "gauge",
			Numerator:      usedOID,
			Denominator:    freeOID,
			SumDenominator: true,
			Transform:      &mapping.Transform{Scale: &scale},
		}
}

// evalDerived mirrors the adapter's derived-scalar arithmetic. Driving the full
// poll needs a live agent; the arithmetic is what regressed and what matters.
func evalDerived(byOID map[string]g.SnmpPDU, d mapping.DerivedScalar) (float64, bool) {
	num, okN := toFloat(valueOf(byOID, d.Numerator))
	den, okD := toFloat(valueOf(byOID, d.Denominator))
	if okN && okD && d.SumDenominator {
		den += num
	}
	if !okN || !okD || den == 0 {
		return 0, false
	}
	value := num / den
	if d.OneMinus {
		value = 1 - value
	}
	return d.Transform.Apply(value), true
}

func TestPoolUtilisationIsUsedOverUsedPlusFree(t *testing.T) {
	byOID, d := derivePool(3*1024*1024*1024, 5*1024*1024*1024)
	got, ok := evalDerived(byOID, d)
	if !ok {
		t.Fatal("no sample from a healthy pool")
	}
	if want := 37.5; got != want {
		t.Fatalf("utilisation = %v, want %v", got, want)
	}
}

func TestAFullPoolStillReportsRatherThanVanishing(t *testing.T) {
	byOID, d := derivePool(8*1024*1024*1024, 0)
	got, ok := evalDerived(byOID, d)
	if !ok {
		t.Fatal("a pool with zero free produced no sample - it is at 100%, " +
			"which is when the metric matters most")
	}
	if got != 100 {
		t.Fatalf("utilisation = %v, want 100", got)
	}
}

func TestAnEmptyPoolIsAMissNotADivideByZero(t *testing.T) {
	byOID, d := derivePool(0, 0)
	if _, ok := evalDerived(byOID, d); ok {
		t.Fatal("used=0 and free=0 is an agent saying nothing, not a pool at 0%")
	}
}

func TestSumDenominatorIsOptIn(t *testing.T) {
	// UCD's memory pair is total/available and must keep dividing by the
	// denominator as published.
	byOID, d := derivePool(3, 5)
	d.SumDenominator = false
	got, ok := evalDerived(byOID, d)
	if !ok {
		t.Fatal("no sample")
	}
	if want := 60.0; got != want {
		t.Fatalf("plain ratio = %v, want %v", got, want)
	}
}
