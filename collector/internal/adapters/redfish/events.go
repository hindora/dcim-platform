package redfish

import (
	"bytes"
	"context"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/hari/dcim-platform/collector/internal/assign"
	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// EventPath is the URL path BMCs POST events to.
const EventPath = "/redfish-events"

// EventReceiver accepts pushed Redfish events and reconciles the subscriptions
// that produce them.
//
// Three properties are non-negotiable and drive the whole design:
//
//  1. THE HANDLER MUST NOT BLOCK. A BMC gives the destination a few seconds
//     and does not retry. Talking to Redis inside the handler means events are
//     lost during exactly the incident that generated them, so the handler
//     decodes, queues and answers 204 immediately.
//
//  2. SUBSCRIPTIONS MUST BE RECONCILED, NOT CREATED ONCE. A BMC reset drops
//     every subscription silently - the simulator models this deliberately -
//     and orphans from a previous collector address accumulate until the
//     per-BMC cap (often 8-20) is hit, after which new subscriptions fail
//     with no visible symptom other than events quietly stopping.
//
//  3. EVENTS ARE NOT TELEMETRY. An event says a state changed; the value that
//     goes with it comes from the next poll.
type EventReceiver struct {
	adapter  *Adapter
	maps     *mapping.RedfishEventMap
	resolver *assign.Resolver
	sink     models.Sink
	log      *slog.Logger
	mets     *obs.Metrics

	listen      string
	destination string // what BMCs are told to POST to
	workers     int
	perMin      int

	mu     sync.Mutex
	seen   map[string]*rateWindow
	server *http.Server
}

type rateWindow struct {
	windowStart time.Time
	count       int
	dropped     int
}

type inboundEvent struct {
	doc    redfishEventDoc
	source string
	at     time.Time
}

// redfishEventDoc is the body a BMC POSTs. Only the fields that carry meaning
// are decoded; unknown members are ignored, which is what keeps the receiver
// working across firmware revisions.
type redfishEventDoc struct {
	Context string `json:"Context"`
	Events  []struct {
		EventType         string `json:"EventType"`
		EventID           string `json:"EventId"`
		Severity          string `json:"Severity"`
		Message           string `json:"Message"`
		MessageID         string `json:"MessageId"`
		MemberID          string `json:"MemberId"`
		EventTimestamp    string `json:"EventTimestamp"`
		OriginOfCondition struct {
			ODataID string `json:"@odata.id"`
		} `json:"OriginOfCondition"`
	} `json:"Events"`
}

func NewEventReceiver(a *Adapter, maps *mapping.RedfishEventMap,
	resolver *assign.Resolver, sink models.Sink, log *slog.Logger,
	mets *obs.Metrics, listen, destination string, workers, perMinute int) *EventReceiver {
	if workers < 1 {
		workers = 4
	}
	if perMinute <= 0 {
		perMinute = 120
	}
	return &EventReceiver{
		adapter: a, maps: maps, resolver: resolver, sink: sink, log: log,
		mets: mets, listen: listen, destination: destination,
		workers: workers, perMin: perMinute,
		seen: make(map[string]*rateWindow),
	}
}

func (r *EventReceiver) Protocol() string { return "redfish_event" }

// Destination is the URL BMCs are told to deliver to.
func (r *EventReceiver) Destination() string { return r.destination }

// Listen blocks until ctx is cancelled.
func (r *EventReceiver) Listen(ctx context.Context) error {
	// Deep buffer: a chassis-wide fault delivers a burst, and that burst is
	// precisely what must not be dropped.
	queue := make(chan inboundEvent, 4096)

	mux := http.NewServeMux()
	mux.HandleFunc(EventPath, func(w http.ResponseWriter, req *http.Request) {
		r.handleHTTP(w, req, queue)
	})
	r.server = &http.Server{
		Addr:              r.listen,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
	}

	var wg sync.WaitGroup
	for i := 0; i < r.workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-ctx.Done():
					return
				case in := <-queue:
					r.publish(ctx, in)
				}
			}
		}()
	}

	errCh := make(chan error, 1)
	go func() {
		err := r.server.ListenAndServe()
		if errors.Is(err, http.ErrServerClosed) {
			err = nil
		}
		errCh <- err
	}()

	r.log.Info("redfish event receiver listening", "addr", r.listen,
		"destination", r.destination, "workers", r.workers)

	select {
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = r.server.Shutdown(shutdown)
		wg.Wait()
		return nil
	case err := <-errCh:
		if err != nil {
			return fmt.Errorf("redfish event listener on %s: %w", r.listen, err)
		}
		return nil
	}
}

