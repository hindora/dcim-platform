package snmp

import (
	"context"
	"crypto/sha1"
	"encoding/hex"
	"fmt"
	"log/slog"
	"net"
	"strings"
	"sync"
	"time"

	g "github.com/gosnmp/gosnmp"

	"github.com/hari/dcim-platform/collector/internal/assign"
	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// snmpTrapOID is the varbind that carries the actual notification OID in a
// v2c trap. The trap's identity is its VALUE, not the varbind's own name.
const snmpTrapOID = "1.3.6.1.6.3.1.1.4.1.0"

// sysUpTimeOID is the first varbind of every v2c trap and is not a payload.
const trapSysUpTime = "1.3.6.1.2.1.1.3.0"

// TrapReceiver listens for SNMP traps and turns them into canonical events.
//
// Traps are NOT polling and must not be modelled as such: a trap says a state
// changed, and the value that goes with it comes from the next poll. Writing
// telemetry from a trap produces sawtooth charts.
type TrapReceiver struct {
	table    *mapping.TrapTable
	resolver *assign.Resolver
	sink     models.Sink
	log      *slog.Logger
	mets     *obs.Metrics
	listen   string
	workers  int

	// Per-source rate limiting: one flapping interface must not be able to
	// fill the stream or the disk.
	mu       sync.Mutex
	seen     map[string]*rateWindow
	perMin   int
	listener *g.TrapListener
}

type rateWindow struct {
	windowStart time.Time
	count       int
	dropped     int
}

func NewTrapReceiver(table *mapping.TrapTable, resolver *assign.Resolver,
	sink models.Sink, log *slog.Logger, mets *obs.Metrics,
	listen string, workers, perMinute int) *TrapReceiver {
	if workers < 1 {
		workers = 4
	}
	if perMinute <= 0 {
		perMinute = 100
	}
	return &TrapReceiver{
		table: table, resolver: resolver, sink: sink, log: log, mets: mets,
		listen: listen, workers: workers, perMin: perMinute,
		seen: make(map[string]*rateWindow),
	}
}

func (t *TrapReceiver) Protocol() string { return "snmp_trap" }

// Listen blocks until ctx is cancelled.
func (t *TrapReceiver) Listen(ctx context.Context) error {
	type inbound struct {
		packet *g.SnmpPacket
		addr   *net.UDPAddr
		at     time.Time
	}
	// Generous buffer: traps are lossy by design and a burst during an outage
	// is exactly when they matter most. Decoding inline would drop packets
	// while the handler talks to Redis.
	queue := make(chan inbound, 10000)

	listener := g.NewTrapListener()
	listener.Params = g.Default
	listener.OnNewTrap = func(p *g.SnmpPacket, addr *net.UDPAddr) {
		select {
		case queue <- inbound{packet: p, addr: addr, at: time.Now().UTC()}:
		default:
			t.mets.TrapsTotal.WithLabelValues("queue_full").Inc()
		}
	}
	t.listener = listener

	var wg sync.WaitGroup
	for i := 0; i < t.workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-ctx.Done():
					return
				case in := <-queue:
					t.handle(ctx, in.packet, in.addr, in.at)
				}
			}
		}()
	}

	errCh := make(chan error, 1)
	go func() { errCh <- listener.Listen(t.listen) }()

	t.log.Info("trap receiver listening", "addr", t.listen,
		"mappings", t.table.Len(), "workers", t.workers)

	select {
	case <-ctx.Done():
		listener.Close()
		wg.Wait()
		return nil
	case err := <-errCh:
		return fmt.Errorf("trap listener on %s: %w", t.listen, err)
	}
}

