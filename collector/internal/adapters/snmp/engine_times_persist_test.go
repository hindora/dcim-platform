package snmp

import (
	"testing"
	"time"
)

// The hole persistence closes: in memory only, the first message after a
// restart became the baseline, so a TRAP captured long before was accepted
// once per engine per restart.
func TestARestoredTableRefusesAnOldCapture(t *testing.T) {
	t0 := time.Now()
	live := NewEngineTimes()
	live.Accept("e", 4, 5000, t0) // a capture taken here...
	live.Accept("e", 4, 8000, t0.Add(50*time.Minute))

	restarted := NewEngineTimes()
	restarted.Import(live.Export())
	now := t0.Add(60 * time.Minute)
	if restarted.Accept("e", 4, 5000, now) {
		t.Fatal("a capture from before the restart was accepted after it")
	}
	if !restarted.Accept("e", 4, 8600, now) {
		t.Fatal("the sender's real, current trap was refused")
	}

	// What the same restart did without the table.
	if !NewEngineTimes().Accept("e", 4, 5000, now) {
		t.Fatal("an empty table should trust first sight - the behaviour replaced")
	}
}

// The sender's clock kept running while the collector was down; the restored
// notion of it must have run too. A message seen just before the downtime -
// 50 s behind the latest - is inside the window by the saved numbers alone;
// counting the 2 h that passed puts it far outside. (A message NEWER than the
// latest ever seen advances the notion and is accepted at any age: that is
// RFC 3414 3.2.7b, not something persistence can change.)
func TestDowntimeCountsTowardsTheSendersClock(t *testing.T) {
	t0 := time.Now()
	live := NewEngineTimes()
	live.Accept("e", 1, 950, t0.Add(-50*time.Second))
	live.Accept("e", 1, 1000, t0)
	restarted := NewEngineTimes()
	restarted.Import(live.Export())
	after := t0.Add(2 * time.Hour) // sender now ~8200
	if restarted.Accept("e", 1, 950, after) {
		t.Fatal("a message seen just before a 2 h downtime was accepted after it")
	}
	if !restarted.Accept("e", 1, 8150, after) {
		t.Fatal("a current message was refused")
	}
}

func TestImportNeverMovesAClockBack(t *testing.T) {
	t0 := time.Now()
	e := NewEngineTimes()
	e.Accept("e", 2, 900, t0)
	e.Import(map[string]EngineClockState{"e": {Boots: 2, Time: 100, Latest: 100, Learned: t0}})
	if e.Accept("e", 2, 500, t0) {
		t.Fatal("an older imported clock replaced a newer live one")
	}
}

func TestAClockSteppedBackIsNotStricter(t *testing.T) {
	// Learned "in the future" (a host clock that ran ahead, then corrected):
	// elapsed counts as zero, not negative.
	t0 := time.Now()
	e := NewEngineTimes()
	e.Import(map[string]EngineClockState{"e": {Boots: 1, Time: 1000, Latest: 1000,
		Learned: t0.Add(time.Hour)}})
	if !e.Accept("e", 1, 900, t0) {
		t.Fatal("a message 100 s behind was refused because the clock stepped back")
	}
}

func TestForgetLetsAFactoryResetDeviceBackIn(t *testing.T) {
	// Same engine ID, boots back to 1: RFC 3414 refuses it until boots
	// catch up. Forget is the way out.
	t0 := time.Now()
	e := NewEngineTimes()
	e.Accept("e", 57, 100, t0)
	if e.Accept("e", 1, 10, t0) {
		t.Fatal("lower boots accepted")
	}
	if !e.Forget("e") || e.Forget("e") {
		t.Fatal("Forget did not report what it knew")
	}
	if !e.Accept("e", 1, 10, t0) {
		t.Fatal("refused after Forget")
	}
}

func TestDirtyTracksUnsavedChanges(t *testing.T) {
	e := NewEngineTimes()
	if e.Dirty() {
		t.Fatal("new table dirty")
	}
	e.Accept("e", 1, 10, time.Now())
	if !e.Dirty() {
		t.Fatal("a learned engine left the table clean")
	}
	e.Export()
	if e.Dirty() {
		t.Fatal("Export did not clear the mark")
	}
	e.Accept("e", 1, 5, time.Now()) // older: no advance
	if e.Dirty() {
		t.Fatal("a message that advanced nothing dirtied the table")
	}
}
