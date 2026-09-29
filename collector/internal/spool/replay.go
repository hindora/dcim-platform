package spool

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
	"time"
)

const cursorFile = ".cursor"

// cursor is a partition reader's durable position: which segment, and how
// many bytes into it have already been sent and acknowledged. Persisted as
// JSON (not msgpack, deliberately - this file is small, rare to write, and
// worth being readable with `cat` when a spool needs debugging by hand).
type cursor struct {
	SegmentIndex uint64 `json:"segment_index"`
	ByteOffset   int64  `json:"byte_offset"`
}

func loadCursor(dir string) (cursor, error) {
	raw, err := os.ReadFile(filepath.Join(dir, cursorFile))
	if os.IsNotExist(err) {
		return cursor{}, nil // never replayed from here: start at the oldest segment
	}
	if err != nil {
		return cursor{}, err
	}
	var c cursor
	if err := json.Unmarshal(raw, &c); err != nil {
		return cursor{}, nil // a corrupt cursor is treated as "start over", not fatal
	}
	return c, nil
}

func saveCursor(dir string, c cursor) error {
	raw, err := json.Marshal(c)
	if err != nil {
		return err
	}
	// Write to a temp file and rename: a crash mid-write must never leave a
	// half-written cursor that loadCursor then trusts, which is the classic
	// way a replay reader loses its place after the exact event - a
	// crash - it exists to be durable across.
	tmp := filepath.Join(dir, cursorFile+".tmp")
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, filepath.Join(dir, cursorFile))
}

// reader replays one partition's segments oldest-first from its persisted
// cursor, skipping whatever segments have already been sheared away by
// shed() - a gap the cursor cannot see past, and the honest outcome of
// budget pressure winning over history the reader has not gotten to yet.
type reader struct {
	dir string
	cur cursor
	f   *os.File
}

func openReader(dir string) (*reader, error) {
	c, err := loadCursor(dir)
	if err != nil {
		return nil, err
	}
	return &reader{dir: dir, cur: c}, nil
}

// next returns the following record, or (record{}, false, nil) if the
// reader has caught up to what is currently on disk. It does NOT advance
// the persisted cursor - see ack - so a crash between next and ack replays
// the same record again rather than skipping one that was never actually
// delivered.
func (r *reader) next() (record, bool, error) {
	segments, err := listSegments(r.dir)
	if err != nil {
		return record{}, false, err
	}
	// Catch the cursor up if its segment was shed out from under it: jump to
	// the oldest segment still on disk rather than waiting forever for a
	// file that is never coming back.
	if len(segments) > 0 && r.cur.SegmentIndex < segments[0].index {
		r.cur = cursor{SegmentIndex: segments[0].index, ByteOffset: 0}
		if r.f != nil {
			r.f.Close()
			r.f = nil
		}
	}

	for {
		if r.f == nil {
			path := segmentPath(r.dir, r.cur.SegmentIndex)
			f, err := os.Open(path)
			if os.IsNotExist(err) {
				return record{}, false, nil // nothing written this far yet
			}
			if err != nil {
				return record{}, false, err
			}
			if _, err := f.Seek(r.cur.ByteOffset, io.SeekStart); err != nil {
				f.Close()
				return record{}, false, err
			}
			r.f = f
		}

		rec, err := readRecord(r.f)
		if err == nil {
			return rec, true, nil
		}
		r.f.Close()
		r.f = nil
		if err != io.EOF && err != io.ErrUnexpectedEOF {
			return record{}, false, err
		}
		// End of this segment (clean, or a truncated tail from a crash mid-
		// write - either way there is nothing more to read from it). Move on
		// to the next index IF a newer segment actually exists; otherwise
		// this is the live tail and there is nothing to replay right now.
		next := r.cur.SegmentIndex + 1
		if _, statErr := os.Stat(segmentPath(r.dir, next)); statErr != nil {
			return record{}, false, nil
		}
		r.cur = cursor{SegmentIndex: next, ByteOffset: 0}
	}
}

// ack records that the record most recently returned by next was sent and
// accepted, advancing and persisting the cursor. byteLen is the number of
// framed bytes that record occupied on disk (its caller gets this from
// next's bookkeeping - see Spool.Drain), which is what lets ack move the
// offset without re-reading.
func (r *reader) ack(byteLen int64) error {
	r.cur.ByteOffset += byteLen
	return saveCursor(r.dir, r.cur)
}

func (r *reader) close() error {
	if r.f != nil {
		return r.f.Close()
	}
	return nil
}

// rateLimiter is a small token bucket: replay must not exceed roughly 3x
// live rate (docs/26 Phase 3), so a catch-up burst cannot starve ingest of
// capacity it also needs for whatever is arriving live on the same
// connection.
type rateLimiter struct {
	mu           sync.Mutex
	tokens       float64
	max          float64
	refillPerSec float64
	last         time.Time
}

