package gnmi

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"math/rand"
	"sync"
	"time"

	gpb "github.com/openconfig/gnmi/proto/gnmi"

	"github.com/hari/dcim-platform/collector/internal/health"
	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// Subscriber holds long-lived gNMI Subscribe streams.
//
// This is where gNMI stops being a poller and becomes what it is for. A
// subscription is opened once and the device pushes on a schedule it was ASKED
// for but does not guarantee, which changes three things the rest of the
// collector assumes:
//
//   - THE DEVICE OWNS THE CADENCE. sample_interval is a request. Gear under
//     load sends late, and gear with on-change leaves sends early. Telemetry
//     is stamped with the notification's own timestamp, not with arrival,
//     because on a stream those differ by up to a whole interval.
//
//   - HEALTH IS NOT DERIVED FROM POLLS. A stream that is up and silent looks
//     identical to one that has died, so a session is only healthy while it is
//     both connected and delivering; silence past a grace multiple of the
//     interval is a failure even though nothing errored.
//
//   - RECONNECTION IS THE NORMAL CASE. A switch reboot, a supervisor
//     failover, a maintenance window - all end the stream, and the collector's
//     job is to come back without a thundering herd of 46 simultaneous dials.
type Subscriber struct {
	adapter *Adapter
	conns   *ConnPool
	maps    *mapping.GNMIMap
	sink    models.Sink
	tracker *health.Tracker
	log     *slog.Logger
	mets    *obs.Metrics

	// Silence past this multiple of the requested interval fails the session.
	graceFactor float64
	// A floor under the grace window, so a one-second sample interval does
	// not fail a device that paused briefly.
	minGrace time.Duration
	// graceWindow, when set, replaces the interval-derived window entirely.
	// Some operators want one staleness rule for the whole estate rather than
	// one that varies with whatever each mapping asked the device for.
	graceWindow time.Duration
	minBackoff  time.Duration
	maxBackoff  time.Duration

	mu       sync.Mutex
	sessions map[string]*session
	rnd      *rand.Rand
}

type session struct {
	endpoint *models.Endpoint
	cancel   context.CancelFunc
	done     chan struct{}
}

func NewSubscriber(a *Adapter, conns *ConnPool, maps *mapping.GNMIMap,
	sink models.Sink, tracker *health.Tracker, log *slog.Logger,
	mets *obs.Metrics, graceFactor float64) *Subscriber {

	if graceFactor < 1.5 {
		// Anything tighter fails a healthy device that sampled a moment late.
		graceFactor = 3
	}
	return &Subscriber{
		adapter: a, conns: conns, maps: maps, sink: sink, tracker: tracker,
		log: log, mets: mets, graceFactor: graceFactor,
		minGrace:   30 * time.Second,
		minBackoff: time.Second, maxBackoff: 2 * time.Minute,
		sessions: make(map[string]*session),
		rnd:      rand.New(rand.NewSource(time.Now().UnixNano())), //nolint:gosec // jitter, not crypto
	}
}

// StreamOnly reports whether an endpoint is meant to be streamed rather than
// polled: a zero interval with push enabled, which is exactly how the
// gnmi-stream poll profile is defined.
func StreamOnly(ep *models.Endpoint) bool {
	return ep.Protocol == "gnmi" && ep.Poll.PushEnabled && ep.Poll.IntervalS == 0
}

// Manage brings the running sessions in line with the assignment.
func (s *Subscriber) Manage(ctx context.Context, endpoints []*models.Endpoint) {
	want := make(map[string]*models.Endpoint)
	for _, ep := range endpoints {
		if StreamOnly(ep) {
			want[ep.ID] = ep
		}
	}

	s.mu.Lock()
	for id, sess := range s.sessions {
		if _, keep := want[id]; !keep {
			sess.cancel()
			delete(s.sessions, id)
		}
	}
	var starting []*models.Endpoint
	for id, ep := range want {
		if _, running := s.sessions[id]; running {
			continue
		}
		starting = append(starting, ep)
	}
	s.mu.Unlock()

	for _, ep := range starting {
		s.start(ctx, ep)
	}
	if len(starting) > 0 {
		s.log.Info("gnmi streams starting", "count", len(starting),
			"total", s.Sessions())
	}
}

