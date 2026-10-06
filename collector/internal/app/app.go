// Package app wires the collector together and owns its lifecycle.
package app

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/hari/dcim-platform/collector/internal/adapters/bacnet"
	"github.com/hari/dcim-platform/collector/internal/adapters/gnmi"
	"github.com/hari/dcim-platform/collector/internal/adapters/modbus"
	"github.com/hari/dcim-platform/collector/internal/adapters/provider"
	"github.com/hari/dcim-platform/collector/internal/adapters/redfish"
	"github.com/hari/dcim-platform/collector/internal/adapters/snmp"
	"github.com/hari/dcim-platform/collector/internal/assign"
	"github.com/hari/dcim-platform/collector/internal/capacity"
	"github.com/hari/dcim-platform/collector/internal/config"
	"github.com/hari/dcim-platform/collector/internal/discovery"
	"github.com/hari/dcim-platform/collector/internal/health"
	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/mtls"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/internal/publish"
	"github.com/hari/dcim-platform/collector/internal/sched"
	"github.com/hari/dcim-platform/collector/internal/sealedbox"
	"github.com/hari/dcim-platform/collector/internal/spool"
	"github.com/hari/dcim-platform/collector/internal/throttle"
	"github.com/hari/dcim-platform/collector/internal/vault"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// sink is what App needs from a publisher, satisfied by both publish.
// Publisher (direct-Redis) and publish.GatewayPublisher (spool-backed
// HTTPS, docs/26 Phase 3) - see either's SpoolStats/ReplayRateRPS/Capacity/
// Dropped/QueueDepth docstrings for which of these five report something
// real for that transport and which are deliberately 0.
type sink interface {
	models.Sink
	Heartbeat(ctx context.Context, hb models.CollectorHeartbeat) error
	Run(ctx context.Context)
	Ping(ctx context.Context) error
	Capacity() int
	Dropped() uint64
	QueueDepth() int
	SpoolStats() spool.Stats
	ReplayRateRPS() uint32
}

type App struct {
	cfg     *config.Config
	log     *slog.Logger
	mets    *obs.Metrics
	ready   *obs.Readiness
	rdb     *redis.Client
	pub     sink
	sp      *spool.Spool // nil unless cfg.Transport.Mode == "gateway"
	tracker *health.Tracker
	sched   *sched.Scheduler
	assign  *assign.Client

	// Liveness, on its own wheel and its own cadence: a Pinger probe for every
	// endpoint polled less often than availEvery. Nil when disabled.
	avail      *sched.Scheduler
	availEvery time.Duration

	snmp     *snmp.Adapter
	redfish  *redfish.Adapter
	rfEvents *redfish.EventReceiver
	bacnet   *bacnet.Adapter
	modbus   *modbus.Adapter
	provider *provider.Adapter
	gnmi     *gnmi.Adapter
	gnmiSubs *gnmi.Subscriber
	// The context streams live under. Held on the App because assignment
	// changes arrive on a callback that has no context of its own, and a
	// stream has to outlive the change that started it.
	streamCtx context.Context
	traps     *snmp.TrapReceiver

	// The trap receiver is the one part of the collector that can be moved
	// while it runs: it owns its socket and its workers, so it can be closed
	// and reopened in place. Everything else is read once, when the adapters
	// are built.
	trapTable *mapping.TrapTable
	// SNMPv3 engine clock for the trap receiver, and its notion of each
	// sender's: here, not on the receiver, so a rebuild keeps them.
	engineBoots uint32
	engineStart time.Time
	trapTimes   *snmp.EngineTimes
	trapMu      sync.Mutex
	trapCfg     config.TrapCfg
	trapStop    context.CancelFunc
	trapDone    chan struct{}

	cfgClient *config.RemoteClient
	// What the process actually booted with, kept to answer "does this change
	// need a restart" honestly rather than by assumption.
	bootProtocols any
	cfgErrMu      sync.Mutex
	cfgErr        string
	resolver      *assign.Resolver
	adapters      map[string]models.Adapter

	startedAt time.Time
	// Atomic: every poll worker bumps these concurrently, and the heartbeat
	// reads them from its own goroutine.
	pollsOK  atomic.Uint64
	pollsBad atomic.Uint64

	// Poll-worker capacity (docs/26 Phase 5): samples per protocol and pool,
	// and the trailing window the heartbeat reports over.
	points   *capacity.Points
	capTrack capacity.Tracker
	// The facility's limits (internal/throttle), shared by the poll and
	// liveness schedulers: a probe and a poll at one gateway are two
	// requests to one device.
	limiter *throttle.Limiter

	// Where mapping data came from and its fingerprint - reported in every
	// heartbeat so the platform can catch a collector running data it does
	// not recognise, which a version string alone would not.
	mappingSource    string
	mappingBundleSHA string

	// nil for a collector that has never enrolled - every http.Client built
	// with it then presents no client certificate. Live: a certificate
	// renewed after construction is picked up on the next connection with
	// nothing here to update, because it came from an mtls.Store.
	tlsConfig *tls.Config

	// One BBMD registration loop per distinct BBMD address this collector
	// should be foreign-registered with - the config's own, plus one per
	// pool in the assignment that has one (docs/26 Phase 5/9). Reconciled
	// on every assignment refresh; see fdr.go.
	fdrMu    sync.Mutex
	fdrLoops map[string]fdrLoop
	fdrCtx   context.Context
}

