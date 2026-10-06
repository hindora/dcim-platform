package app

import (
	"io"
	"log/slog"
	"testing"
)

// Boots goes up on every start and survives the process: the INFORM replay
// window depends on (boots, time) never repeating.
func TestEngineBootsIncreaseAcrossStarts(t *testing.T) {
	dir := t.TempDir()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	for want := uint32(1); want <= 3; want++ {
		if got := nextEngineBoots(dir, log); got != want {
			t.Fatalf("start %d: boots %d", want, got)
		}
	}
}
