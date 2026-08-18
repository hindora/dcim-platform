// Package obs holds logging, Prometheus metrics and the health endpoints.
//
// Cardinality discipline: no metric is ever labelled with an endpoint or device
// id. At a few hundred devices times several protocols that is an explosion,
// and it is the most common way a Prometheus install is killed. Per-device
// detail belongs in the poll_result hypertable, which is built for it.
package obs

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync/atomic"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

type Metrics struct {
	PollsTotal    *prometheus.CounterVec
	PollDuration  *prometheus.HistogramVec
	PollsSkipped  *prometheus.CounterVec
	PollsShed     *prometheus.CounterVec
	SamplesTotal  *prometheus.CounterVec
	MissesTotal   *prometheus.CounterVec
	Endpoints     *prometheus.GaugeVec
	FailuresTotal *prometheus.CounterVec

	PublishQueueDepth prometheus.Gauge
	PublishBatchSize  prometheus.Histogram
	PublishDuration   prometheus.Histogram
	PublishDropped    *prometheus.CounterVec

	AssignmentVersion prometheus.Gauge
	AssignmentAge     prometheus.Gauge
	AssignmentErrors  prometheus.Counter
}

func NewMetrics() *Metrics {
	return &Metrics{
		PollsTotal: promauto.NewCounterVec(prometheus.CounterOpts{
			Name: "dcim_collector_polls_total",
			Help: "Polls by protocol, device type and result.",
		}, []string{"protocol", "device_type", "result"}),

		PollDuration: promauto.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "dcim_collector_poll_duration_seconds",
			Help:    "Poll round-trip duration.",
			Buckets: []float64{.01, .05, .1, .25, .5, 1, 2.5, 5, 10},
		}, []string{"protocol", "device_type"}),

		PollsSkipped: promauto.NewCounterVec(prometheus.CounterOpts{
			Name: "dcim_collector_poll_skipped_total",
			Help: "Polls skipped because the previous one was still running.",
		}, []string{"protocol"}),

		PollsShed: promauto.NewCounterVec(prometheus.CounterOpts{
			Name: "dcim_collector_poll_shed_total",
			Help: "Polls dropped because the worker queue was full.",
		}, []string{"protocol"}),

		SamplesTotal: promauto.NewCounterVec(prometheus.CounterOpts{
			Name: "dcim_collector_samples_emitted_total",
			Help: "Canonical samples emitted.",
		}, []string{"protocol"}),

		MissesTotal: promauto.NewCounterVec(prometheus.CounterOpts{
			Name: "dcim_collector_misses_total",
			Help: "Metrics a device did not return, by reason.",
		}, []string{"protocol", "reason"}),

		Endpoints: promauto.NewGaugeVec(prometheus.GaugeOpts{
			Name: "dcim_collector_endpoints",
			Help: "Endpoints owned, by protocol and communication status.",
		}, []string{"protocol", "status"}),

		FailuresTotal: promauto.NewCounterVec(prometheus.CounterOpts{
			Name: "dcim_collector_endpoint_failures_total",
			Help: "Endpoint failures by error class.",
		}, []string{"protocol", "error_class"}),

		PublishQueueDepth: promauto.NewGauge(prometheus.GaugeOpts{
			Name: "dcim_collector_publish_queue_depth",
			Help: "Samples waiting to be published.",
		}),
		PublishBatchSize: promauto.NewHistogram(prometheus.HistogramOpts{
			Name:    "dcim_collector_publish_batch_size",
			Help:    "Samples per published batch.",
			Buckets: []float64{1, 10, 50, 100, 250, 500, 1000},
		}),
		PublishDuration: promauto.NewHistogram(prometheus.HistogramOpts{
			Name:    "dcim_collector_publish_duration_seconds",
			Help:    "Time to publish one batch.",
			Buckets: []float64{.001, .005, .01, .05, .1, .5, 1},
		}),
		PublishDropped: promauto.NewCounterVec(prometheus.CounterOpts{
			Name: "dcim_collector_publish_dropped_total",
			Help: "Messages dropped rather than published.",
		}, []string{"stream", "reason"}),

		AssignmentVersion: promauto.NewGauge(prometheus.GaugeOpts{
			Name: "dcim_collector_assignment_version",
			Help: "Version of the assignment currently in effect.",
		}),
		AssignmentAge: promauto.NewGauge(prometheus.GaugeOpts{
			Name: "dcim_collector_assignment_age_seconds",
			Help: "Age of the last successful assignment fetch.",
		}),
		AssignmentErrors: promauto.NewCounter(prometheus.CounterOpts{
			Name: "dcim_collector_assignment_errors_total",
			Help: "Failed assignment fetches.",
		}),
	}
}

// NewLogger configures slog. Structured JSON unless a human is watching.
func NewLogger(level, format, collectorID string) *slog.Logger {
	var lvl slog.Level
	switch strings.ToLower(level) {
	case "debug":
		lvl = slog.LevelDebug
	case "warn":
		lvl = slog.LevelWarn
	case "error":
		lvl = slog.LevelError
	default:
		lvl = slog.LevelInfo
	}

	opts := &slog.HandlerOptions{Level: lvl}
	var h slog.Handler
	if strings.EqualFold(format, "text") {
		h = slog.NewTextHandler(os.Stderr, opts)
	} else {
		h = slog.NewJSONHandler(os.Stderr, opts)
	}
	return slog.New(h).With("service", "collector", "collector_id", collectorID)
}

// Readiness is flipped by the app once every subsystem is up. A collector that
// is not ready keeps collecting on its last known assignment: /ready removes it
// from a load balancer, it does not stop the work.
type Readiness struct {
	redisOK      atomic.Bool
	assignmentOK atomic.Bool
	adaptersOK   atomic.Bool
}

func (r *Readiness) SetRedis(ok bool)      { r.redisOK.Store(ok) }
func (r *Readiness) SetAssignment(ok bool) { r.assignmentOK.Store(ok) }
func (r *Readiness) SetAdapters(ok bool)   { r.adaptersOK.Store(ok) }

func (r *Readiness) Snapshot() (bool, map[string]bool) {
	checks := map[string]bool{
		"redis":      r.redisOK.Load(),
		"assignment": r.assignmentOK.Load(),
		"adapters":   r.adaptersOK.Load(),
	}
	for _, ok := range checks {
		if !ok {
			return false, checks
		}
	}
	return true, checks
}

// Serve starts the metrics and health listeners. Both shut down with ctx.
func Serve(ctx context.Context, metricsAddr, healthAddr string, ready *Readiness,
	log *slog.Logger) {

	metricsMux := http.NewServeMux()
	metricsMux.Handle("/metrics", promhttp.Handler())
	metricsSrv := &http.Server{Addr: metricsAddr, Handler: metricsMux,
		ReadHeaderTimeout: 5 * time.Second}

	healthMux := http.NewServeMux()
	healthMux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
	})
	healthMux.HandleFunc("/ready", func(w http.ResponseWriter, _ *http.Request) {
		ok, checks := ready.Snapshot()
		w.Header().Set("Content-Type", "application/json")
		if !ok {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"ready": ok, "checks": checks})
	})
	healthSrv := &http.Server{Addr: healthAddr, Handler: healthMux,
		ReadHeaderTimeout: 5 * time.Second}

	go func() {
		if err := metricsSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Error("metrics listener failed", "error", err)
		}
	}()
	go func() {
		if err := healthSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Error("health listener failed", "error", err)
		}
	}()
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = metricsSrv.Shutdown(shutdownCtx)
		_ = healthSrv.Shutdown(shutdownCtx)
	}()
}
