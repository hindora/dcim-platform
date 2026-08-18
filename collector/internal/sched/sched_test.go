package sched

import (
	"testing"
	"time"
)

func TestPhaseOffsetIsDeterministic(t *testing.T) {
	// A restart must land endpoints in the same slots, otherwise every deploy
	// re-thunders the whole fleet.
	a := phaseOffset("endpoint-abc", 30*time.Second)
	b := phaseOffset("endpoint-abc", 30*time.Second)
	if a != b {
		t.Fatalf("phase offset not deterministic: %v vs %v", a, b)
	}
}

func TestPhaseOffsetStaysWithinTheInterval(t *testing.T) {
	interval := 30 * time.Second
	for _, id := range []string{"a", "b", "c", "very-long-endpoint-uuid-0001"} {
		got := phaseOffset(id, interval)
		if got < 0 || got >= interval {
			t.Fatalf("offset %v for %q is outside [0, %v)", got, id, interval)
		}
	}
}

func TestPhaseOffsetSpreadsLoadAcrossTheInterval(t *testing.T) {
	// 664 endpoints on a 30 s schedule must fire ~22 per second, not 664 at
	// t=0. Assert no single second carries a wildly disproportionate share.
	const endpoints = 664
	interval := 30 * time.Second

	buckets := make(map[int]int)
	for i := 0; i < endpoints; i++ {
		id := "endpoint-" + string(rune('a'+i%26)) + "-" + itoa(i)
		bucket := int(phaseOffset(id, interval) / time.Second)
		buckets[bucket]++
	}

	if len(buckets) < 20 {
		t.Fatalf("offsets clustered into %d of 30 buckets; expected a broad spread",
			len(buckets))
	}
	worst := 0
	for _, n := range buckets {
		if n > worst {
			worst = n
		}
	}
	// Perfect spread is ~22 per bucket. Allow 3x before calling it clustered.
	if worst > endpoints/len(buckets)*3 {
		t.Fatalf("worst bucket holds %d endpoints; distribution is too uneven", worst)
	}
}

func TestPhaseOffsetHandlesSubSecondAndZeroIntervals(t *testing.T) {
	if got := phaseOffset("x", 0); got != 0 {
		t.Fatalf("zero interval should give zero offset, got %v", got)
	}
	// A streaming endpoint has interval 0 in its poll profile; it must not
	// panic or produce a negative modulus.
	if got := phaseOffset("x", 500*time.Millisecond); got != 0 {
		t.Fatalf("sub-second interval should give zero offset, got %v", got)
	}
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var buf []byte
	for i > 0 {
		buf = append([]byte{byte('0' + i%10)}, buf...)
		i /= 10
	}
	return string(buf)
}
