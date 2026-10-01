package app

import (
	"testing"
	"time"
)

func TestHandoffWindowHasAFloorAndScalesWithTheBatch(t *testing.T) {
	cases := map[int]time.Duration{1: 15 * time.Second, 95: 15 * time.Second,
		300: 15 * time.Second, 437: 21850 * time.Millisecond, 2000: 100 * time.Second}
	for n, want := range cases {
		if got := handoffWindow(n); got != want {
			t.Errorf("handoffWindow(%d) = %v, want %v", n, got, want)
		}
	}
}
