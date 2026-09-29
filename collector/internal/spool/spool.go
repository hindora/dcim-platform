package spool

import (
	"fmt"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"
)

// Two streams this collector produces are spoolable; heartbeats are not -
// see Append.
const (
	StreamTelemetry     = "telemetry.v1"
	StreamEvents        = "events.v1"
	StreamEndpointState = "endpointstate.v1"
)

// Config sizes the spool. Defaults match docs/26 Phase 3's sizing rationale:
// ~10 GB/day uncompressed for the whole simulated estate, so 72h fits inside
// 40GB with room for a per-site collector's actual (smaller) share; the cap
// is the backstop, not the expected steady state.
type Config struct {
	Dir string

	SegmentBytes int64 // default 64 MiB

	// The 90% telemetry gets. Shed first: a gap in a metric chart is a
	// smaller loss than a missed alarm.
	BulkCapBytes int64
	BulkCapAge   time.Duration

	// The reserved 10% for events and endpoint-state. Shed last, and
	// replayed first on reconnect - see Drain.
	ReservedCapBytes int64
	ReservedCapAge   time.Duration

	// Memory is the hot path; these bound how long anything can sit there
	// before a flush, which is also the crash-loss window: kill -9 loses at
	// most what has not yet been fsynced to a segment.
	FlushInterval time.Duration
	FlushBytes    int64
	FlushCount    int
}

func (c Config) withDefaults() Config {
	if c.SegmentBytes == 0 {
		c.SegmentBytes = 64 << 20
	}
	if c.BulkCapBytes == 0 {
		c.BulkCapBytes = 36 << 30 // 36 GiB: 90% of the 40GB backstop
	}
	if c.BulkCapAge == 0 {
		c.BulkCapAge = 72 * time.Hour
	}
	if c.ReservedCapBytes == 0 {
		c.ReservedCapBytes = 4 << 30 // 4 GiB: the reserved 10%
	}
	if c.ReservedCapAge == 0 {
		c.ReservedCapAge = 72 * time.Hour
	}
	if c.FlushInterval == 0 {
		c.FlushInterval = 5 * time.Second
	}
	if c.FlushBytes == 0 {
		c.FlushBytes = 1 << 20 // 1 MiB
	}
	if c.FlushCount == 0 {
		c.FlushCount = 500
	}
	return c
}

// Stats is what a heartbeat reports - contracts/schema/messages_v1.yaml's
// spool_bytes, spool_oldest_age_s and (from the Reader) replay_rate.
type Stats struct {
	Bytes     int64
	OldestAge time.Duration
}

type Spool struct {
	cfg Config

	bulk, reserved *partition

	mu           sync.Mutex
	memBulk      []pending
	memReserved  []pending
	memBulkBytes int64
	memResBytes  int64

	stopCh chan struct{}
	doneCh chan struct{}

	// shedBytes is cumulative bytes shed for capacity since this process
	// started - the gateway transport's equivalent of Publisher.Dropped(),
	// in bytes rather than a sample count because a whole segment is what
	// shedding actually removes, telemetry and events mixed together.
	shedBytes atomic.Int64
}

// Open creates (or resumes) a spool rooted at cfg.Dir, recovering whatever
// segments and sequence numbers a previous process left behind, and starts
// the background flush loop. Call Close to stop it and flush anything still
// only in memory.
func Open(cfg Config) (*Spool, error) {
	cfg = cfg.withDefaults()
	bulk, err := openPartition(filepath.Join(cfg.Dir, "bulk"), "bulk",
		cfg.BulkCapBytes, cfg.BulkCapAge, cfg.SegmentBytes)
	if err != nil {
		return nil, err
	}
	reserved, err := openPartition(filepath.Join(cfg.Dir, "reserved"), "reserved",
		cfg.ReservedCapBytes, cfg.ReservedCapAge, cfg.SegmentBytes)
	if err != nil {
		bulk.close()
		return nil, err
	}
	s := &Spool{cfg: cfg, bulk: bulk, reserved: reserved,
		stopCh: make(chan struct{}), doneCh: make(chan struct{})}
	go s.flushLoop()
	return s, nil
}

// partitionFor is the one place that decides which budget a stream draws
// from. Anything not explicitly bulk goes to reserved rather than being
// silently dropped - a stream this package does not yet know about is far
// more likely to be a future addition than junk, and reserved is the safer
// place to be wrong in.
func partitionFor(stream string) bool { // true = bulk
	return stream == StreamTelemetry
}

