package publish

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/klauspost/compress/zstd"
	"github.com/vmihailenco/msgpack/v5"

	"github.com/hari/dcim-platform/collector/internal/config"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/internal/spool"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// pathSegment maps a spool stream name to the URL segment
// POST /collector/batches/{path_segment} expects - app/services/
// collector_gateway.py's STREAM_BY_PATH on the platform side, mirrored here
// so the two ends agree without either importing the other.
var pathSegment = map[string]string{
	spool.StreamTelemetry:     "telemetry",
	spool.StreamEvents:        "events",
	spool.StreamEndpointState: "endpointstate",
}

// GatewayPublisher is the WAN-safe alternative to Publisher (docs/26 Phase
// 3): used when a collector reaches the platform over the internet rather
// than a trusted network Redis can be exposed on directly.
//
// Every record is spooled to disk before it is ever sent - "always spool,
// drain continuously" rather than "send live, spool only on failure". That
// is a deliberate simplification over the alternative of sending live with
// the spool as a fallback: this way there is exactly one sequence authority
// per stream (the spool partition's own nextSeq), so a retried and a
// replayed copy of the same record can never disagree about what X-DCIM-
// Spool-Seq to present. The cost is a few seconds of steady-state latency -
// bounded by the spool's flush interval plus however far behind the drain
// loop's rate limiter is - which is immaterial for facility telemetry.
//
// Heartbeats are the one exception: they are never spooled (see
// spool.Spool.Append's own docstring on why a replayed "right now" is wrong
// information) and go straight to POST /collector/heartbeat instead of the
// batches endpoint, matching the REST fallback that already existed for a
// collector not using the Redis stream.
type GatewayPublisher struct {
	http        *http.Client
	baseURL     string
	token       string
	collectorID string
	log         *slog.Logger
	mets        *obs.Metrics
	sp          *spool.Spool
	enc         *zstd.Encoder

	drainStop chan struct{}
	drainDone chan struct{}

	replayed atomic.Uint64
	// rateWindow bookkeeping for ReplayRateRPS - see that method.
	rateAt    atomic.Int64  // UnixNano of the last ReplayRateRPS call
	rateCount atomic.Uint64 // g.replayed's value as of rateAt
}

// NewGateway builds a GatewayPublisher. tlsConfig is nil for a collector
// that has never enrolled (see app.New) - its http.Client then presents no
// client certificate and relies on the bearer token alone, exactly like
// every other HTTP client this collector builds.
func NewGateway(cfg *config.Config, log *slog.Logger, mets *obs.Metrics,
	tlsConfig *tls.Config, sp *spool.Spool) (*GatewayPublisher, error) {
	enc, err := zstd.NewWriter(nil)
	if err != nil {
		return nil, fmt.Errorf("build zstd encoder: %w", err)
	}
	g := &GatewayPublisher{
		http: &http.Client{Timeout: cfg.DCIM.RequestTimeout,
			Transport: &http.Transport{TLSClientConfig: tlsConfig}},
		baseURL:     strings.TrimRight(cfg.DCIM.BaseURL, "/"),
		token:       cfg.Token(),
		collectorID: cfg.Collector.ID,
		log:         log, mets: mets, sp: sp, enc: enc,
		drainStop: make(chan struct{}), drainDone: make(chan struct{}),
	}
	g.rateAt.Store(time.Now().UnixNano())
	return g, nil
}

func (g *GatewayPublisher) Telemetry(_ context.Context, samples []models.Telemetry) error {
	if len(samples) == 0 {
		return nil
	}
	batch := models.TelemetryBatch{CollectorID: g.collectorID, Samples: samples,
		SentAt: models.NowMicros(), SchemaVersion: models.SchemaVersion}
	payload, err := msgpack.Marshal(batch)
	if err != nil {
		return fmt.Errorf("encode telemetry batch: %w", err)
	}
	g.sp.Append(spool.StreamTelemetry, payload, time.Now())
	g.mets.PublishBatchSize.Observe(float64(len(samples)))
	return nil
}

