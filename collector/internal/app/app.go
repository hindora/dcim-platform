// Package app wires the collector together and owns its lifecycle.
package app

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/hari/dcim-platform/collector/internal/adapters/bacnet"
	"github.com/hari/dcim-platform/collector/internal/adapters/gnmi"
	"github.com/hari/dcim-platform/collector/internal/adapters/modbus"
	"github.com/hari/dcim-platform/collector/internal/adapters/redfish"
	"github.com/hari/dcim-platform/collector/internal/adapters/snmp"
	"github.com/hari/dcim-platform/collector/internal/assign"
	"github.com/hari/dcim-platform/collector/internal/config"
	"github.com/hari/dcim-platform/collector/internal/discovery"
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
	bacnet   *bacnet.Adapter
	modbus   *modbus.Adapter
	gnmi     *gnmi.Adapter
	gnmiSubs *gnmi.Subscriber
	// The context streams live under. Held on the App because assignment
	// changes arrive on a callback that has no context of its own, and a
	// stream has to outlive the change that started it.
	streamCtx context.Context
	traps     *snmp.TrapReceiver
	resolver  *assign.Resolver
	adapters  map[string]models.Adapter

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
		pub, log, mets, cfg.Health.RefreshInterval)

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

	if cfg.Protocols.BACnet.Enabled {
		bnMaps, err := mapping.LoadBACnet(cfg.Mappings.Dir)
		if err != nil {
			return nil, fmt.Errorf("load bacnet mappings: %w", err)
		}
		client := bacnet.NewClient(cfg.Protocols.BACnet.LocalPort,
			cfg.Protocols.BACnet.Timeout, cfg.Protocols.BACnet.Retries, log)
		a.bacnet = bacnet.New(bnMaps, client, log, mets,
			cfg.Protocols.BACnet.BatchSize)
		a.adapters["bacnet"] = a.bacnet
		log.Info("bacnet adapter enabled",
			"device_types", len(bnMaps.DeviceTypes),
			"batch_size", cfg.Protocols.BACnet.BatchSize)
	}

	if cfg.Protocols.Modbus.Enabled {
		mbMaps, err := mapping.LoadModbus(cfg.Mappings.Dir)
		if err != nil {
			return nil, fmt.Errorf("load modbus templates: %w", err)
		}
		client := modbus.NewClient(cfg.Protocols.Modbus.Timeout,
			cfg.Protocols.Modbus.Retries, log)
		a.modbus = modbus.New(mbMaps, client, log, mets)
		a.adapters["modbus"] = a.modbus
		log.Info("modbus adapter enabled", "templates", len(mbMaps.Templates))
	}

	if cfg.Protocols.GNMI.Enabled {
		gnMaps, err := mapping.LoadGNMI(cfg.Mappings.Dir)
		if err != nil {
			return nil, fmt.Errorf("load gnmi mappings: %w", err)
		}
		pool := gnmi.NewConnPool(cfg.Protocols.GNMI.Timeout, log)
		a.gnmi = gnmi.New(gnMaps, pool, log, mets)
		a.adapters["gnmi"] = a.gnmi
		if cfg.Protocols.GNMI.Stream {
			a.gnmiSubs = gnmi.NewSubscriber(a.gnmi, pool, gnMaps, pub, tracker,
				log, mets, cfg.Protocols.GNMI.StreamGraceFactor)
			if cfg.Protocols.GNMI.GraceWindow > 0 {
				a.gnmiSubs.SetGraceWindow(cfg.Protocols.GNMI.GraceWindow)
			}
		}
		log.Info("gnmi adapter enabled", "subscriptions", len(gnMaps.Subscriptions),
			"stream", cfg.Protocols.GNMI.Stream)
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
			"bacnet":  cfg.Protocols.BACnet.MaxConcurrent,
			"modbus":  cfg.Protocols.Modbus.MaxConcurrent,
			"gnmi":    cfg.Protocols.GNMI.MaxConcurrent,
		},
		PerHostLimits: map[string]int{
			"snmp":    cfg.Protocols.SNMP.PerHost,
			"redfish": cfg.Protocols.Redfish.PerHost,
			// Per HOST, not per device: an MS/TP router fronts a whole trunk,
			// and hammering it in parallel is how a trunk saturates.
			"bacnet": cfg.Protocols.BACnet.PerHost,
			// Same reasoning, more strictly: a Modbus serial gateway forwards
			// one RS-485 transaction at a time, so parallel requests only
			// queue inside the gateway where the collector cannot see them.
			"modbus": cfg.Protocols.Modbus.PerHost,
			"gnmi":   cfg.Protocols.GNMI.PerHost,
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

	// Discovery is opt-in per run: nothing sweeps unless an operator queues a
	// run, so this goroutine is idle until there is work.
	if a.cfg.Protocols.SNMP.Enabled {
		go (&discovery.Runner{
			BaseURL:  a.cfg.DCIM.BaseURL,
			Token:    a.cfg.Token,
			Interval: a.cfg.DCIM.AssignmentInterval,
			Sweeper:  discovery.New(a.log, discovery.PerAddressCommunity, 0),
			HTTP:     &http.Client{Timeout: a.cfg.DCIM.RequestTimeout},
			Log:      a.log,
		}).Run(ctx)
	}

	if a.rfEvents != nil {
		// Reconciliation runs AFTER the first assignment, so the very first
		// pass sees the real endpoint list rather than subscribing to nothing.
		go a.rfEvents.RunReconciler(ctx, a.cfg.Protocols.RedfishEvent.ReconcileEvery,
			a.assign.Endpoints)
	}

	if a.gnmiSubs != nil {
		a.streamCtx = ctx
		a.gnmiSubs.Manage(ctx, a.assign.Endpoints())
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
	if a.gnmiSubs != nil {
		a.gnmiSubs.Stop()
	}
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

// streamCount is how many endpoints the subscriber holds, and 0 when this
// build has no subscriber - so `owned` stays the scheduler alone rather than
// silently gaining a phantom population.
func (a *App) streamCount() int {
	if a.gnmiSubs == nil {
		return 0
	}
	return a.gnmiSubs.Sessions()
}

// streamed reports an endpoint the gNMI subscriber owns rather than the
// scheduler: a zero interval with push enabled, which is what the gnmi-stream
// poll profile means.
func (a *App) streamed(ep *models.Endpoint) bool {
	return a.gnmiSubs != nil && gnmi.StreamOnly(ep)
}

func (a *App) applyDiff(diff assign.Diff) {
	// The resolver turns a trap's source address into a device, so it has to
	// track the full assignment rather than the diff.
	a.resolver.Replace(a.assign.Endpoints())
	if a.gnmiSubs != nil && a.streamCtx != nil {
		// The subscriber diffs the full assignment itself: a stream is a
		// long-lived session keyed on the endpoint, not something to start and
		// stop from a delta. Before Run has set the context there is nothing
		// to attach a session to, and Run performs the first Manage itself.
		a.gnmiSubs.Manage(a.streamCtx, a.assign.Endpoints())
	}

	for _, ep := range diff.Added {
		if _, ok := a.adapters[ep.Protocol]; !ok {
			// An endpoint for a protocol this build does not implement is not
			// an error - it is a phase that has not landed yet.
			continue
		}
		if a.streamed(ep) {
			// Handed to the subscriber below. Scheduling it as well would
			// collect the same device twice by two different mechanisms, and
			// the duplicate samples are indistinguishable from real ones.
			continue
		}
		a.tracker.Register(ep)
		a.sched.Add(ep)
	}
	for _, ep := range diff.Changed {
		if a.streamed(ep) {
			// A profile change can turn a polled endpoint into a streamed one.
			a.sched.Remove(ep.ID)
			continue
		}
		a.sched.Add(ep)
	}
	for _, ep := range diff.Removed {
		a.sched.Remove(ep.ID)
		a.tracker.Forget(ep.ID)
		if a.snmp != nil {
			a.snmp.Forget(ep.ID)
		}
		if a.bacnet != nil {
			a.bacnet.Forget(ep.ID)
		}
		if a.modbus != nil {
			a.modbus.Forget(ep.ID)
		}
		if a.gnmi != nil {
			a.gnmi.Forget(ep.ID)
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
				// Owned is the scheduler PLUS the streams. A stream-only
				// gNMI endpoint is deliberately kept out of the scheduler -
				// polling it as well would collect the same device twice -
				// but the collector owns it just as much, and the health
				// tracker counts it online like any other.
				//
				// Counting only the scheduler made owned exclude a population
				// that online included, so the heartbeat reported 1344 online
				// out of 1340 owned. That is not a collector in trouble, it is
				// two counts measuring different sets, and it raised a
				// permanent `collector_degraded` that no operator could act
				// on. Now the same set is on both sides: online below owned
				// means a real coverage gap, and above it is impossible.
				EndpointsOwned:  uint32(a.sched.Count() + a.streamCount()),
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