// tlsStore is nil for a collector run without ever having enrolled - every
// http.Client built here then presents no client certificate, the ordinary
// case for a collector still on its bearer token alone.
func New(cfg *config.Config, version string, tlsStore *mtls.Store) (*App, error) {
	var tlsConfig *tls.Config
	if tlsStore != nil {
		tlsConfig = tlsStore.TLSConfig()
	}

	log := obs.NewLogger(cfg.Observability.LogLevel, cfg.Observability.LogFormat,
		cfg.Collector.ID)
	mets := obs.NewMetrics()

	// The credential-sealing keypair (docs/26 Phase 4) is independent of
	// mTLS enrollment: generated and persisted here unconditionally, same
	// as any other piece of this collector's local state. It does nothing
	// until the platform has this collector's public half on file - a
	// bearer-token-only collector, or one that enrolled before ever
	// registering a key, simply keeps getting plaintext credentials, the
	// same as before this phase existed.
	sealKP, err := sealedbox.LoadOrGenerate(cfg.Collector.StateDir)
	if err != nil {
		return nil, fmt.Errorf("prepare credential-sealing key: %w", err)
	}

	// Redis is only ever dialled for the direct transport. A gateway-
	// transport collector reaches the platform over the internet and Redis's
	// own port and password are not something that connection can expose -
	// see config.Config.Transport's docstring.
	var rdb *redis.Client
	if cfg.Transport.Mode == "redis" {
		opts, err := redis.ParseURL(cfg.Redis.URL)
		if err != nil {
			return nil, fmt.Errorf("parse redis url: %w", err)
		}
		rdb = redis.NewClient(opts)
	}

	mapFS, mapSource := mapping.Resolve(cfg.Mappings.Dir)
	bundleSHA, err := mapping.BundleSHA(mapFS)
	if err != nil {
		return nil, fmt.Errorf("fingerprint mappings (%s): %w", mapSource, err)
	}

	maps, err := mapping.Load(mapFS)
	if err != nil {
		return nil, fmt.Errorf("load mappings: %w", err)
	}
	log.Info("mappings loaded", "profiles", maps.Names(), "source", mapSource,
		"bundle_sha", bundleSHA[:12])

	var pub sink
	var sp *spool.Spool
	if cfg.Transport.Mode == "gateway" {
		sp, err = spool.Open(spool.Config{Dir: cfg.Transport.Gateway.SpoolDir})
		if err != nil {
			return nil, fmt.Errorf("open spool: %w", err)
		}
		pub, err = publish.NewGateway(cfg, log, mets, tlsConfig, sp)
		if err != nil {
			sp.Close()
			return nil, fmt.Errorf("build gateway publisher: %w", err)
		}
		log.Info("transport: gateway (spool-backed HTTPS)",
			"spool_dir", cfg.Transport.Gateway.SpoolDir)
	} else {
		pub = publish.New(rdb, cfg, log, mets)
	}
	tracker := health.NewTracker(cfg.Health.OfflineThreshold, cfg.Collector.ID,
		pub, log, mets, cfg.Health.RefreshInterval)

	a := &App{
		cfg: cfg, log: log, mets: mets, ready: &obs.Readiness{},
		rdb: rdb, pub: pub, sp: sp, tracker: tracker,
		adapters:         map[string]models.Adapter{},
		startedAt:        time.Now().UTC(),
		mappingSource:    mapSource,
		mappingBundleSHA: bundleSHA,
		tlsConfig:        tlsConfig,
	}

	if cfg.Protocols.SNMP.Enabled {
		a.snmp = snmp.New(maps, log, mets, cfg.Protocols.SNMP.MaxRepetitions,
			cfg.Protocols.SNMP.AcceptAnySourceReply)
		a.adapters["snmp"] = a.snmp
	}

	if cfg.Protocols.Redfish.Enabled {
		rfMaps, err := mapping.LoadRedfish(mapFS)
		if err != nil {
			return nil, fmt.Errorf("load redfish mappings: %w", err)
		}
		a.redfish = redfish.New(rfMaps, log, mets)
		a.adapters["redfish"] = a.redfish
		log.Info("redfish adapter enabled")
	}

	if cfg.Protocols.BACnet.Enabled {
		bnMaps, err := mapping.LoadBACnet(mapFS)
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
		mbMaps, err := mapping.LoadModbus(mapFS)
		if err != nil {
			return nil, fmt.Errorf("load modbus templates: %w", err)
		}
		client := modbus.NewClient(cfg.Protocols.Modbus.Timeout,
			cfg.Protocols.Modbus.Retries, log)
		a.modbus = modbus.New(mbMaps, client, log, mets)
		a.adapters["modbus"] = a.modbus
		log.Info("modbus adapter enabled", "templates", len(mbMaps.Templates))
	}

	if cfg.Protocols.Provider.Enabled {
		a.provider = provider.New(log, mets, cfg.Protocols.Provider.Timeout)
		a.adapters["provider"] = a.provider
		log.Info("provider adapter enabled")
	}

	if cfg.Protocols.GNMI.Enabled {
		gnMaps, err := mapping.LoadGNMI(mapFS)
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
		evMaps, err := mapping.LoadRedfishEvents(mapFS)
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

	// The receiver itself is built only by startTraps, at Run and on every
	// trap config change. A second construction here once took the engine ID
	// and was then replaced, so the receiver that ran had none and no SNMPv3
	// INFORM was ever answered. The mappings load even with traps disabled:
	// a config change can enable them on a running collector.
	trapTable, err := mapping.LoadTraps(mapFS)
	if err != nil {
		return nil, fmt.Errorf("load trap mappings: %w", err)
	}
	log.Info("trap mappings loaded", "wire_oids", trapTable.Len())
	a.trapTable = trapTable
	a.trapCfg = cfg.Protocols.SNMPTrap
	a.engineBoots = nextEngineBoots(cfg.Collector.StateDir, log)
	a.engineStart = time.Now()
	a.trapTimes = snmp.NewEngineTimes()
	if len(a.adapters) == 0 {
		return nil, fmt.Errorf("no protocol adapters enabled")
	}

	a.points = capacity.NewPoints()
	a.limiter = throttle.New()
	a.sched = sched.New(sched.Options{
		Limiter:   a.limiter,
		Budgeted:  true,
		Workers:   cfg.Workers.PoolSize,
		QueueSize: cfg.Workers.PoolSize * cfg.Workers.QueueMultiplier,
		ProtoLimits: map[string]int{
			"snmp":     cfg.Protocols.SNMP.MaxConcurrent,
			"redfish":  cfg.Protocols.Redfish.MaxConcurrent,
			"bacnet":   cfg.Protocols.BACnet.MaxConcurrent,
			"modbus":   cfg.Protocols.Modbus.MaxConcurrent,
			"gnmi":     cfg.Protocols.GNMI.MaxConcurrent,
			"provider": cfg.Protocols.Provider.MaxConcurrent,
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
			// The provider's own API gateway, not a device - its rate limit
			// is what per_host is bounding here, not a serial trunk.
			"provider": cfg.Protocols.Provider.PerHost,
		},
	}, a.poll, log, mets)

	if every := cfg.Health.AvailabilityInterval; every > 0 {
		a.availEvery = every
		// Small and separate. A probe is one request, and sharing the poll
		// pool would put a 30 s liveness check behind a 600 s ifTable walk -
		// the exact wait it exists to avoid. Per-host 1: two probes at one
		// agent at once would only measure each other.
		a.avail = sched.New(sched.Options{
			Limiter:   a.limiter,
			Workers:   16,
			QueueSize: 16 * 64,
			ProtoLimits: map[string]int{
				"snmp": 8, "redfish": 16,
			},
			PerHostLimits: map[string]int{"snmp": 1, "redfish": 1},
		}, a.ping, log, mets)
	}

	a.assign = assign.New(cfg, log, mets, tlsConfig, sealKP)
	if cfg.Vault.HashiCorp.Enabled {
		reg := vault.NewRegistry()
		reg.Register("hashicorp", vault.NewHashiCorpResolver(vault.HashiCorpConfig{
			Addr: cfg.Vault.HashiCorp.Addr, RoleID: cfg.Vault.HashiCorp.RoleID,
			SecretID:        cfg.VaultHashiCorpSecretID(),
			SecretIDWrapped: cfg.Vault.HashiCorp.SecretIDWrapped,
			Namespace:       cfg.Vault.HashiCorp.Namespace,
			MountPath:       cfg.Vault.HashiCorp.MountPath,
			Timeout:         cfg.Vault.HashiCorp.Timeout,
		}))
		a.assign.SetVaultRegistry(reg)
		log.Info("vault: hashicorp resolver enabled", "addr", cfg.Vault.HashiCorp.Addr)
	}
	a.assign.OnChange = a.applyDiff
	a.assign.OnRefreshed = a.refreshResolver
	a.cfg.Collector.Version = version
	return a, nil
}

// redfishSweeper builds the Redfish half of a sweep, or nil if it is not wanted.
//
// Nil rather than a disabled sweeper: a run then does exactly what it did before
// Redfish existed, and there is no chance of an unconfigured deployment quietly
// probing 443 across its management network.
func (a *App) redfishSweeper() *discovery.RedfishSweeper {
	cfg := a.cfg.Discovery.Redfish
	if !cfg.Enabled {
		return nil
	}
	creds := make([]discovery.RedfishCredential, 0, len(cfg.Credentials))
	for _, c := range cfg.Credentials {
		creds = append(creds, discovery.RedfishCredential{
			Username: c.Username, Password: c.Password})
	}
	// Said out loud, because the two halves fail differently: with no credentials
	// a sweep still FINDS every BMC (the service root is unauthenticated) and
	// reports none of their serials - which looks like a working sweep that cannot
	// match anything.
	a.log.Info("discovery will sweep redfish",
		"ports", cfg.Ports, "credentials", len(creds),
		"allow_plaintext", cfg.AllowPlaintext)
	return &discovery.RedfishSweeper{
		Ports:          cfg.Ports,
		AllowPlaintext: cfg.AllowPlaintext,
		Credentials:    creds,
		Log:            a.log,
	}
}

// discoveryCommunities picks how a sweep will ask, from configuration.
//
// This used to be the literal discovery.PerAddressCommunity - the simulator's
// convention, compiled in - so the sweep could not have found a real device. The
// strategy is now a deployment's decision, and the log line says which one is in
// force because a sweep that finds nothing is otherwise indistinguishable from a
// network with nothing on it.
func (a *App) discoveryCommunities() discovery.CommunityFor {
	if a.cfg.Discovery.CommunityIsAddress {
		a.log.Info("discovery will use each address as its own community",
			"reason", "community_is_address is set; this suits an snmpsim-backed plane")
		return discovery.PerAddressCommunity
	}
	list := a.cfg.Discovery.Communities
	a.log.Info("discovery communities", "count", len(list))
	return discovery.Communities(list...)
}

func (a *App) Run(ctx context.Context) error {
	obs.Serve(ctx, a.cfg.Observability.MetricsListen,
		a.cfg.Observability.HealthListen, a.ready, a.mets, a.log)

	if a.rdb != nil {
		if err := a.rdb.Ping(ctx).Err(); err != nil {
			// Not fatal: the publisher buffers and the collector still polls. A
			// collector that refuses to start because Redis is briefly down is
			// worse than one that starts degraded and says so.
			a.log.Error("redis unreachable at startup; starting degraded", "error", err)
			a.tracker.SetSelfDegraded(true)
		} else {
			a.ready.SetRedis(true)
		}
	} else {
		// Gateway transport: readiness for this leg is judged by the
		// publisher's own Ping (a platform reachability probe), polled the
		// same way the heartbeat loop already reports it below.
		a.ready.SetRedis(true)
	}

	for name, adapter := range a.adapters {
		if err := adapter.Init(ctx); err != nil {
			return fmt.Errorf("init %s adapter: %w", name, err)
		}
	}
	a.ready.SetAdapters(true)

	// BBMD registrations: the config's own, if any, now - and one per pool
	// with a BBMD once the first assignment lands (reconcileFDR runs again
	// on every refresh, see refreshResolver).
	a.fdrCtx = ctx
	a.reconcileFDR()

	go a.pub.Run(ctx)

	a.startTraps(ctx, a.trapCfg)
	if a.cfgClient != nil {
		a.cfgClient.OnChange = func(version uint32, o config.Overrides) {
			a.applyConfig(ctx, version, o)
		}
		go a.cfgClient.Run(ctx)
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
		a.log.Info("initial assignment", "endpoints", a.assign.Count())
	}
	go a.assign.Run(ctx)
	// docs/26 Phase 7: finish (confirm or report) an upgrade this process is
	// part of, then long-poll for commands.
	a.resumeUpgrade(ctx)
	go a.commandLoop(ctx)

	// Discovery is opt-in per run: nothing sweeps unless an operator queues a
	// run, so this goroutine is idle until there is work.
	if a.cfg.Protocols.SNMP.Enabled {
		go (&discovery.Runner{
			BaseURL:     a.cfg.DCIM.BaseURL,
			CollectorID: a.cfg.Collector.ID,
			Token:       a.cfg.Token,
			Interval:    a.cfg.DCIM.AssignmentInterval,
			Sweeper:     discovery.New(a.log, a.discoveryCommunities(), 0),
			Redfish:     a.redfishSweeper(),
			HTTP: &http.Client{Timeout: a.cfg.DCIM.RequestTimeout,
				Transport: &http.Transport{TLSClientConfig: a.tlsConfig}},
			Log: a.log,
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
	if a.avail != nil {
		a.avail.Start(ctx)
	}
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
	go func() {
		a.sched.Wait()
		if a.avail != nil {
			a.avail.Wait()
		}
		close(done)
	}()
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
	if a.rdb != nil {
		_ = a.rdb.Close()
	}
	if a.sp != nil {
		// Flushes whatever is still only in memory - the other half of "kill
		// -9 loses at most the flush interval": a clean stop loses nothing.
		if err := a.sp.Close(); err != nil {
			a.log.Warn("spool close failed", "error", err)
		}
	}
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
		a.pollsBad.Add(1)
		a.mets.PollsTotal.WithLabelValues(ep.Protocol, ep.DeviceType, "failure").Inc()
		a.tracker.Failure(ep, err)
		// It put next to nothing on the network: refund its reservation.
		a.limiter.Charge(ep, 0, time.Now())
		a.log.Debug("poll failed", "endpoint_id", ep.ID, "device", ep.DeviceName,
			"error", err)
		return
	}

	result := "success"
	if outcome.Partial {
		result = "partial"
	}
	a.pollsOK.Add(1)
	a.points.Add(ep.Protocol, ep.PoolID, len(outcome.Samples))
	a.limiter.Charge(ep, len(outcome.Samples), time.Now())
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

// ping runs one liveness probe and feeds the same health tracker the poll
// does: a device is OFFLINE when it stops answering, whichever check noticed.
func (a *App) ping(ctx context.Context, ep *models.Endpoint) {
	pinger, ok := a.adapters[ep.Protocol].(models.Pinger)
	if !ok {
		return
	}
	started := time.Now()
	if err := pinger.Ping(ctx, ep); err != nil {
		a.tracker.Failure(ep, err)
		a.log.Debug("liveness probe failed", "endpoint_id", ep.ID,
			"device", ep.DeviceName, "error", err)
		return
	}
	a.tracker.Success(ep, int(time.Since(started).Milliseconds()))
}

// watchAvailability puts an endpoint on the liveness wheel when its adapter
// can probe and its poll is slower than the probe - otherwise the poll already
// is the liveness check - and takes it off again when neither holds.
func (a *App) watchAvailability(ep *models.Endpoint) {
	if a.avail == nil {
		return
	}
	_, pingable := a.adapters[ep.Protocol].(models.Pinger)
	if pingable && ep.Poll.Interval() > a.availEvery {
		a.avail.AddEvery(ep, a.availEvery)
		a.tracker.SetCheckInterval(ep, a.availEvery)
		return
	}
	a.avail.Remove(ep.ID)
	a.tracker.SetCheckInterval(ep, 0)
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

// refreshResolver rebuilds the trap resolver from the owned endpoints and the
// rest of the estate. Run after every fetch that returned a body, not only on
// a diff: the resolve list moves when other collectors' endpoints do.
func (a *App) refreshResolver() {
	a.resolver.Replace(a.assign.Endpoints(), a.assign.Resolve(), a.assign.Site())
	// Pool settings ride the same refresh: a BBMD added to a pool in the UI
	// is registered with on the next fetch, with nothing restarted.
	a.reconcileFDR()
	a.limiter.SetBudgets(budgetShares(a.assign.Pools()))
	a.limiter.Sync(a.assign.Endpoints())
	a.syncTrapUSM()
}

// receiverEngineID is this collector's own SNMPv3 engine ID, which a device
// sending an INFORM localises its keys to: RFC 3411 format 4 (text) under
// net-snmp's enterprise number, from the collector id - stable across
// restarts, unique per collector, at most 32 octets.
func receiverEngineID(collectorID string) string {
	id := collectorID
	if len(id) > 27 {
		id = id[:27]
	}
	return string([]byte{0x80, 0x00, 0x1f, 0x88, 0x04}) + id
}

// syncTrapUSM gives the trap receiver the SNMPv3 user its pools' devices
// send as - the pool's default SNMP credential when it is v3. A collector
// normally serves one pool; if its pools disagree, the first by id is used
// and the rest said so, since one listener takes one user.
func (a *App) syncTrapUSM() {
	traps := a.trapReceiver()
	if traps == nil {
		return
	}
	pools := a.assign.Pools()
	ids := make([]string, 0, len(pools))
	for id := range pools {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	var chosen *models.Credential
	chosenPool := ""
	for _, id := range ids {
		c := pools[id].SNMPv3Credential
		if c == nil || c.Data == nil {
			continue
		}
		if chosen == nil {
			chosen, chosenPool = c, id
		} else if c.Data["security_name"] != chosen.Data["security_name"] {
			a.log.Warn("pools carry different SNMPv3 trap users; receiving as one of them",
				"using_pool", chosenPool, "ignored_pool", id)
		}
	}
	if err := traps.SetUSM(chosen); err != nil {
		a.log.Error("SNMPv3 trap credential unusable; receiving v2c only", "error", err)
		_ = traps.SetUSM(nil)
	}
}

// trapReceiver is the running receiver, or nil. startTraps swaps it under
// trapMu while the heartbeat and assignment loops read it.
func (a *App) trapReceiver() *snmp.TrapReceiver {
	a.trapMu.Lock()
	defer a.trapMu.Unlock()
	return a.traps
}

// budgetShares is this collector's slice of each pool's budget, as the
// platform computed it from what it owns. A pool the platform sent no share
// for has no budget here.
func budgetShares(pools map[string]assign.Pool) map[string]float64 {
	out := make(map[string]float64, len(pools))
	for id, p := range pools {
		if p.RateBudgetShare != nil && *p.RateBudgetShare > 0 {
			out[id] = *p.RateBudgetShare
		}
	}
	return out
}

// startupSpreadFor is how long after start an assignment counts as the
// initial one: the first fetch can fail and land a few retries later.
const startupSpreadFor = 90 * time.Second

// handoffWindow spreads a handed-over batch's first polls over at least 15 s,
// and over n/20 s for a large batch (twenty first polls a second), so a whole
// pool failing over to one member is a short ramp, not a single tick.
func handoffWindow(n int) time.Duration {
	w := time.Duration(n) * time.Second / 20
	if w < 15*time.Second {
		w = 15 * time.Second
	}
	return w
}

func (a *App) applyDiff(diff assign.Diff) {
	// The resolver turns a trap's source address into a device, so it has to
	// track the full assignment rather than the diff.
	a.refreshResolver()
	if a.gnmiSubs != nil && a.streamCtx != nil {
		// The subscriber diffs the full assignment itself: a stream is a
		// long-lived session keyed on the endpoint, not something to start and
		// stop from a delta. Before Run has set the context there is nothing
		// to attach a session to, and Run performs the first Manage itself.
		a.gnmiSubs.Manage(a.streamCtx, a.assign.Endpoints())
	}

	// Endpoints that arrive once this process is past its start-up (a
	// failover, failback, drain or rebalance handing them over) are polled
	// within a short window instead of at their spread slot: their previous
	// owner has already stopped. At start-up the full spread stays, so a
	// restart does not poll the whole assignment in one burst.
	soon := handoffWindow(len(diff.Added))
	handoff := time.Since(a.startedAt) > startupSpreadFor
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
		if handoff {
			a.sched.AddSoon(ep, soon)
		} else {
			a.sched.Add(ep)
		}
		a.watchAvailability(ep)
	}
	for _, ep := range diff.Changed {
		if a.streamed(ep) {
			// A profile change can turn a polled endpoint into a streamed one.
			a.sched.Remove(ep.ID)
			if a.avail != nil {
				a.avail.Remove(ep.ID)
			}
			continue
		}
		a.sched.Add(ep)
		a.watchAvailability(ep)
	}
	for _, ep := range diff.Removed {
		a.sched.Remove(ep.ID)
		if a.avail != nil {
			a.avail.Remove(ep.ID)
		}
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
				CollectorID: a.cfg.Collector.ID,
				Version:     a.cfg.Collector.Version,
				Hostname:    hostname,
				StartedAt:   a.startedAt.UnixMicro(),
				SentAt:      models.NowMicros(),
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
				PollsTotal:      a.pollsOK.Load() + a.pollsBad.Load(),
				PollsFailed:     a.pollsBad.Load(),
				QueueDepth:      uint32(a.pub.QueueDepth()),
				// These five were in the contract from the start and never
				// filled in, so the platform checks that read them could not
				// fire. Queue capacity turns depth into a fraction; drops and
				// assignment age are what collector_degraded and
				// assignment_stale are judged on.
				QueueCapacity:     uint32(a.pub.Capacity()),
				PublishDropped:    a.pub.Dropped(),
				AssignmentAgeS:    uint32(a.assign.AgeSeconds()),
				AssignmentVersion: uint32(a.assign.Version()),
				ActiveStreams:     uint32(a.streamCount()),
				MappingBundleSha:  a.mappingBundleSHA,
			}
			// Zero for the direct-Redis transport (Publisher.SpoolStats/
			// ReplayRateRPS are stubs) and real for the gateway transport -
			// see either's docstring. Filled in generically here so neither
			// transport has to know it is the one being reported.
			if rep := a.capTrack.Observe(capacity.Read(a.sched, a.points)); rep != nil {
				if raw, err := json.Marshal(rep); err == nil {
					hb.Capacity = string(raw)
				}
			}
			spoolStats := a.pub.SpoolStats()
			hb.SpoolBytes = uint64(spoolStats.Bytes)
			hb.SpoolOldestAgeS = uint32(spoolStats.OldestAge.Seconds())
			hb.ReplayRate = a.pub.ReplayRateRPS()
			if traps := a.trapReceiver(); traps != nil {
				hb.TrapsReceived = traps.Received()
			}
			if a.rfEvents != nil {
				hb.EventsReceived = a.rfEvents.Received()
			}
			// Which configuration this process is running, as opposed to what
			// the database was last told to store. Without these three the
			// settings page can only report what it saved, which is not the
			// same claim.
			if a.cfgClient != nil {
				hb.ConfigVersion = a.cfgClient.Version()
				hb.ConfigRestartPending = a.restartPending()
				hb.ConfigError = a.configError()
			}
			// What this process is effectively running, override included.
			// Sent unconditionally: a collector with no stored config at all
			// still has to be able to show an operator its own settings, and
			// that is the case on every fresh install.
			a.trapMu.Lock()
			trap := a.trapCfg
			a.trapMu.Unlock()
			hb.ConfigEffective = config.Effective(a.cfg, trap)
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
