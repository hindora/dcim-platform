package snmp

import (
	"testing"

	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

func TestToFloatAcceptsEverySNMPNumericType(t *testing.T) {
	// gosnmp hands back whichever Go type fits the ASN.1 value, so the
	// conversion has to cover all of them or metrics silently vanish.
	cases := []struct {
		in   any
		want float64
	}{
		{int(42), 42},
		{int32(42), 42},
		{int64(42), 42},
		{uint(42), 42},
		{uint32(42), 42},
		{uint64(42), 42},
		{float32(1.5), 1.5},
		{float64(1.5), 1.5},
		{"42", 42},
		{[]byte("42"), 42},
	}
	for _, c := range cases {
		got, ok := toFloat(c.in)
		if !ok || got != c.want {
			t.Fatalf("toFloat(%T %v) = %v, %v; want %v, true", c.in, c.in, got, ok, c.want)
		}
	}
	if _, ok := toFloat(struct{}{}); ok {
		t.Fatal("toFloat accepted a struct")
	}
	if _, ok := toFloat("not a number"); ok {
		t.Fatal("toFloat accepted a non-numeric string")
	}
}

func TestToUintRejectsNegatives(t *testing.T) {
	if _, ok := toUint(-1); ok {
		t.Fatal("toUint accepted a negative counter value")
	}
	got, ok := toUint(int64(1 << 40))
	if !ok || got != 1<<40 {
		t.Fatalf("toUint = %v, %v; want %v, true", got, ok, uint64(1<<40))
	}
}

func TestReduceModes(t *testing.T) {
	values := []float64{10, 20, 60}
	cases := map[string]float64{
		"avg": 30,
		"max": 60,
		"min": 10,
		"sum": 90,
		"":    30, // default is avg
	}
	for mode, want := range cases {
		if got := reduce(mode, values); got != want {
			t.Fatalf("reduce(%q) = %v, want %v", mode, got, want)
		}
	}
	if got := reduce("avg", nil); got != 0 {
		t.Fatalf("reduce over no rows = %v, want 0", got)
	}
}

func TestRowMatchesFilters(t *testing.T) {
	row := map[string]any{
		"1.3.6.1.2.1.25.2.3.1.2": ".1.3.6.1.2.1.25.2.1.2", // hrStorageRam
		"1.3.6.1.2.1.99.1.1.1.1": 8,                       // celsius
	}

	// No filter keeps every row.
	if !rowMatches(nil, row) {
		t.Fatal("nil filter rejected a row")
	}

	// OID equality, tolerant of the leading dot gosnmp may or may not include.
	ram := &mapping.RowFilter{
		OID:    "1.3.6.1.2.1.25.2.3.1.2",
		Equals: "1.3.6.1.2.1.25.2.1.2",
	}
	if !rowMatches(ram, row) {
		t.Fatal("leading-dot mismatch rejected a matching row")
	}

	disk := &mapping.RowFilter{
		OID:    "1.3.6.1.2.1.25.2.3.1.2",
		Equals: "1.3.6.1.2.1.25.2.1.4",
	}
	if rowMatches(disk, row) {
		t.Fatal("a RAM row matched the fixed-disk filter")
	}

	eight := int64(8)
	celsius := &mapping.RowFilter{OID: "1.3.6.1.2.1.99.1.1.1.1", EqualsInt: &eight}
	if !rowMatches(celsius, row) {
		t.Fatal("integer filter failed to match")
	}

	missing := &mapping.RowFilter{OID: "1.2.3.4", Equals: "x"}
	if rowMatches(missing, row) {
		t.Fatal("filter matched a row lacking the filter column")
	}
}

func TestQualityMarksOutOfRangeAsSuspectNotDropped(t *testing.T) {
	// The value is evidence. Hiding it makes a sensor fault look like a data
	// gap, which sends an operator looking in the wrong place.
	def, ok := models.ValidateMetric("cpu_utilization")
	if !ok {
		t.Fatal("cpu_utilization missing from the registry")
	}

	if got := quality(def, 50); got != models.QualityGood {
		t.Fatalf("in-range value got %v, want GOOD", got)
	}
	if got := quality(def, 150); got != models.QualitySuspect {
		t.Fatalf("above max got %v, want SUSPECT", got)
	}
	if got := quality(def, -1); got != models.QualitySuspect {
		t.Fatalf("below min got %v, want SUSPECT", got)
	}
}

func TestTransformApplyAndBool(t *testing.T) {
	scale := 0.01
	tr := &mapping.Transform{Scale: &scale}
	if got := tr.Apply(12345); got != 123.45 {
		t.Fatalf("scale gave %v, want 123.45", got)
	}

	// A nil transform must be an identity, not a panic: most columns have none.
	var nilTr *mapping.Transform
	if got := nilTr.Apply(7); got != 7 {
		t.Fatalf("nil transform changed the value to %v", got)
	}

	// ifOperStatus: 1 = up, everything else down.
	up := &mapping.Transform{EnumTrue: []int64{1}}
	if !up.Bool(1) {
		t.Fatal("ifOperStatus 1 should be true")
	}
	if up.Bool(2) {
		t.Fatal("ifOperStatus 2 (down) should be false")
	}
	if !nilTr.Bool(1) || nilTr.Bool(0) {
		t.Fatal("nil transform should fall back to non-zero == true")
	}
}

func TestEmptyCommunityIsAnAuthErrorNotADefault(t *testing.T) {
	// With a wildcard-listener agent plane an empty community is a guaranteed
	// silent drop, so it must fail loudly rather than fall back to "public".
	a := New(nil, nil, nil, 25)
	ep := &models.Endpoint{ID: "ep-1", Protocol: "snmp", Address: "10.0.0.1"}

	_, err := a.Poll(nil, ep) //nolint:staticcheck // nil ctx never reached
	if err == nil {
		t.Fatal("poll with no community succeeded")
	}
	if models.ClassifyError(err) != models.ErrClassAuth {
		t.Fatalf("error class %q, want auth", models.ClassifyError(err))
	}
}
