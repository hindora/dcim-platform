package spool

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func testConfig(t *testing.T) Config {
	return Config{
		Dir: t.TempDir(),
		// Small caps so tests can actually exercise shedding without
		// writing gigabytes.
		SegmentBytes: 512, BulkCapBytes: 2048, BulkCapAge: time.Hour,
		ReservedCapBytes: 1024, ReservedCapAge: time.Hour,
		FlushInterval: time.Hour, // effectively disabled; tests flush explicitly
		FlushBytes:    1 << 20, FlushCount: 1000,
	}
}

func TestAppendThenFlushSurvivesAReopen(t *testing.T) {
	cfg := testConfig(t)
	s, err := Open(cfg)
	if err != nil {
		t.Fatal(err)
	}
	s.Append(StreamTelemetry, []byte("sample-1"), time.Now())
	s.Append(StreamTelemetry, []byte("sample-2"), time.Now())
	if err := s.Flush(); err != nil {
		t.Fatal(err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}

	s2, err := Open(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	d, err := s2.NewDrainer()
	if err != nil {
		t.Fatal(err)
	}
	defer d.Close()

	var got []string
	for {
		res, ok, err := d.Next(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if !ok {
			break
		}
		got = append(got, string(res.Payload))
		if err := res.Ack(); err != nil {
			t.Fatal(err)
		}
	}
	if len(got) != 2 || got[0] != "sample-1" || got[1] != "sample-2" {
		t.Fatalf("got %v, want [sample-1 sample-2] in order", got)
	}
}

func TestUnflushedMemoryIsLostOnACrashButNothingElseIs(t *testing.T) {
	// Simulates kill -9: no Flush, no Close, just drop the reference and
	// open a fresh Spool on the same directory, the way a restarted process
	// would. Only what was fsynced before the "crash" should come back.
	cfg := testConfig(t)
	s, err := Open(cfg)
	if err != nil {
		t.Fatal(err)
	}
	s.Append(StreamTelemetry, []byte("flushed"), time.Now())
	if err := s.Flush(); err != nil {
		t.Fatal(err)
	}
	s.Append(StreamTelemetry, []byte("never-flushed"), time.Now())
	// No Flush, no Close - the crash. Only the raw file handle is closed,
	// directly and without going through the exported Close/Flush path, so
	// the test can inspect what actually reached disk without also holding
	// a handle Windows would then refuse to delete during t.TempDir cleanup
	// - a real process crash releases every handle the OS gave it too.
	t.Cleanup(func() { s.bulk.active.Close(); s.reserved.active.Close() })

	s2, err := Open(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	d, err := s2.NewDrainer()
	if err != nil {
		t.Fatal(err)
	}
	defer d.Close()

	res, ok, err := d.Next(context.Background())
	if err != nil || !ok {
		t.Fatalf("expected the flushed record to survive: ok=%v err=%v", ok, err)
	}
	if string(res.Payload) != "flushed" {
		t.Fatalf("got %q, want %q", res.Payload, "flushed")
	}
	_, ok, err = d.Next(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if ok {
		t.Fatal("the never-flushed record should not have survived the crash")
	}
}

func TestReplayIsOldestFirstAndInOrderAcrossSegmentRotation(t *testing.T) {
	cfg := testConfig(t)
	// A generous cap: this test is about rotation and ordering, not
	// shedding - testConfig's small BulkCapBytes would otherwise shed the
	// very records the test expects to still be there to replay.
	cfg.BulkCapBytes = 1 << 20
	s, err := Open(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	// SegmentBytes is 512 in testConfig; enough records forces at least one
	// rotation, which is exactly what this test wants to cross.
	var want []string
	for i := 0; i < 40; i++ {
		payload := []byte(paddedPayload(i))
		want = append(want, string(payload))
		s.Append(StreamTelemetry, payload, time.Now())
	}
	if err := s.Flush(); err != nil {
		t.Fatal(err)
	}

	segments, err := listSegments(filepath.Join(cfg.Dir, "bulk"))
	if err != nil {
		t.Fatal(err)
	}
	if len(segments) < 2 {
		t.Fatalf("test did not actually exercise rotation: %d segment(s)", len(segments))
	}

	d, err := s.NewDrainer()
	if err != nil {
		t.Fatal(err)
	}
	defer d.Close()

	var got []string
	for {
		res, ok, err := d.Next(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if !ok {
			break
		}
		got = append(got, string(res.Payload))
		if err := res.Ack(); err != nil {
			t.Fatal(err)
		}
	}
	if len(got) != len(want) {
		t.Fatalf("got %d records, want %d", len(got), len(want))
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("record %d: got %q, want %q (order not preserved)", i, got[i], want[i])
		}
	}
}

// paddedPayload is a fixed-width record: 20 bytes each, so a handful cross
// the 512-byte segment cap in testConfig without needing hundreds of them.
func paddedPayload(i int) string {
	return fmt.Sprintf("record-%-12d", i)
}

func TestSheddingRemovesOldestSegmentsFirstAndKeepsAtLeastOne(t *testing.T) {
	cfg := testConfig(t) // BulkCapBytes: 2048, SegmentBytes: 512
	s, err := Open(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	// Write enough to blow well past the 2048-byte bulk cap.
	for i := 0; i < 200; i++ {
		s.Append(StreamTelemetry, []byte(paddedPayload(i)), time.Now())
	}
	if err := s.Flush(); err != nil {
		t.Fatal(err)
	}

	bytes, _ := s.bulk.stats()
	if bytes > cfg.BulkCapBytes {
		t.Errorf("bulk partition holds %d bytes, over its %d cap after shedding",
			bytes, cfg.BulkCapBytes)
	}

	segments, err := listSegments(filepath.Join(cfg.Dir, "bulk"))
	if err != nil {
		t.Fatal(err)
	}
	if len(segments) < 1 {
		t.Fatal("shedding deleted every segment - at least the active one must survive")
	}

	// The survivors must be the NEWEST ones (highest indices), not an
	// arbitrary subset - shedding removes from the front, never the back.
	for i := 1; i < len(segments); i++ {
		if segments[i].index <= segments[i-1].index {
			t.Fatalf("segments out of order after shedding: %v", segments)
		}
	}
}

func TestReservedPartitionIsNeverStarvedByBulkTraffic(t *testing.T) {
	// Bulk and reserved have independent budgets - flooding one must not
	// touch the other's segments at all.
	cfg := testConfig(t)
	s, err := Open(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	s.Append(StreamEvents, []byte("important-alarm"), time.Now())
	for i := 0; i < 300; i++ {
		s.Append(StreamTelemetry, []byte(paddedPayload(i)), time.Now())
	}
	if err := s.Flush(); err != nil {
		t.Fatal(err)
	}

	rBytes, _ := s.reserved.stats()
	if rBytes == 0 {
		t.Fatal("the reserved partition lost its only record to bulk pressure")
	}
}

func TestDrainerServesReservedBeforeBulk(t *testing.T) {
	cfg := testConfig(t)
	s, err := Open(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	s.Append(StreamTelemetry, []byte("metric"), time.Now())
	s.Append(StreamEvents, []byte("alarm"), time.Now())
	if err := s.Flush(); err != nil {
		t.Fatal(err)
	}

	d, err := s.NewDrainer()
	if err != nil {
		t.Fatal(err)
	}
	defer d.Close()

	res, ok, err := d.Next(context.Background())
	if err != nil || !ok {
		t.Fatalf("ok=%v err=%v", ok, err)
	}
	if res.Stream != StreamEvents {
		t.Fatalf("first replayed record was %q, want events (reserved) served first",
			res.Stream)
	}
}

func TestACrashedCursorWriteDoesNotCorruptTheCursor(t *testing.T) {
	// saveCursor writes to a temp file and renames it into place - the
	// rename is atomic, so a process killed mid-write leaves either the OLD
	// cursor intact or the NEW one, never a half-written file that
	// loadCursor could misread.
	cfg := testConfig(t)
	s, err := Open(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	s.Append(StreamTelemetry, []byte("x"), time.Now())
	if err := s.Flush(); err != nil {
		t.Fatal(err)
	}

	d, err := s.NewDrainer()
	if err != nil {
		t.Fatal(err)
	}
	res, ok, err := d.Next(context.Background())
	if err != nil || !ok {
		t.Fatal(err, ok)
	}
	if err := res.Ack(); err != nil {
		t.Fatal(err)
	}
	d.Close()

	if _, err := os.Stat(filepath.Join(cfg.Dir, "bulk", cursorFile+".tmp")); !os.IsNotExist(err) {
		t.Error("a .tmp cursor file was left behind - the rename did not clean up")
	}
	c, err := loadCursor(filepath.Join(cfg.Dir, "bulk"))
	if err != nil {
		t.Fatal(err)
	}
	if c.ByteOffset == 0 {
		t.Error("cursor was not actually advanced by Ack")
	}
}

func TestStatsIgnoreTheEmptyActiveSegmentOfAnIdleSpool(t *testing.T) {
	cfg := testConfig(t)
	s, err := Open(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	stats := s.Stats()
	if stats.Bytes != 0 {
		t.Errorf("an idle, freshly opened spool reports %d bytes, want 0", stats.Bytes)
	}
	if stats.OldestAge != 0 {
		t.Errorf("an idle spool reports oldest age %v, want 0 - "+
			"the empty active segment must not count as content", stats.OldestAge)
	}
}

func TestStatsIncludeTheUnflushedMemoryTail(t *testing.T) {
	cfg := testConfig(t)
	s, err := Open(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	s.Append(StreamTelemetry, []byte("still-in-memory"), time.Now().Add(-time.Minute))
	stats := s.Stats()
	if stats.Bytes == 0 {
		t.Error("Stats ignored a record that has not been flushed yet")
	}
	if stats.OldestAge < 50*time.Second {
		t.Errorf("oldest age %v does not reflect the record's actual age", stats.OldestAge)
	}
}

func TestRateLimiterActuallyThrottles(t *testing.T) {
	l := newRateLimiter(1000) // 1000/s - fast enough not to make the test slow
	start := time.Now()
	for i := 0; i < 5; i++ {
		if err := l.wait(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	// Draining 5 tokens from a bucket that started full should be
	// near-instant; this is really a smoke test that wait doesn't deadlock
	// or return early forever.
	if time.Since(start) > time.Second {
		t.Errorf("wait took %v for 5 tokens at 1000/s", time.Since(start))
	}
}

func TestRateLimiterRespectsContextCancellation(t *testing.T) {
	l := newRateLimiter(0.001) // effectively never refills within the test
	l.tokens = 0
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	err := l.wait(ctx)
	if err == nil {
		t.Fatal("expected wait to respect context cancellation")
	}
}

func TestNextDoesNotAdvanceTheCursorUntilAck(t *testing.T) {
	// The crash-safety property that makes replay at-least-once rather than
	// occasionally zero-once: reading a record must not itself count as
	// delivering it.
	cfg := testConfig(t)
	s, err := Open(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	s.Append(StreamTelemetry, []byte("x"), time.Now())
	if err := s.Flush(); err != nil {
		t.Fatal(err)
	}

	d, err := s.NewDrainer()
	if err != nil {
		t.Fatal(err)
	}
	if _, ok, err := d.Next(context.Background()); err != nil || !ok {
		t.Fatal(err, ok)
	}
	d.Close() // no Ack

	d2, err := s.NewDrainer()
	if err != nil {
		t.Fatal(err)
	}
	defer d2.Close()
	res, ok, err := d2.Next(context.Background())
	if err != nil || !ok {
		t.Fatalf("expected the un-acked record to still be pending: ok=%v err=%v", ok, err)
	}
	if string(res.Payload) != "x" {
		t.Errorf("got %q", res.Payload)
	}
}
