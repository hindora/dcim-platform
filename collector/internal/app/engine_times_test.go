package app

import (
	"io"
	"log/slog"
	"os"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/adapters/snmp"
)

// A real engine ID: RFC 3411 format, bytes that are not UTF-8. JSON keys
// would have mangled it; the file stores hex.
const binaryEngine = "\x80\x00\x01\x3e\x01\x0a\x34\x0b\xff\xfe"

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func TestSenderClocksSurviveARestart(t *testing.T) {
	dir := t.TempDir()
	t0 := time.Now()
	live := snmp.NewEngineTimes()
	live.Accept(binaryEngine, 3, 5000, t0)
	live.Accept(binaryEngine, 3, 8000, t0.Add(50*time.Minute))
	saveEngineTimes(dir, live, quiet())

	restarted := snmp.NewEngineTimes()
	loadEngineTimes(dir, restarted, quiet())
	if restarted.Len() != 1 {
		t.Fatalf("restored %d engines, want 1", restarted.Len())
	}
	if restarted.Accept(binaryEngine, 3, 5000, t0.Add(60*time.Minute)) {
		t.Fatal("a pre-restart capture was accepted: the engine ID did not survive the file")
	}
}

func TestNothingChangedNothingWritten(t *testing.T) {
	dir := t.TempDir()
	saveEngineTimes(dir, snmp.NewEngineTimes(), quiet())
	if _, err := os.Stat(engineTimesPath(dir)); !os.IsNotExist(err) {
		t.Fatal("an unchanged table was written")
	}
}

func TestAnUnreadableFileStartsEmptyRatherThanFailing(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(engineTimesPath(dir), []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	e := snmp.NewEngineTimes()
	loadEngineTimes(dir, e, quiet())
	if e.Len() != 0 {
		t.Fatal("garbage restored as engines")
	}
}

func TestForgetEngineEditsTheSavedTable(t *testing.T) {
	dir := t.TempDir()
	e := snmp.NewEngineTimes()
	e.Accept(binaryEngine, 57, 100, time.Now())
	e.Accept("\x80\x00\x00\x09\x03other", 2, 100, time.Now())
	saveEngineTimes(dir, e, quiet())

	known, err := ForgetEngine(dir, "8000013e010a340bfffe")
	if err != nil || !known {
		t.Fatalf("forget: known=%v err=%v", known, err)
	}
	again, err := ForgetEngine(dir, "8000013e010a340bfffe")
	if err != nil || again {
		t.Fatalf("second forget: known=%v err=%v", again, err)
	}
	if _, err := ForgetEngine(dir, "not-hex"); err == nil {
		t.Fatal("a non-hex engine ID was accepted")
	}
	after := snmp.NewEngineTimes()
	loadEngineTimes(dir, after, quiet())
	if after.Len() != 1 {
		t.Fatalf("%d engines left, want the other one", after.Len())
	}
	if !after.Accept(binaryEngine, 1, 10, time.Now()) {
		t.Fatal("the forgotten engine was still refused")
	}
}
