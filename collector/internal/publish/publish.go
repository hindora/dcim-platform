// Package publish batches canonical messages onto Redis Streams.
//
// The stream is both the contract boundary and the buffer. If the database is
// slow or the ingest worker is restarting, the collector keeps polling and the
// stream absorbs it; nothing is lost until the cap is reached. That buffer is
// the whole reason the collector does not write to PostgreSQL itself.
package publish

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/vmihailenco/msgpack/v5"

	"github.com/hari/dcim-platform/collector/internal/config"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

type Publisher struct {
	rdb  *redis.Client
	cfg  *config.Config
	log  *slog.Logger
	mets *obs.Metrics

	mu      sync.Mutex
	pending []models.Telemetry
	events  []models.Event

	// Bounded fallback for when Redis is unreachable. Telemetry is shed from
	// the oldest end; events are not, because they are rarer and carry more
	// information per byte.
	ring    []models.Telemetry
	ringCap int

	flushCh chan struct{}
	wg      sync.WaitGroup
}

func New(rdb *redis.Client, cfg *config.Config, log *slog.Logger, mets *obs.Metrics) *Publisher {
	return &Publisher{
		rdb: rdb, cfg: cfg, log: log, mets: mets,
		ringCap: cfg.Publisher.RingCapacity,
		flushCh: make(chan struct{}, 1),
	}
}

// Run drives the periodic flush. It returns when ctx is cancelled, after a
// final flush - dropping a full batch on shutdown is an avoidable data gap.
func (p *Publisher) Run(ctx context.Context) {
	ticker := time.NewTicker(p.cfg.Publisher.MaxDelay)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			flushCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			p.flush(flushCtx)
			cancel()
			return
		case <-ticker.C:
			p.flush(ctx)
		case <-p.flushCh:
			p.flush(ctx)
		}
	}
}

func (p *Publisher) Telemetry(_ context.Context, samples []models.Telemetry) error {
	if len(samples) == 0 {
		return nil
	}
	p.mu.Lock()
	p.pending = append(p.pending, samples...)
	n := len(p.pending)
	p.mu.Unlock()

	p.mets.PublishQueueDepth.Set(float64(n))
	if n >= p.cfg.Publisher.MaxBatch {
		select {
		case p.flushCh <- struct{}{}:
		default: // a flush is already pending; do not block the poll path
		}
	}
	return nil
}

func (p *Publisher) Events(_ context.Context, events []models.Event) error {
	if len(events) == 0 {
		return nil
	}
	p.mu.Lock()
	p.events = append(p.events, events...)
	p.mu.Unlock()
	select {
	case p.flushCh <- struct{}{}:
	default:
	}
	return nil
}

// EndpointState is published immediately and on change only, never every poll.
func (p *Publisher) EndpointState(ctx context.Context, st models.EndpointState) error {
	return p.xadd(ctx, p.cfg.Redis.Streams.EndpointState, st)
}

func (p *Publisher) Heartbeat(ctx context.Context, hb models.CollectorHeartbeat) error {
	return p.xadd(ctx, p.cfg.Redis.Streams.Heartbeat, hb)
}

func (p *Publisher) flush(ctx context.Context) {
	p.mu.Lock()
	samples := p.pending
	events := p.events
	p.pending = nil
	p.events = nil
	// Retry anything the ring is holding ahead of new data, so ordering within
	// a stream stays roughly chronological.
	if len(p.ring) > 0 {
		samples = append(p.ring, samples...)
		p.ring = nil
	}
	p.mu.Unlock()

	if len(events) > 0 {
		batch := models.EventBatch{
			CollectorID: p.cfg.Collector.ID, Events: events,
			SentAt: models.NowMicros(), SchemaVersion: models.SchemaVersion,
		}
		if err := p.xadd(ctx, p.cfg.Redis.Streams.Events, batch); err != nil {
			// Events are never shed. Losing an alarm is worse than losing a
			// sample, and they are cheap to hold.
			p.log.Error("event publish failed; will retry", "error", err,
				"events", len(events))
			p.mu.Lock()
			p.events = append(events, p.events...)
			p.mu.Unlock()
		}
	}

	if len(samples) == 0 {
		p.mets.PublishQueueDepth.Set(0)
		return
	}

	max := p.cfg.Publisher.MaxBatch
	for start := 0; start < len(samples); start += max {
		end := start + max
		if end > len(samples) {
			end = len(samples)
		}
		chunk := samples[start:end]
		batch := models.TelemetryBatch{
			CollectorID: p.cfg.Collector.ID, Samples: chunk,
			SentAt: models.NowMicros(), SchemaVersion: models.SchemaVersion,
		}
		started := time.Now()
		err := p.xadd(ctx, p.cfg.Redis.Streams.Telemetry, batch)
		p.mets.PublishDuration.Observe(time.Since(started).Seconds())
		p.mets.PublishBatchSize.Observe(float64(len(chunk)))
		if err != nil {
			p.log.Warn("telemetry publish failed; buffering", "error", err,
				"samples", len(chunk))
			p.bufferOrShed(chunk)
		}
	}
	p.mu.Lock()
	depth := len(p.pending) + len(p.ring)
	p.mu.Unlock()
	p.mets.PublishQueueDepth.Set(float64(depth))
}

// bufferOrShed keeps the newest samples and drops the oldest when the ring is
// full. Dropping the newest would make the dashboard permanently wrong rather
// than briefly incomplete.
func (p *Publisher) bufferOrShed(chunk []models.Telemetry) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.ring = append(p.ring, chunk...)
	if overflow := len(p.ring) - p.ringCap; overflow > 0 {
		p.ring = p.ring[overflow:]
		p.mets.PublishDropped.WithLabelValues(
			p.cfg.Redis.Streams.Telemetry.Name, "ring_full").Add(float64(overflow))
		p.log.Error("shedding telemetry: publish buffer full",
			"dropped", overflow, "capacity", p.ringCap)
	}
}

func (p *Publisher) xadd(ctx context.Context, stream config.StreamCfg, msg any) error {
	payload, err := msgpack.Marshal(msg)
	if err != nil {
		return err
	}
	var lastErr error
	backoff := 50 * time.Millisecond
	for attempt := 0; attempt < 3; attempt++ {
		err := p.rdb.XAdd(ctx, &redis.XAddArgs{
			Stream: stream.Name,
			MaxLen: stream.MaxLen,
			Approx: true,
			Values: map[string]any{"p": payload},
		}).Err()
		if err == nil {
			return nil
		}
		lastErr = err
		if ctx.Err() != nil {
			return err
		}
		time.Sleep(backoff)
		backoff *= 2
	}
	return lastErr
}

// Ping is used by the readiness check.
func (p *Publisher) Ping(ctx context.Context) error { return p.rdb.Ping(ctx).Err() }

func (p *Publisher) QueueDepth() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.pending) + len(p.ring)
}

var _ models.Sink = (*Publisher)(nil)