func (s *Subscriber) start(ctx context.Context, ep *models.Endpoint) {
	sctx, cancel := context.WithCancel(ctx)
	sess := &session{endpoint: ep, cancel: cancel, done: make(chan struct{})}

	s.mu.Lock()
	s.sessions[ep.ID] = sess
	s.mu.Unlock()

	s.tracker.Register(ep)
	go func() {
		defer close(sess.done)
		s.run(sctx, ep)
	}()
}

// run keeps one subscription alive, reconnecting with backoff.
func (s *Subscriber) run(ctx context.Context, ep *models.Endpoint) {
	backoff := s.minBackoff
	for {
		if ctx.Err() != nil {
			return
		}
		started := time.Now()
		err := s.subscribe(ctx, ep)
		if ctx.Err() != nil {
			return
		}
		if err != nil {
			s.tracker.Failure(ep, classify(err))
			s.log.Warn("gnmi stream ended", "endpoint", ep.ID,
				"address", ep.Address, "error", err,
				"uptime_s", int(time.Since(started).Seconds()))
		}
		// A session that lasted a long time was healthy; a reconnect loop that
		// fails instantly must not hammer the device.
		if time.Since(started) > 5*time.Minute {
			backoff = s.minBackoff
		}
		wait := s.jitter(backoff)
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
		if backoff < s.maxBackoff {
			backoff *= 2
		}
	}
}

// jitter spreads reconnects so 46 devices coming back from one outage do not
// dial in lockstep.
func (s *Subscriber) jitter(d time.Duration) time.Duration {
	s.mu.Lock()
	f := 0.5 + s.rnd.Float64() // 0.5x .. 1.5x
	s.mu.Unlock()
	return time.Duration(float64(d) * f)
}

// subscribe opens one stream and reads it until it ends.
func (s *Subscriber) subscribe(ctx context.Context, ep *models.Endpoint) error {
	tgt, err := targetOf(ep)
	if err != nil {
		return err
	}
	client, err := s.conns.Client(ctx, ep.ID, tgt)
	if err != nil {
		return err
	}

	stream, err := client.Subscribe(s.conns.WithAuth(ctx, tgt.addr))
	if err != nil {
		return err
	}

	subs := make([]*gpb.Subscription, 0, len(s.maps.Subscriptions))
	interval := time.Duration(0)
	for _, sub := range s.maps.Subscriptions {
		iv := sub.SampleInterval
		if iv <= 0 {
			iv = 30 * time.Second
		}
		if interval == 0 || iv < interval {
			interval = iv
		}
		subs = append(subs, &gpb.Subscription{
			Path: pathOf(sub.Path),
			// SAMPLE, not TARGET_DEFINED: the collector needs a cadence it can
			// reason about for staleness, and TARGET_DEFINED lets the device
			// choose one nobody recorded.
			Mode:           gpb.SubscriptionMode_SAMPLE,
			SampleInterval: uint64(iv.Nanoseconds()),
		})
	}

	req := &gpb.SubscribeRequest{
		Request: &gpb.SubscribeRequest_Subscribe{
			Subscribe: &gpb.SubscriptionList{
				Prefix:       &gpb.Path{Target: tgt.target},
				Subscription: subs,
				Mode:         gpb.SubscriptionList_STREAM,
				Encoding:     gpb.Encoding_JSON_IETF,
			},
		},
	}
	if err := stream.Send(req); err != nil {
		return err
	}

	s.log.Info("gnmi stream open", "endpoint", ep.ID, "address", tgt.addr,
		"target", tgt.target, "paths", len(subs),
		"sample_interval", interval.String())

	// Silence is the failure a stream cannot report for itself.
	grace := s.graceWindow
	if grace <= 0 {
		grace = time.Duration(float64(interval) * s.graceFactor)
		if grace < s.minGrace {
			grace = s.minGrace
		}
	}
	deadline := time.NewTimer(grace)
	defer deadline.Stop()

	recv := make(chan *gpb.SubscribeResponse)
	errCh := make(chan error, 1)
	go func() {
		for {
			resp, err := stream.Recv()
			if err != nil {
				errCh <- err
				return
			}
			select {
			case recv <- resp:
			case <-ctx.Done():
				return
			}
		}
	}()

	synced := false
	for {
		select {
		case <-ctx.Done():
			return nil
		case err := <-errCh:
			if errors.Is(err, io.EOF) {
				return errors.New("device closed the stream")
			}
			return err
		case <-deadline.C:
			// Nothing arrived within the grace window. The connection may well
			// be fine, which is exactly the point: a silent stream delivers no
			// telemetry and must not be reported as a healthy endpoint.
			return errors.New("no updates within " + grace.String())
		case resp := <-recv:
			if !deadline.Stop() {
				select {
				case <-deadline.C:
				default:
				}
			}
			deadline.Reset(grace)

			if resp.GetSyncResponse() {
				synced = true
				s.log.Debug("gnmi initial snapshot complete", "endpoint", ep.ID)
				continue
			}
			n := resp.GetUpdate()
			if n == nil {
				continue
			}
			s.deliver(ctx, ep, n, synced)
		}
	}
}