func (r *EventReceiver) handleHTTP(w http.ResponseWriter, req *http.Request,
	queue chan inboundEvent) {

	if req.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	source := hostOf(req.RemoteAddr)
	if !r.allow(source) {
		r.mets.TrapsTotal.WithLabelValues("redfish_rate_limited").Inc()
		// 204 anyway: a 429 makes some firmware disable the subscription.
		w.WriteHeader(http.StatusNoContent)
		return
	}

	var doc redfishEventDoc
	if err := json.NewDecoder(io.LimitReader(req.Body, 1<<20)).Decode(&doc); err != nil {
		r.mets.TrapsTotal.WithLabelValues("redfish_decode_error").Inc()
		r.log.Warn("undecodable redfish event", "source", source, "error", err)
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	// Answer BEFORE doing any work. The BMC's timeout is short and it will not
	// retry, so anything slow here is an event lost.
	w.WriteHeader(http.StatusNoContent)

	select {
	case queue <- inboundEvent{doc: doc, source: source, at: time.Now().UTC()}:
	default:
		r.mets.TrapsTotal.WithLabelValues("redfish_queue_full").Inc()
		r.log.Warn("redfish event queue full", "source", source)
	}
}

func (r *EventReceiver) publish(ctx context.Context, in inboundEvent) {
	if len(in.doc.Events) == 0 {
		return
	}
	out := make([]models.Event, 0, len(in.doc.Events))

	// Context carries the endpoint id we set at subscribe time; the source
	// address is only the fallback.
	ep, resolved := r.resolver.ResolveID(in.doc.Context)
	if !resolved {
		ep, resolved = r.resolver.Resolve(in.source, "")
	}

	for _, e := range in.doc.Events {
		class, known := r.maps.Classify(e.MessageID, e.Message, e.Severity)
		sev, ok := models.ParseSeverity(class.Severity)
		if !ok {
			sev = models.SeverityInfo
		}
		ev := models.Event{
			SourceIP:       in.source,
			EventType:      class.EventType,
			Instance:       mapping.InstanceFrom(class.InstanceFrom, e.Message),
			Severity:       sev,
			IsClear:        class.IsClear,
			Message:        e.Message,
			ObservedAt:     observedAt(e.EventTimestamp, in.at),
			CollectedAt:    models.NowMicros(),
			SourceProtocol: models.ProtocolRedfish,
			RawIdentifier:  e.MessageID,
			Varbinds: map[string]string{
				"event_type_wire": e.EventType,
				"origin":          e.OriginOfCondition.ODataID,
			},
		}
		if resolved {
			ev.EndpointID = ep.ID
			ev.DeviceID = ep.DeviceID
		} else {
			// Emitted, never dropped: an unattributable event is evidence
			// that inventory and the device plane disagree.
			r.mets.TrapsTotal.WithLabelValues("redfish_unresolved_source").Inc()
			r.log.Warn("redfish event from an unknown source",
				"source", in.source, "context", in.doc.Context)
		}
		if known {
			r.mets.TrapsTotal.WithLabelValues("redfish_ok").Inc()
		} else {
			r.mets.TrapsTotal.WithLabelValues("redfish_unknown_message").Inc()
		}
		ev.DedupKey = eventDedupKey(&ev)
		out = append(out, ev)
	}

	if err := r.sink.Events(ctx, out); err != nil {
		r.log.Warn("redfish event publish failed", "error", err)
	}
}

// observedAt prefers the BMC's own timestamp and falls back to arrival. A
// clock-skewed BMC is still better than no ordering at all, but an unparseable
// timestamp must never zero the field.
func observedAt(ts string, arrival time.Time) int64 {
	if ts != "" {
		for _, layout := range []string{time.RFC3339Nano, time.RFC3339,
			"2006-01-02T15:04:05Z"} {
			if t, err := time.Parse(layout, ts); err == nil {
				return t.UnixMicro()
			}
		}
	}
	return arrival.UnixMicro()
}

func eventDedupKey(ev *models.Event) string {
	h := sha1.New()
	_, _ = io.WriteString(h, ev.EndpointID+"|"+ev.SourceIP+"|"+ev.EventType+"|"+
		ev.Instance+"|"+ev.RawIdentifier)
	return hex.EncodeToString(h.Sum(nil))[:16]
}

func hostOf(remote string) string {
	if h, _, err := net.SplitHostPort(remote); err == nil {
		return h
	}
	return remote
}

func (r *EventReceiver) allow(source string) bool {
	now := time.Now()
	r.mu.Lock()
	defer r.mu.Unlock()
	w, ok := r.seen[source]
	if !ok || now.Sub(w.windowStart) > time.Minute {
		if ok && w.dropped > 0 {
			r.log.Warn("redfish event rate limit dropped events",
				"source", source, "dropped", w.dropped, "limit_per_minute", r.perMin)
		}
		r.seen[source] = &rateWindow{windowStart: now, count: 1}
		return true
	}
	if w.count >= r.perMin {
		w.dropped++
		return false
	}
	w.count++
	return true
}

// ------------------------------------------------------- reconciliation

// ReconcileAll brings every endpoint's subscriptions in line with this
// collector's destination. Errors are logged per endpoint, never fatal: one
// unreachable BMC must not stop the other 309 from being reconciled.
func (r *EventReceiver) ReconcileAll(ctx context.Context, endpoints []*models.Endpoint) {
	created, deleted, failed := 0, 0, 0
	for _, ep := range endpoints {
		if ep.Protocol != "redfish" {
			continue
		}
		c, d, err := r.Reconcile(ctx, ep)
		created += c
		deleted += d
		if err != nil {
			failed++
			r.log.Warn("redfish subscription reconcile failed",
				"endpoint", ep.ID, "address", ep.Address, "error", err)
		}
	}
	r.log.Info("redfish subscriptions reconciled",
		"created", created, "deleted_stale", deleted, "failed", failed)
}

// Reconcile makes one BMC's subscriptions match our destination exactly.
func (r *EventReceiver) Reconcile(ctx context.Context, ep *models.Endpoint) (int, int, error) {
	s, err := r.adapter.ensureSession(ctx, ep)
	if err != nil {
		return 0, 0, err
	}
	base := s.base + "/EventService/Subscriptions"

	body, err := r.adapter.get(ctx, ep, s, base)
	if err != nil {
		return 0, 0, err
	}

	want := r.destination
	haveWanted := false
	deleted := 0

	for _, m := range arrayOf(body, "Members") {
		uri, _ := m["@odata.id"].(string)
		if uri == "" {
			continue
		}
		sub, err := r.adapter.get(ctx, ep, s, uri)
		if err != nil {
			continue
		}
		dest, _ := sub["Destination"].(string)
		switch {
		case dest == want:
			// Keep exactly one. A duplicate delivers every event twice, and
			// the second copy is indistinguishable from a real repeat.
			if haveWanted {
				if err := r.adapter.delete(ctx, ep, s, uri); err == nil {
					deleted++
				}
				continue
			}
			haveWanted = true
		case isOurs(dest, want):
			// Same path, different host or port: an orphan from a previous
			// collector address. These are what fill the per-BMC cap.
			if err := r.adapter.delete(ctx, ep, s, uri); err == nil {
				deleted++
			}
		}
		// Anything else belongs to another consumer and is left alone.
	}

	if haveWanted {
		return 0, deleted, nil
	}

	payload := map[string]any{
		"Destination": want,
		"EventTypes":  []string{"Alert", "StatusChange", "ResourceUpdated"},
		"Protocol":    "Redfish",
		// The endpoint id, so a delivered event identifies its source without
		// depending on the address it arrives from.
		"Context": ep.ID,
	}
	if err := r.adapter.post(ctx, ep, s, base, payload); err != nil {
		return 0, deleted, err
	}
	return 1, deleted, nil
}

// isOurs reports whether a destination is a stale copy of ours: same path,
// different address. Matching on the path alone is what makes a collector
// recognise its own orphans after its IP or port changes.
func isOurs(dest, want string) bool {
	if dest == "" || dest == want {
		return false
	}
	du, err1 := url.Parse(dest)
	wu, err2 := url.Parse(want)
	if err1 != nil || err2 != nil {
		return false
	}
	return du.Path == wu.Path && du.Path != ""
}

// RunReconciler reconciles at startup and then on a fixed interval.
func (r *EventReceiver) RunReconciler(ctx context.Context, every time.Duration,
	endpoints func() []*models.Endpoint) {

	if every <= 0 {
		every = 10 * time.Minute
	}
	r.ReconcileAll(ctx, endpoints())
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			r.ReconcileAll(ctx, endpoints())
		}
	}
}