func (g *GatewayPublisher) Events(_ context.Context, events []models.Event) error {
	if len(events) == 0 {
		return nil
	}
	batch := models.EventBatch{CollectorID: g.collectorID, Events: events,
		SentAt: models.NowMicros(), SchemaVersion: models.SchemaVersion}
	payload, err := msgpack.Marshal(batch)
	if err != nil {
		return fmt.Errorf("encode event batch: %w", err)
	}
	g.sp.Append(spool.StreamEvents, payload, time.Now())
	return nil
}

func (g *GatewayPublisher) EndpointState(_ context.Context, st models.EndpointState) error {
	payload, err := msgpack.Marshal(st)
	if err != nil {
		return fmt.Errorf("encode endpoint state: %w", err)
	}
	g.sp.Append(spool.StreamEndpointState, payload, time.Now())
	return nil
}

// Heartbeat is never spooled - see the type docstring - and POSTs straight
// to the JSON fallback endpoint rather than the msgpack batches route, since
// there is no gateway batches path for it (collector_gateway.STREAM_BY_PATH
// deliberately excludes heartbeats). hb.SpoolBytes/SpoolOldestAgeS/
// ReplayRate are expected to already be filled in - app.App's heartbeatLoop
// does it generically from SpoolStats/ReplayRateRPS for whichever transport
// is active, rather than each transport mutating the struct differently.
func (g *GatewayPublisher) Heartbeat(ctx context.Context, hb models.CollectorHeartbeat) error {
	body, err := json.Marshal(hb)
	if err != nil {
		return fmt.Errorf("encode heartbeat: %w", err)
	}
	url := g.baseURL + "/api/v1/collector/heartbeat"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+g.token)
	resp, err := g.http.Do(req)
	if err != nil {
		return fmt.Errorf("heartbeat POST: %w", err)
	}
	defer func() { _, _ = io.Copy(io.Discard, resp.Body); _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusNoContent {
		return fmt.Errorf("heartbeat POST: %s", resp.Status)
	}
	return nil
}

// Run drains the spool for as long as ctx lives. Unlike Publisher.Run there
// is no periodic in-memory flush to drive here - spool.Spool already runs
// its own flush loop from Open - so this only owns the replay side.
func (g *GatewayPublisher) Run(ctx context.Context) {
	defer close(g.drainDone)
	drainer, err := g.sp.NewDrainer()
	if err != nil {
		g.log.Error("gateway: could not open spool drainer; replay disabled", "error", err)
		<-ctx.Done()
		return
	}
	defer drainer.Close()

	const baseBackoff = 500 * time.Millisecond
	const maxBackoff = 30 * time.Second
	backoff := baseBackoff

	for {
		select {
		case <-ctx.Done():
			return
		case <-g.drainStop:
			return
		default:
		}

		res, ok, err := drainer.Next(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			g.log.Warn("gateway: spool replay read failed", "error", err)
			if !g.sleep(ctx, backoff) {
				return
			}
			continue
		}
		if !ok {
			// Caught up to the live write position. This is the ordinary
			// steady state, not a fault - wait a beat rather than spin.
			if !g.sleep(ctx, baseBackoff) {
				return
			}
			continue
		}

		if err := g.send(ctx, res); err != nil {
			if ctx.Err() != nil {
				return
			}
			// A 4xx other than 429/426 means the platform rejected THIS
			// record specifically - most likely spool file corruption,
			// since the collector's own encoder produced it. Retrying
			// forever at the throttled rate is the honest outcome: nothing
			// behind it in this partition can be sent out of order, so it
			// stays visibly stuck rather than being silently dropped. A
			// dead-letter path for a genuinely poison record is real future
			// work, scoped out of this phase - see docs/26.
			g.log.Warn("gateway: batch send failed; will retry", "stream", res.Stream,
				"seq", res.Seq, "error", err)
			if !g.sleep(ctx, backoff) {
				return
			}
			if backoff < maxBackoff {
				backoff *= 2
			}
			continue
		}
		backoff = baseBackoff
		if err := res.Ack(); err != nil {
			g.log.Error("gateway: sent but could not persist the replay cursor; "+
				"a restart may resend this record", "stream", res.Stream, "error", err)
		}
		g.replayed.Add(1)
	}
}