// Append queues one record for eventual disk durability. It does not write
// to disk itself except when a flush threshold is crossed here inline -
// see flushLoop for the timer-driven path, and Close for the shutdown path.
//
// Heartbeats are never spooled: a heartbeat is a snapshot of "right now",
// and a replayed one three hours later is not stale information, it is
// actively wrong information. A caller that cannot reach the platform
// simply drops the current heartbeat and sends the next one when it can.
func (s *Spool) Append(stream string, payload []byte, at time.Time) {
	p := pending{stream: stream, at: at, payload: payload}
	s.mu.Lock()
	defer s.mu.Unlock()
	if partitionFor(stream) {
		s.memBulk = append(s.memBulk, p)
		s.memBulkBytes += int64(len(payload))
	} else {
		s.memReserved = append(s.memReserved, p)
		s.memResBytes += int64(len(payload))
	}
	if s.memBulkBytes >= s.cfg.FlushBytes || len(s.memBulk) >= s.cfg.FlushCount ||
		s.memResBytes >= s.cfg.FlushBytes || len(s.memReserved) >= s.cfg.FlushCount {
		_ = s.flushLocked()
	}
}

func (s *Spool) flushLoop() {
	defer close(s.doneCh)
	ticker := time.NewTicker(s.cfg.FlushInterval)
	defer ticker.Stop()
	for {
		select {
		case <-s.stopCh:
			return
		case <-ticker.C:
			s.mu.Lock()
			_ = s.flushLocked()
			s.mu.Unlock()
		}
	}
}

// flushLocked writes whatever is queued in memory to the active segment of
// each partition that has anything pending, fsyncing as part of
// partition.append, then sheds either partition that is now over budget.
// Caller holds s.mu.
func (s *Spool) flushLocked() error {
	if len(s.memBulk) > 0 {
		if err := s.bulk.append(s.memBulk); err != nil {
			return fmt.Errorf("flush bulk partition: %w", err)
		}
		s.memBulk = nil
		s.memBulkBytes = 0
		dropped, err := s.bulk.shed()
		if err != nil {
			return fmt.Errorf("shed bulk partition: %w", err)
		}
		s.shedBytes.Add(dropped)
	}
	if len(s.memReserved) > 0 {
		if err := s.reserved.append(s.memReserved); err != nil {
			return fmt.Errorf("flush reserved partition: %w", err)
		}
		s.memReserved = nil
		s.memResBytes = 0
		dropped, err := s.reserved.shed()
		if err != nil {
			return fmt.Errorf("shed reserved partition: %w", err)
		}
		s.shedBytes.Add(dropped)
	}
	return nil
}

// SheddedBytes is cumulative bytes shed for capacity since Open - see
// shedBytes.
func (s *Spool) SheddedBytes() int64 { return s.shedBytes.Load() }

// Flush forces the in-memory tail to disk now, outside the timer. Call this
// on a clean shutdown - it is the other half of "kill -9 loses at most the
// in-memory tail (≤ the flush interval)": a clean stop loses nothing at all.
func (s *Spool) Flush() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.flushLocked()
}

// Stats combines both partitions' on-disk state with whatever is still only
// in memory, so "spool_bytes" and "spool_oldest_age_s" describe everything
// a restart or a replay would actually have to account for, not only the
// fraction already fsynced.
func (s *Spool) Stats() Stats {
	s.mu.Lock()
	memBytes := s.memBulkBytes + s.memResBytes
	var memOldest time.Time
	for _, p := range s.memBulk {
		if memOldest.IsZero() || p.at.Before(memOldest) {
			memOldest = p.at
		}
	}
	for _, p := range s.memReserved {
		if memOldest.IsZero() || p.at.Before(memOldest) {
			memOldest = p.at
		}
	}
	s.mu.Unlock()

	bBytes, bAge := s.bulk.stats()
	rBytes, rAge := s.reserved.stats()

	total := Stats{Bytes: bBytes + rBytes + memBytes}
	total.OldestAge = maxDuration(bAge, rAge)
	if !memOldest.IsZero() {
		total.OldestAge = maxDuration(total.OldestAge, time.Since(memOldest))
	}
	return total
}

func maxDuration(a, b time.Duration) time.Duration {
	if a > b {
		return a
	}
	return b
}

// Close stops the flush loop, flushes anything still in memory, and closes
// both partitions' segment files.
func (s *Spool) Close() error {
	close(s.stopCh)
	<-s.doneCh
	if err := s.Flush(); err != nil {
		return err
	}
	if err := s.bulk.close(); err != nil {
		return err
	}
	return s.reserved.close()
}