func (t *TrapReceiver) handle(ctx context.Context, p *g.SnmpPacket,
	addr *net.UDPAddr, at time.Time) {

	source := ""
	if addr != nil {
		source = addr.IP.String()
	}
	if !t.allow(source) {
		t.mets.TrapsTotal.WithLabelValues("rate_limited").Inc()
		return
	}

	trapOID, varbinds := splitTrap(p)
	if trapOID == "" {
		t.mets.TrapsTotal.WithLabelValues("no_trap_oid").Inc()
		t.log.Warn("trap without an snmpTrapOID varbind", "source", source)
		return
	}

	ev := models.Event{
		SourceIP:       source,
		Instance:       "",
		ObservedAt:     at.UnixMicro(),
		CollectedAt:    models.NowMicros(),
		SourceProtocol: models.ProtocolSNMPTrap,
		RawIdentifier:  trapOID,
		Varbinds:       varbinds,
	}

	// The sender's device type disambiguates an OID that carries more than one
	// meaning; it stays empty when the trap cannot be attributed, in which case
	// only the unrestricted meanings can apply.
	senderType := ""
	if ep, ok := t.resolver.Resolve(source, p.Community); ok {
		ev.EndpointID = ep.ID
		ev.DeviceID = ep.DeviceID
		senderType = ep.DeviceType
	} else {
		// Recorded, not dropped: an unattributable trap is evidence that
		// inventory and the network disagree, which is itself worth seeing.
		t.mets.TrapsTotal.WithLabelValues("unresolved_source").Inc()
		t.log.Warn("trap from an unknown source", "source", source, "oid", trapOID)
	}

	def, known := t.table.Lookup(trapOID, senderType)
	if !known {
		// Never drop an unknown trap. It becomes an INFO event carrying the
		// raw OID so the gap in the mapping is visible in the UI rather than
		// invisible in a counter.
		ev.EventType = "unknown_trap"
		ev.Severity = models.SeverityInfo
		ev.Message = "unmapped trap " + trapOID
		t.mets.TrapsTotal.WithLabelValues("unknown_oid").Inc()
	} else {
		ev.EventType = def.EventType
		ev.Severity = severityFromName(def.Severity)
		ev.IsClear = def.IsClear
		ev.Message = describe(def, trapOID)
		if def.InstanceFromVarbind != "" {
			if v, ok := varbinds[def.InstanceFromVarbind]; ok {
				ev.Instance = v
			}
		}
		if len(def.Clears) > 0 {
			// Carried through so the alarm engine can resolve a whole family
			// from one clear without knowing anything about SNMP.
			ev.Varbinds["_clears"] = strings.Join(def.Clears, ",")
		}
		t.mets.TrapsTotal.WithLabelValues("ok").Inc()
	}

	ev.DedupKey = dedupKey(&ev)

	if err := t.sink.Events(ctx, []models.Event{ev}); err != nil {
		t.log.Warn("trap publish failed", "error", err, "oid", trapOID)
	}
}

// allow rate-limits per source with a rolling one-minute window.
func (t *TrapReceiver) allow(source string) bool {
	now := time.Now()
	t.mu.Lock()
	defer t.mu.Unlock()
	w, ok := t.seen[source]
	if !ok || now.Sub(w.windowStart) > time.Minute {
		if ok && w.dropped > 0 {
			t.log.Warn("trap rate limit dropped traps", "source", source,
				"dropped", w.dropped, "limit_per_minute", t.perMin)
		}
		t.seen[source] = &rateWindow{windowStart: now, count: 1}
		return true
	}
	if w.count >= t.perMin {
		w.dropped++
		return false
	}
	w.count++
	return true
}

// splitTrap pulls the notification OID out of the varbinds and returns the
// remaining payload.
func splitTrap(p *g.SnmpPacket) (string, map[string]string) {
	out := make(map[string]string, len(p.Variables))
	trapOID := ""
	for _, v := range p.Variables {
		name := strings.TrimPrefix(v.Name, ".")
		switch name {
		case snmpTrapOID:
			trapOID = strings.TrimPrefix(toString(v.Value), ".")
		case trapSysUpTime:
			// sysUpTime is framing, not payload. Note that it is the device's
			// uptime, NOT a wall clock: a trap carries no timestamp, which is
			// why observed_at is stamped on arrival.
		default:
			out[name] = toString(v.Value)
		}
	}
	return trapOID, out
}

func severityFromName(name string) models.Severity {
	switch strings.ToUpper(name) {
	case "CLEAR":
		return models.SeverityClear
	case "INFO":
		return models.SeverityInfo
	case "WARNING":
		return models.SeverityWarning
	case "MINOR":
		return models.SeverityMinor
	case "MAJOR":
		return models.SeverityMajor
	case "CRITICAL":
		return models.SeverityCritical
	default:
		return models.SeverityMinor
	}
}

func describe(def mapping.TrapDef, oid string) string {
	if def.IsClear {
		return fmt.Sprintf("%s cleared (%s)", def.EventType, oid)
	}
	return fmt.Sprintf("%s (%s)", def.EventType, oid)
}

// dedupKey lets the ingest worker discard a redelivered trap without needing a
// database constraint. Second granularity: the same condition reported twice in
// one second is a duplicate, twice in two seconds is two occurrences.
func dedupKey(ev *models.Event) string {
	h := sha1.New()
	fmt.Fprintf(h, "%s|%s|%s|%s|%d", ev.EndpointID, ev.SourceIP, ev.EventType,
		ev.Instance, ev.ObservedAt/1_000_000)
	return hex.EncodeToString(h.Sum(nil))[:32]
}
