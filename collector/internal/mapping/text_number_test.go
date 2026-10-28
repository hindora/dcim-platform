package mapping

import "testing"

// PDU2-MIB's overCurrentProtectorRatedCurrent is a DisplayString ("16A").
// Read as a number it fails, and the breaker the bank is judged against goes
// missing - so the figure is read out of the text, only where a column asks.
func TestTextNumberReadsARatingWithItsUnit(t *testing.T) {
	tr := &Transform{NumberInText: true}
	for in, want := range map[string]float64{"16A": 16, "16 A": 16, " 20 ": 20, "12.5A": 12.5} {
		got, ok := tr.TextNumber(in)
		if !ok || got != want {
			t.Errorf("TextNumber(%q) = %v, %v; want %v", in, got, ok, want)
		}
	}
	for _, in := range []string{"", "C1", "16-20A", "A16", "sixteen"} {
		if _, ok := tr.TextNumber(in); ok {
			t.Errorf("TextNumber(%q) parsed; want refused", in)
		}
	}
}

func TestTextNumberIsOptIn(t *testing.T) {
	if _, ok := (&Transform{}).TextNumber("16A"); ok {
		t.Error("a column that did not ask must not read numbers out of text")
	}
	var nilTr *Transform
	if _, ok := nilTr.TextNumber("16A"); ok {
		t.Error("nil transform parsed text")
	}
}