// deliver decodes one notification and publishes it.
func (s *Subscriber) deliver(ctx context.Context, ep *models.Endpoint,
	n *gpb.Notification, synced bool) {

	outcome := &models.PollOutcome{}
	now := models.NowMicros()

	// A notification carries no indication of which subscription produced it,
	// so every mapping is offered the tree and the ones that do not match
	// contribute nothing. Cheap, and it is what makes a device that answers a
	// deeper path than we asked for still decode.
	for _, sub := range s.maps.Subscriptions {
		s.adapter.collect(ep, sub, n, outcome, now)
	}
	if len(outcome.Samples) == 0 {
		return
	}

	// Deduplicate. When a peer answers with the whole document - one update
	// per requested path, each carrying everything - all three mappings match
	// the same tree, and the same interface counter would be published once
	// per update. Three identical samples one microsecond apart are
	// indistinguishable from three real ones, and a counter published three
	// times is a rate computed three times.
	samples := dedupe(outcome.Samples)

	if err := s.sink.Telemetry(ctx, samples); err != nil {
		s.log.Warn("gnmi stream publish failed", "endpoint", ep.ID, "error", err)
		return
	}
	s.mets.SamplesTotal.WithLabelValues("gnmi_stream").
		Add(float64(len(samples)))
	if synced {
		// Latency is meaningless on a push, so it is reported as zero rather
		// than as a made-up number: the device chose when to send.
		s.tracker.Success(ep, 0)
	}
}

// SetGraceWindow fixes the silence window for every session, overriding the
// one derived from the sample interval.
func (s *Subscriber) SetGraceWindow(d time.Duration) {
	s.mu.Lock()
	s.graceWindow = d
	s.mu.Unlock()
}

// dedupe keeps one sample per metric and instance, the last decoded winning.
// Order is preserved so a batch still reads in the order the tree was walked.
func dedupe(in []models.Telemetry) []models.Telemetry {
	if len(in) < 2 {
		return in
	}
	index := make(map[string]int, len(in))
	out := make([]models.Telemetry, 0, len(in))
	for _, s := range in {
		key := s.Metric + "" + s.Instance
		if at, seen := index[key]; seen {
			out[at] = s
			continue
		}
		index[key] = len(out)
		out = append(out, s)
	}
	return out
}

// Sessions reports how many streams are running.
func (s *Subscriber) Sessions() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.sessions)
}

// Stop ends every session.
func (s *Subscriber) Stop() {
	s.mu.Lock()
	sessions := make([]*session, 0, len(s.sessions))
	for _, sess := range s.sessions {
		sessions = append(sessions, sess)
	}
	s.sessions = make(map[string]*session)
	s.mu.Unlock()

	for _, sess := range sessions {
		sess.cancel()
	}
	for _, sess := range sessions {
		select {
		case <-sess.done:
		case <-time.After(2 * time.Second):
		}
	}
}
