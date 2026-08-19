// Package app wires the collector together and owns its lifecycle.
package app

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/hari/dcim-platform/collector/internal/adapters/redfish"
	"github.com/hari/dcim-platform/collector/internal/adapters/snmp"
	"github.com/hari/dcim-platform/collector/internal/assign"
	"github.com/hari/dcim-platform/collector/internal/config"
	"github.com/hari/dcim-platform/collector/internal/health"
	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/internal/publish"
	"github.com/hari/dcim-platform/collector/internal/sched"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

type App struct {
	cfg     *config.Config
	log     *slog.Logger
	mets    *obs.Metrics
	ready   *obs.Readiness
	rdb     *redis.Client
	pub     *publish.Publisher
	tracker *health.Tracker
	sched   *sched.Scheduler
	assign  *assign.Client

	snmp     *snmp.Adapter
	redfish  *redfish.Adapter
	rfEvents *redfish.EventReceiver
	traps    *snmp.TrapReceiver
	resolver *assign.Resolver
	adapters map[string]models.Adapter

	startedAt time.Time
	pollsOK   uint64
	pollsBad  uint64
}

func New(cfg *config.Config, version string) (*App, error) {
	log := obs.NewLogger(cfg.Observability.LogLevel, cfg.Observability.LogFormat,
		cfg.Collector.ID)
	mets := obs.NewMetrics()

	opts, err := redis.ParseURL(cfg.Redis.URL)
	if err != nil {
		return nil, fmt.Errorf("parse redis url: %w", err)
	}
	rdb := redis.NewClient(opts)

	maps, err := mapping.Load(cfg.Mappings.Dir)
	if err != nil {
		return nil, fmt.Errorf("load mappings: %w", err)
	}
	log.Info("mappings loaded", "profiles", maps.Names(), "dir", cfg.Mappings.Dir)

	pub := publish.New(rdb, cfg, log, mets)
	tracker := health.NewTracker(cfg.Health.OfflineThreshold, cfg.Collector.ID,
		pub, log, mets)

	a := &App{
		cfg: cfg, log: log, mets: mets, ready: &obs.Readiness{},
		rdb: rdb, pub: pub, tracker: tracker,
		adapters:  map[string]models.Adapter{},
		startedAt: time.Now().UTC(),
	}

	if cfg.Protocols.SNMP.Enabled {
		a.snmp = snmp.New(maps, log, mets, cfg.Protocols.SNMP.MaxRepetitions,
			cfg.Protocols.SNMP.AcceptAnySourceReply)
		a.adapters["snmp"] = a.snmp
	}

	if cfg.Protocols.Redfish.Enabled {
		rfMaps, err := mapping.LoadRedfish(cfg.Mappings.Dir)
		if err != nil {
			return nil, fmt.Errorf("load redfish mappings: %w", err)
		}
		a.redfish = redfish.New(rfMaps, log, mets)
		a.adapters["redfish"] = a.redfish
		log.Info("redfish adapter enabled")
	}

	a.resolver = assign.NewResolver()

	if cfg.Protocols.RedfishEvent.Enabled {
		if a.redfish == nil {
			return nil, fmt.Errorf("redfish_event needs the redfish adapter enabled")
		}
		if cfg.Protocols.RedfishEvent.Advertise == "" {
			return nil, fmt.Errorf("redfish_event.advertise is required: the " +
				"collector cannot guess which of its addresses the BMCs can reach")
		}
		evMaps, err := mapping.LoadRedfishEvents(cfg.Mappings.Dir)
		if err != nil {
			return nil, fmt.Errorf("load redfish event mappings: %w", err)
		}
		dest := redfish.DefaultDestination(cfg.Protocols.RedfishEvent.Advertise,
			cfg.Protocols.RedfishEvent.TLS)
		a.rfEvents = redfish.NewEventReceiver(a.redfish, evMaps, a.resolver, pub,
			log, mets, cfg.Protocols.RedfishEvent.Listen, dest,
			cfg.Protocols.RedfishEvent.Workers,
			cfg.Protocols.RedfishEvent.RateLimitPerMinute)
		log.Info("redfish event receiver enabled", "destination", dest,
			"message_ids", len(evMaps.MessageIDs), "patterns", len(evMaps.Patterns))
	}

	if cfg.Protocols.SNMPTrap.Enabled {
		trapTable, err := mapping.LoadTraps(cfg.Mappings.Dir)
		if err != nil {
			return nil, fmt.Errorf("load trap mappings: %w", err)
		}
		log.Info("trap mappings loaded", "wire_oids", trapTable.Len())
		a.traps = snmp.NewTrapReceiver(trapTable, a.resolver, pub, log, mets,
			cfg.Protocols.SNMPTrap.Listen, cfg.Protocols.SNMPTrap.Workers,
			cfg.Protocols.SNMPTrap.RateLimitPerMinute)
	}
	if len(a.adapters) == 0 {
		return nil, fmt.Errorf("no protocol adapters enabled")
	}

	a.sched = sched.New(sched.Options{
		Workers:   cfg.Workers.PoolSize,
		QueueSize: cfg.Workers.PoolSize * cfg.Workers.QueueMultiplier,
		ProtoLimits: map[string]int{
			"snmp":    cfg.Protocols.SNMP.MaxConcurrent,
			"redfish": cfg.Protocols.Redfish.MaxConcurrent,
		},
		PerHostLimits: map[string]int{
			"snmp":    cfg.Protocols.SNMP.PerHost,
			"redfish": cfg.Protocols.Redfish.PerHost,
		},
	}, a.poll, log, mets)

	a.assign = assign.New(cfg, log, mets)
	a.assign.OnChange = a.applyDiff
	a.cfg.Collector.Version = version
	return a, nil
}