// -------------------------------------------------- adapter HTTP verbs

func (a *Adapter) post(ctx context.Context, ep *models.Endpoint, s *session,
	path string, payload any) error {
	raw, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		a.url(ep, s, path), bytes.NewReader(raw))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	return a.send(ep, s, req, path)
}

func (a *Adapter) delete(ctx context.Context, ep *models.Endpoint, s *session,
	path string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete,
		a.url(ep, s, path), nil)
	if err != nil {
		return err
	}
	return a.send(ep, s, req, path)
}

func (a *Adapter) send(ep *models.Endpoint, s *session,
	req *http.Request, path string) error {
	if s.token != "" {
		req.Header.Set("X-Auth-Token", s.token)
	}
	resp, err := a.client(s.verifyTLS, ep.Poll.Timeout()).Do(req)
	if err != nil {
		return fmt.Errorf("%w: %v", models.ErrUnreachable, err)
	}
	defer func() { _, _ = io.Copy(io.Discard, resp.Body); resp.Body.Close() }()

	if resp.StatusCode == http.StatusUnauthorized {
		a.Forget(ep.ID)
		return fmt.Errorf("%w: token rejected", models.ErrAuth)
	}
	if resp.StatusCode >= 300 {
		return fmt.Errorf("%w: %s %s returned %d", models.ErrProtocolStatus,
			req.Method, path, resp.StatusCode)
	}
	return nil
}

// DefaultDestination builds the URL BMCs POST to, from the collector's
// advertised address.
//
// It defaults to http, NOT https. A BMC that cannot verify the receiver's
// certificate drops every event silently - there is no error anywhere, the
// events simply stop - so https is opt-in and only worth it with a
// certificate the BMCs actually trust.
func DefaultDestination(advertise string, useTLS bool) string {
	scheme := "http"
	if useTLS {
		scheme = "https"
	}
	advertise = strings.TrimSuffix(advertise, "/")
	return fmt.Sprintf("%s://%s%s", scheme, advertise, EventPath)
}