func newRateLimiter(perSecond float64) *rateLimiter {
	if perSecond <= 0 {
		perSecond = 1
	}
	return &rateLimiter{tokens: perSecond, max: perSecond,
		refillPerSec: perSecond, last: time.Now()}
}

func (l *rateLimiter) setRate(perSecond float64) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if perSecond <= 0 {
		perSecond = 1
	}
	l.refillPerSec = perSecond
	l.max = perSecond
	if l.tokens > l.max {
		l.tokens = l.max
	}
}

// wait blocks until a token is available or ctx is done.
func (l *rateLimiter) wait(ctx context.Context) error {
	for {
		l.mu.Lock()
		now := time.Now()
		elapsed := now.Sub(l.last).Seconds()
		l.tokens += elapsed * l.refillPerSec
		if l.tokens > l.max {
			l.tokens = l.max
		}
		l.last = now
		if l.tokens >= 1 {
			l.tokens--
			l.mu.Unlock()
			return nil
		}
		wait := time.Duration((1 - l.tokens) / l.refillPerSec * float64(time.Second))
		l.mu.Unlock()
		if wait <= 0 {
			wait = time.Millisecond
		}
		t := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			t.Stop()
			return ctx.Err()
		case <-t.C:
		}
	}
}

// DrainResult is one record ready to send, with what ack needs to advance
// past it once the caller has confirmed delivery.
type DrainResult struct {
	Stream  string
	Seq     uint64
	At      time.Time
	Payload []byte

	ack func() error
}

// Ack confirms this record was delivered and durably advances the replay
// cursor past it. Call it only after the gateway has returned success - see
// its own docstring on next/ack for why calling it before that risks
// silently skipping a record a crash prevented from ever being sent.
func (d DrainResult) Ack() error { return d.ack() }

// Drainer replays both partitions, reserved first (see Spool.NewDrainer),
// each independently rate-limited to its own estimate of 3x recent live
// rate.
type Drainer struct {
	bulkReader, reservedReader   *reader
	bulkLimiter, reservedLimiter *rateLimiter
}

// NewDrainer opens replay readers for both partitions. Reserved (events,
// endpoint-state) is drained to exhaustion before bulk (telemetry) is
// touched at all: it is the partition Phase 3 protects hardest against
// shedding, on the reasoning that the alarms it carries are worth more per
// byte than a metric series with a gap in it, and prioritising its delivery
// on reconnect is the other half of that same judgement.
func (s *Spool) NewDrainer() (*Drainer, error) {
	br, err := openReader(s.bulk.dir)
	if err != nil {
		return nil, fmt.Errorf("open bulk reader: %w", err)
	}
	rr, err := openReader(s.reserved.dir)
	if err != nil {
		br.close()
		return nil, fmt.Errorf("open reserved reader: %w", err)
	}
	return &Drainer{bulkReader: br, reservedReader: rr,
		bulkLimiter: newRateLimiter(30), reservedLimiter: newRateLimiter(30)}, nil
}

// SetLiveRate updates both partitions' replay ceiling to 3x the given
// records-per-second estimate of current live production. Call this
// periodically from whatever is measuring live throughput; a Drainer with
// no estimate yet replays at a conservative default rather than unthrottled.
func (d *Drainer) SetLiveRate(bulkPerSec, reservedPerSec float64) {
	d.bulkLimiter.setRate(bulkPerSec * 3)
	d.reservedLimiter.setRate(reservedPerSec * 3)
}

// Next blocks for its rate limiter's turn and returns the next record to
// replay, reserved-partition backlog first. Returns ok=false once both
// partitions have caught up to their live write position - not an error,
// the ordinary "nothing left to replay right now" state.
func (d *Drainer) Next(ctx context.Context) (DrainResult, bool, error) {
	if res, ok, err := d.nextFrom(ctx, d.reservedReader, d.reservedLimiter); ok || err != nil {
		return res, ok, err
	}
	return d.nextFrom(ctx, d.bulkReader, d.bulkLimiter)
}

func (d *Drainer) nextFrom(ctx context.Context, r *reader, limiter *rateLimiter,
) (DrainResult, bool, error) {
	rec, ok, err := r.next()
	if err != nil || !ok {
		return DrainResult{}, false, err
	}
	if err := limiter.wait(ctx); err != nil {
		return DrainResult{}, false, err
	}
	framed, encErr := encodeRecord(rec)
	if encErr != nil {
		return DrainResult{}, false, encErr
	}
	return DrainResult{
		Stream: rec.Stream, Seq: rec.Seq,
		At: time.Unix(0, rec.At), Payload: rec.Payload,
		ack: func() error { return r.ack(int64(len(framed))) },
	}, true, nil
}

func (d *Drainer) Close() error {
	err1 := d.bulkReader.close()
	err2 := d.reservedReader.close()
	if err1 != nil {
		return err1
	}
	return err2
}
