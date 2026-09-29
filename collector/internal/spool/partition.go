package spool

import (
	"fmt"
	"os"
	"time"
)

// partition is one on-disk segmented log - "bulk" (telemetry, shed first)
// or "reserved" (events and endpoint-state, shed last - see spool.go for
// why those two get their own budget rather than competing with telemetry
// for the same one).
type partition struct {
	dir  string
	name string

	capBytes    int64
	capAge      time.Duration
	segMaxBytes int64

	active      *os.File
	activeIndex uint64
	activeBytes int64
	nextSeq     uint64
}

func openPartition(dir, name string, capBytes int64, capAge time.Duration,
	segMaxBytes int64) (*partition, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, fmt.Errorf("create %s partition dir: %w", name, err)
	}
	segments, err := listSegments(dir)
	if err != nil {
		return nil, err
	}

	p := &partition{dir: dir, name: name, capBytes: capBytes, capAge: capAge,
		segMaxBytes: segMaxBytes}

	if len(segments) == 0 {
		if err := p.rotate(0); err != nil {
			return nil, err
		}
		return p, nil
	}

	// Recover nextSeq by scanning the newest segment - the only one that
	// could hold a partial write from a crash, and the only one that tells
	// us where sequence numbers actually left off. Older segments are never
	// reopened for writing.
	newest := segments[len(segments)-1]
	maxSeq, err := scanMaxSeq(segmentPath(dir, newest.index))
	if err != nil {
		return nil, fmt.Errorf("recover %s partition: %w", name, err)
	}
	p.nextSeq = maxSeq + 1

	f, err := os.OpenFile(segmentPath(dir, newest.index), os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return nil, fmt.Errorf("reopen %s partition segment: %w", name, err)
	}
	p.active = f
	p.activeIndex = newest.index
	p.activeBytes = newest.bytes
	return p, nil
}

func scanMaxSeq(path string) (uint64, error) {
	f, err := os.Open(path)
	if os.IsNotExist(err) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	defer f.Close()
	var max uint64
	for {
		rec, err := readRecord(f)
		if err != nil {
			break // io.EOF, or a truncated tail - either way, done scanning
		}
		if rec.Seq > max {
			max = rec.Seq
		}
	}
	return max, nil
}

func (p *partition) rotate(index uint64) error {
	if p.active != nil {
		if err := p.active.Close(); err != nil {
			return fmt.Errorf("close %s partition segment: %w", p.name, err)
		}
	}
	f, err := os.OpenFile(segmentPath(p.dir, index), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return fmt.Errorf("create %s partition segment: %w", p.name, err)
	}
	p.active = f
	p.activeIndex = index
	p.activeBytes = 0
	return nil
}

// append assigns each record the next sequence number and writes it,
// rotating to a new segment BETWEEN records whenever the next one would
// push the active segment over its size limit - never partway through a
// batch that was already checked once, which is what let a single flush of
// many small records land in one oversized segment that shedding could
// then never touch (shed refuses to delete the only segment there is).
// One fsync covers the whole batch; only the durability boundary is
// per-flush, not the size boundary.
func (p *partition) append(batches []pending) error {
	if len(batches) == 0 {
		return nil
	}
	for _, b := range batches {
		p.nextSeq++
		framed, err := encodeRecord(record{Seq: p.nextSeq, Stream: b.stream,
			At: b.at.UnixNano(), Payload: b.payload})
		if err != nil {
			return err
		}
		if p.activeBytes > 0 && p.activeBytes+int64(len(framed)) > p.segMaxBytes {
			if err := p.rotate(p.activeIndex + 1); err != nil {
				return err
			}
		}
		n, err := p.active.Write(framed)
		if err != nil {
			return fmt.Errorf("write %s partition segment: %w", p.name, err)
		}
		p.activeBytes += int64(n)
	}
	if err := p.active.Sync(); err != nil {
		return fmt.Errorf("fsync %s partition segment: %w", p.name, err)
	}
	return nil
}

// pending is one record on its way into a partition - the caller's view,
// before this package assigns it a sequence number.
type pending struct {
	stream  string
	at      time.Time
	payload []byte
}

// shed deletes whole segments, oldest first, until the partition is back
// within its byte and age budget. The active (newest) segment is never
// deleted - there is always at least one segment left, however far over
// budget a single oversized segment runs, because a hard floor beats an
// empty log that silently stops recording anything at all.
func (p *partition) shed() (droppedBytes int64, err error) {
	segments, err := listSegments(p.dir)
	if err != nil {
		return 0, err
	}
	if len(segments) <= 1 {
		return 0, nil
	}
	var total int64
	for _, s := range segments {
		total += s.bytes
	}
	oldestAllowed := time.Now().Add(-p.capAge)
	for len(segments) > 1 {
		oldest := segments[0]
		overBytes := total > p.capBytes
		overAge := p.capAge > 0 && oldest.modTime.Before(oldestAllowed)
		if !overBytes && !overAge {
			break
		}
		if err := os.Remove(segmentPath(p.dir, oldest.index)); err != nil && !os.IsNotExist(err) {
			return droppedBytes, fmt.Errorf("shed %s partition segment: %w", p.name, err)
		}
		droppedBytes += oldest.bytes
		total -= oldest.bytes
		segments = segments[1:]
	}
	return droppedBytes, nil
}

// stats returns bytes currently held and the age of the oldest segment still
// on disk - what a heartbeat reports, and what "site isolated since…" on the
// platform side is computed from. An idle spool always has one empty active
// segment (Open creates it), which must not count: without the size guard
// an idle collector's "oldest age" would climb forever from a file holding
// nothing, and a banner reading that would say a healthy site is isolated.
func (p *partition) stats() (bytes int64, oldestAge time.Duration) {
	segments, err := listSegments(p.dir)
	if err != nil {
		return 0, 0
	}
	var oldest time.Time
	for _, s := range segments {
		if s.bytes == 0 {
			continue
		}
		bytes += s.bytes
		if oldest.IsZero() || s.modTime.Before(oldest) {
			oldest = s.modTime
		}
	}
	if oldest.IsZero() {
		return bytes, 0
	}
	return bytes, time.Since(oldest)
}

func (p *partition) close() error {
	if p.active == nil {
		return nil
	}
	return p.active.Close()
}