func (a *App) Run(ctx context.Context) error {
	obs.Serve(ctx, a.cfg.Observability.MetricsListen,
		a.cfg.Observability.HealthListen, a.ready, a.mets, a.log)

	if err := a.rdb.Ping(ctx).Err(); err != nil {
		// Not fatal: the publisher buffers and the collector still polls. A
		// collector that refuses to start because Redis is briefly down is
		// worse than one that starts degraded and says so.
		a.log.Error("redis unreachable at startup; starting degraded", "error", err)
		a.tracker.SetSelfDegraded(true)
	} else {
		a.ready.SetRedis(true)
	}

	for name, adapter := range a.adapters {
		if err := adapter.Init(ctx); err != nil {
			return fmt.Errorf("init %s adapter: %w", name, err)
		}
	}
	a.ready.SetAdapters(true)

	go a.pub.Run(ctx)

	if a.traps != nil {
		go func() {
			// A trap listener that cannot bind must not take the collector
			// down: polling still works, and the operator needs to see which
			// half is broken.
			if err := a.traps.Listen(ctx); err != nil {
				a.log.Error("trap receiver stopped", "error", err)
			}
		}()
	}

	if a.rfEvents != nil {
		go func() {
			// Same rule as the trap listener: a receiver that cannot bind
			// must not take polling down with it.
			if err := a.rfEvents.Listen(ctx); err != nil {
				a.log.Error("redfish event receiver stopped", "error", err)
			}
		}()
	}

	// First assignment is fetched synchronously: starting with an empty work
	// list and filling it a tick later makes the startup logs lie.
	if err := a.assign.Refresh(ctx); err != nil {
		a.log.Error("initial assignment fetch failed", "error", err)
		a.tracker.SetSelfDegraded(true)
	} else {
		a.ready.SetAssignment(true)
		a.resolver.Replace(a.assign.Endpoints())
		a.log.Info("initial assignment", "endpoints", a.assign.Count())
	}
	go a.assign.Run(ctx)

	if a.rfEvents != nil {
		// Reconciliation runs AFTER the first assignment, so the very first
		// pass sees the real endpoint list rather than subscribing to nothing.
		go a.rfEvents.RunReconciler(ctx, a.cfg.Protocols.RedfishEvent.ReconcileEvery,
			a.assign.Endpoints)
	}

	a.sched.Start(ctx)
	go a.heartbeatLoop(ctx)
	go a.gaugeLoop(ctx)

	a.log.Info("collector running",
		"collector_id", a.cfg.Collector.ID,
		"endpoints", a.sched.Count(),
		"workers", a.cfg.Workers.PoolSize)

	<-ctx.Done()
	a.log.Info("shutting down")

	// Wait for in-flight polls, then let the publisher flush. Order matters:
	// flushing first would drop whatever those polls produce.
	done := make(chan struct{})
	go func() { a.sched.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(15 * time.Second):
		a.log.Warn("timed out waiting for in-flight polls")
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for name, adapter := range a.adapters {
		if err := adapter.Close(shutdownCtx); err != nil {
			a.log.Warn("adapter close failed", "adapter", name, "error", err)
		}
	}
	_ = a.rdb.Close()
	a.log.Info("stopped")
	return nil
}

// poll runs one endpoint and is the only place a poll outcome is turned into
// health, metrics and published telemetry.
func (a *App) poll(ctx context.Context, ep *models.Endpoint) {
	adapter, ok := a.adapters[ep.Protocol]
	if !ok {
		return
	}

	started := time.Now()
	outcome, err := adapter.Poll(ctx, ep)
	elapsed := time.Since(started)
	a.mets.PollDuration.WithLabelValues(ep.Protocol, ep.DeviceType).
		Observe(elapsed.Seconds())

	if err != nil {
		a.pollsBad++
		a.mets.PollsTotal.WithLabelValues(ep.Protocol, ep.DeviceType, "failure").Inc()
		a.tracker.Failure(ep, err)
		a.log.Debug("poll failed", "endpoint_id", ep.ID, "device", ep.DeviceName,
			"error", err)
		return
	}

	result := "success"
	if outcome.Partial {
		result = "partial"
	}
	a.pollsOK++
	a.mets.PollsTotal.WithLabelValues(ep.Protocol, ep.DeviceType, result).Inc()
	for _, miss := range outcome.Misses {
		a.mets.MissesTotal.WithLabelValues(ep.Protocol, miss.Reason).Inc()
	}
	a.tracker.Success(ep, int(elapsed.Milliseconds()))

	if err := a.pub.Telemetry(ctx, outcome.Samples); err != nil {
		a.log.Warn("publish failed", "error", err, "endpoint_id", ep.ID)
	}
	if len(outcome.Events) > 0 {
		_ = a.pub.Events(ctx, outcome.Events)
	}
}

func (a *App) applyDiff(diff assign.Diff) {
	// The resolver turns a trap's source address into a device, so it has to
	// track the full assignment rather than the diff.
	a.resolver.Replace(a.assign.Endpoints())

	for _, ep := range diff.Added {
		if _, ok := a.adapters[ep.Protocol]; !ok {
			// An endpoint for a protocol this build does not implement is not
			// an error - it is a phase that has not landed yet.
			continue
		}
		a.tracker.Register(ep)
		a.sched.Add(ep)
	}
	for _, ep := range diff.Changed {
		a.sched.Add(ep)
	}
	for _, ep := range diff.Removed {
		a.sched.Remove(ep.ID)
		a.tracker.Forget(ep.ID)
		if a.snmp != nil {
			a.snmp.Forget(ep.ID)
		}
		if a.redfish != nil {
			a.redfish.Forget(ep.ID)
		}
	}
}

func (a *App) heartbeatLoop(ctx context.Context) {
	ticker := time.NewTicker(a.cfg.Observability.HeartbeatEvery)
	defer ticker.Stop()
	hostname, _ := os.Hostname()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			// Assignment staleness makes the collector self-degraded, which
			// stops the health tracker condemning endpoints it simply cannot
			// see right now.
			stale := a.assign.Stale()
			a.tracker.SetSelfDegraded(stale)
			if stale {
				a.log.Warn("assignment is stale; not condemning endpoints")
			}

			hb := models.CollectorHeartbeat{
				CollectorID:     a.cfg.Collector.ID,
				Version:         a.cfg.Collector.Version,
				Hostname:        hostname,
				StartedAt:       a.startedAt.UnixMicro(),
				SentAt:          models.NowMicros(),
				EndpointsOwned:  uint32(a.sched.Count()),
				EndpointsOnline: uint32(a.tracker.OnlineCount()),
				PollsTotal:      a.pollsOK + a.pollsBad,
				PollsFailed:     a.pollsBad,
				QueueDepth:      uint32(a.pub.QueueDepth()),
			}
			if err := a.pub.Heartbeat(ctx, hb); err != nil {
				a.log.Warn("heartbeat publish failed", "error", err)
				a.ready.SetRedis(false)
			} else {
				a.ready.SetRedis(true)
			}
		}
	}
}

func (a *App) gaugeLoop(ctx context.Context) {
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			for proto, byStatus := range a.tracker.Counts() {
				for status, n := range byStatus {
					a.mets.Endpoints.WithLabelValues(proto, status).Set(float64(n))
				}
			}
		}
	}
}
