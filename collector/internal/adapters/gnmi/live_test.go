package gnmi

import (
	"context"
	"os"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/health"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// Live tests against a running device plane. Skipped unless DCIM_LIVE_GNMI
// names targets:
//
//	DCIM_LIVE_GNMI="10.51.21.11,10.51.11.12" \
//	  go test ./internal/adapters/gnmi/ -run Live -v
//
// Target syntax: <ip>[:<port>]. Port defaults to 50051.

func parseLiveTargets(spec string) []string {
	var out []string
	for _, raw := range strings.Split(spec, ",") {
		if raw = strings.TrimSpace(raw); raw != "" {
			out = append(out, raw)
		}
	}
	return out
}

func liveEndpoint(t *testing.T, raw string) *models.Endpoint {
	t.Helper()
	host, port := raw, 50051
	if h, p, ok := strings.Cut(raw, ":"); ok {
		host = h
		n, err := strconv.Atoi(p)
		if err != nil {
			t.Fatalf("bad port in %q", raw)
		}
		port = n
	}
	return &models.Endpoint{
		ID: "live-" + raw, DeviceID: "live-dev", DeviceType: "switch",
		Protocol: "gnmi", Address: host, Port: port, Role: "native_card",
		Addressing: map[string]any{"target": host, "insecure": true},
		Poll:       models.PollProfile{IntervalS: 30, TimeoutMs: 8000},
	}
}

func TestLiveGet(t *testing.T) {
	spec := os.Getenv("DCIM_LIVE_GNMI")
	if spec == "" {
		t.Skip("set DCIM_LIVE_GNMI to run against a live device plane")
	}
	a := newAdapter(t)

	for _, raw := range parseLiveTargets(spec) {
		t.Run(raw, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
			defer cancel()

			out, err := a.Poll(ctx, liveEndpoint(t, raw))
			if err != nil {
				t.Fatalf("poll: %v", err)
			}
			if len(out.Samples) == 0 {
				t.Fatal("no samples from a live device")
			}

			byMetric := map[string]int{}
			lines := make([]string, 0, 24)
			for _, s := range out.Samples {
				byMetric[s.Metric]++
				if byMetric[s.Metric] <= 2 {
					key := s.Metric
					if s.Instance != "" {
						key += "{" + s.Instance + "}"
					}
					lines = append(lines, key+" = "+
						strconv.FormatFloat(s.DoubleValue, 'f', 2, 64)+" "+s.Unit)
				}
			}
			sort.Strings(lines)
			for _, l := range lines {
				t.Log(l)
			}
			t.Logf("%d samples across %d metrics", len(out.Samples), len(byMetric))
			for _, m := range out.Misses {
				t.Logf("MISS %s (%s)", m.Metric, m.Reason)
			}

			// A decoder that misreads JSON_IETF still returns numbers; the
			// registry's bounds are what catch it.
			for _, s := range out.Samples {
				def, ok := models.ValidateMetric(s.Metric)
				if !ok {
					t.Errorf("%s is not in the registry", s.Metric)
					continue
				}
				if def.HasMin && s.DoubleValue < def.MinValid {
					t.Errorf("%s = %v, below the registry minimum %v",
						s.Metric, s.DoubleValue, def.MinValid)
				}
				if def.HasMax && s.DoubleValue > def.MaxValid {
					t.Errorf("%s = %v, above the registry maximum %v",
						s.Metric, s.DoubleValue, def.MaxValid)
				}
			}
		})
	}
}

// The point of gNMI: the device pushes and the collector stops polling.
func TestLiveStream(t *testing.T) {
	spec := os.Getenv("DCIM_LIVE_GNMI")
	if spec == "" {
		t.Skip("set DCIM_LIVE_GNMI to run against a live device plane")
	}
	targets := parseLiveTargets(spec)

	a := newAdapter(t)
	sink := &captureSink{}
	tracker := health.NewTracker(3, "col-live", sink, testLogger(), obs.NewMetrics())
	sub := NewSubscriber(a, a.conns, loadMaps(t), sink, tracker, testLogger(),
		obs.NewMetrics(), 3)

	eps := make([]*models.Endpoint, 0, len(targets))
	for _, raw := range targets {
		ep := liveEndpoint(t, raw)
		ep.Poll = models.PollProfile{IntervalS: 0, TimeoutMs: 8000, PushEnabled: true}
		eps = append(eps, ep)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	sub.Manage(ctx, eps)
	defer sub.Stop()

	// Long enough for the initial snapshot; the periodic push follows at the
	// mapping's sample interval, which is deliberately not waited for here.
	deadline := time.Now().Add(30 * time.Second)
	for sink.count() == 0 && time.Now().Before(deadline) {
		time.Sleep(100 * time.Millisecond)
	}
	if sink.count() == 0 {
		t.Fatal("no samples arrived on the stream")
	}

	byMetric := map[string]int{}
	for _, s := range sink.all() {
		byMetric[s.Metric]++
	}
	t.Logf("%d streamed samples across %d metrics from %d target(s)",
		sink.count(), len(byMetric), len(eps))
	for m, n := range byMetric {
		t.Logf("  %-22s %d", m, n)
	}
	if byMetric["if_in_octets"] == 0 {
		t.Error("no interface counters arrived on the stream")
	}
}