func (g *GatewayPublisher) sleep(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return true
	case <-g.drainStop:
		return false
	case <-ctx.Done():
		return false
	}
}

func (g *GatewayPublisher) send(ctx context.Context, res spool.DrainResult) error {
	seg, ok := pathSegment[res.Stream]
	if !ok {
		// Cannot happen from this process's own spool - partitionFor only
		// ever files records under these three stream names - but a stuck,
		// mislabelled record must not be sent nowhere silently either.
		return fmt.Errorf("no batches path for spooled stream %q", res.Stream)
	}
	compressed := g.enc.EncodeAll(res.Payload, nil)
	url := g.baseURL + "/api/v1/collector/batches/" + seg
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(compressed))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	req.Header.Set("Authorization", "Bearer "+g.token)
	req.Header.Set("X-DCIM-Spool-Seq", strconv.FormatUint(res.Seq, 10))

	started := time.Now()
	resp, err := g.http.Do(req)
	if err != nil {
		return fmt.Errorf("batches POST %s: %w", seg, err)
	}
	defer func() { _, _ = io.Copy(io.Discard, resp.Body); _ = resp.Body.Close() }()
	g.mets.PublishDuration.Observe(time.Since(started).Seconds())

	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return nil
	}
	retryAfter := resp.Header.Get("Retry-After")
	return fmt.Errorf("batches POST %s: %s (retry-after=%s)", seg, resp.Status, retryAfter)
}

// ReplayRateRPS is records replayed per second over the interval since it
// was last called - contracts/schema/messages_v1.yaml's replay_rate, read
// once per heartbeat tick.
func (g *GatewayPublisher) ReplayRateRPS() uint32 {
	now := time.Now().UnixNano()
	prevAt := g.rateAt.Swap(now)
	prevCount := g.rateCount.Swap(g.replayed.Load())
	elapsed := time.Duration(now - prevAt).Seconds()
	if elapsed <= 0 {
		return 0
	}
	delta := g.rateCount.Load() - prevCount
	return uint32(float64(delta) / elapsed)
}

func (g *GatewayPublisher) SpoolStats() spool.Stats { return g.sp.Stats() }

// Ping is used by the readiness check - a lightweight reachability probe,
// not a full round trip through the batches or heartbeat path.
func (g *GatewayPublisher) Ping(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, g.baseURL+"/api/v1/health", nil)
	if err != nil {
		return err
	}
	resp, err := g.http.Do(req)
	if err != nil {
		return err
	}
	defer func() { _, _ = io.Copy(io.Discard, resp.Body); _ = resp.Body.Close() }()
	if resp.StatusCode >= 500 {
		return fmt.Errorf("platform health check: %s", resp.Status)
	}
	return nil
}

// Capacity, Dropped and QueueDepth exist so GatewayPublisher satisfies the
// same interface app.App uses for Publisher. None of the three sample-count
// concepts they report for the direct-Redis transport apply here - the
// spool's own byte-denominated capacity and shed counter are what actually
// bound this transport, and those already travel in every heartbeat as
// SpoolBytes/SpoolOldestAgeS and PublishDropped (see Dropped below) rather
// than through these. Capacity and QueueDepth return 0: "not applicable",
// not "empty".
func (g *GatewayPublisher) Capacity() int { return 0 }

// Dropped is cumulative bytes shed from the spool for capacity, unlike
// Publisher.Dropped's sample count - a deliberate, documented unit
// difference between the two transports for the same heartbeat field, since
// shedding here removes whole segments rather than individual samples.
func (g *GatewayPublisher) Dropped() uint64 { return uint64(g.sp.SheddedBytes()) }

func (g *GatewayPublisher) QueueDepth() int { return 0 }

// Close stops the drain loop (Run returns) without touching the spool
// itself - the caller closes that separately, since it may outlive this
// publisher's Run across a transport reconfiguration.
func (g *GatewayPublisher) Close() {
	close(g.drainStop)
	<-g.drainDone
}

var _ models.Sink = (*GatewayPublisher)(nil)
