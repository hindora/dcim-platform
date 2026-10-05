package snmp

import (
	"context"
	"crypto/sha1"
	"encoding/hex"
	"fmt"
	"log/slog"
	"net"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
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

// How long a trap waits for the inventory to explain it.
//
// Longer than the assignment interval by a wide margin: the first fetch
// happens at startup and the periodic one every thirty seconds, so a trap that
// cannot be attributed within two minutes is not waiting on a slow fetch - it
// is from something this platform does not know about, which is a different
// finding and deserves to be published as one.
const defaultHoldFor = 2 * time.Minute

// A ceiling, because "hold until we know" is unbounded otherwise: a device
// plane pointed at the wrong collector would fill memory with traps nobody can
// ever attribute. Past this the oldest is published unattributed - degraded,
// not lost.
const defaultHoldMax = 5000

// How often held traps are retried. Frequent enough that a trap held at
// startup is delivered within a second or two of the inventory arriving, which
// is the whole point.
const holdRetryEvery = 2 * time.Second

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

	// SNMPv3 (docs/26 Phase 4): the USM user devices send as, this
	// receiver's own engine ID (for INFORMs), and the signal that rebuilds
	// the listener when the user changes.
	usmMu    sync.Mutex
	usm      *usmParams
	engineID string
	reconfig chan struct{}

	// Traps that arrived before the collector knew who sent them.
	//
	// The socket binds in milliseconds and the first assignment lands twenty
	// seconds later, so every trap in that window resolves to nothing: it is
	// published with no device on it, and an event with no device raises no
	// alarm. Measured, not theorised - a CPU trap fired 22 seconds after a
	// restart was logged as "trap from an unknown source" and the fault never
	// appeared on the console.
	//
	// Held rather than dropped, and retried as soon as the inventory arrives.
	holdMu  sync.Mutex
	held    []*heldTrap
	holdFor time.Duration
	holdMax int

	// Every datagram that reached the socket, for the heartbeat. The
	// heartbeat has carried `traps_received` since the first contract and
	// nothing ever filled it in, so the platform could not tell a quiet
	// estate from a listener nothing reaches.
	received atomic.Uint64
}

// Received is how many traps have reached the socket since start.
func (t *TrapReceiver) Received() uint64 { return t.received.Load() }

// heldTrap is a decoded trap waiting for the inventory that explains it.
//
// Decoded rather than raw: the packet is parsed once, on arrival, so a replay
// cannot decode differently from the first attempt.
type heldTrap struct {
	source    string
	community string
	trapOID   string
	varbinds  map[string]string
	// sysUpTime.0 as the device sent it. Not a clock, but it is the same in
	// every copy of one notification and different in the next one, which
	// is what a dedup key needs - see dedupKey.
	uptime string
	// The moment the datagram ARRIVED, carried through every retry. Stamping
	// a replay with its replay time would place the alarm's first_seen minutes
	// after the condition, and - worse - could make a raise look newer than
	// the clear that actually followed it.
	at time.Time
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
		seen:     make(map[string]*rateWindow),
		holdFor:  defaultHoldFor,
		holdMax:  defaultHoldMax,
		reconfig: make(chan struct{}, 1),
	}
}

// SetEngineID is this receiver's own SNMPv3 engine ID - what a device
// sending an INFORM localises its keys to (the receiver is authoritative for
// an INFORM, the sender for a TRAP). Set before Listen.
func (t *TrapReceiver) SetEngineID(id string) { t.engineID = id }

// SetUSM gives the receiver the USM user its devices send v3 notifications
// as (docs/26 Phase 4): the pool's default SNMPv3 credential. nil means v2c
// only. One user is enough for every device that shares it: gosnmp
// re-localises the keys to each packet's own engine ID. v2c keeps working
// alongside - a network mid-migration sends both. A change rebuilds the
// listener (milliseconds without a socket); an unchanged one is a no-op.
func (t *TrapReceiver) SetUSM(cred *models.Credential) error {
	var next *usmParams
	if cred != nil && cred.Kind == "snmp_v3" {
		p, err := parseUSM(cred)
		if err != nil {
			return err
		}
		next = &p
	}
	t.usmMu.Lock()
	same := (t.usm == nil && next == nil) || (t.usm != nil && next != nil && *t.usm == *next)
	t.usm = next
	t.usmMu.Unlock()
	if same {
		return nil
	}
	select {
	case t.reconfig <- struct{}{}:
	default:
	}
	return nil
}

// params is the listener configuration for the current USM user.
func (t *TrapReceiver) params() *g.GoSNMP {
	t.usmMu.Lock()
	u := t.usm
	t.usmMu.Unlock()
	if u == nil {
		return g.Default
	}
	return &g.GoSNMP{
		Version: g.Version3, SecurityModel: g.UserSecurityModel, MsgFlags: u.msgFlags(),
		Logger: g.Default.Logger, Timeout: g.Default.Timeout,
		SecurityParameters: &g.UsmSecurityParameters{
			UserName:                 u.securityName,
			AuthenticationProtocol:   u.authProtocol,
			AuthenticationPassphrase: u.authKey,
			PrivacyProtocol:          u.privProtocol,
			PrivacyPassphrase:        u.privKey,
			AuthoritativeEngineID:    t.engineID,
		},
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

	onTrap := func(p *g.SnmpPacket, addr *net.UDPAddr) {
		select {
		case queue <- inbound{packet: p, addr: addr, at: time.Now().UTC()}:
		default:
			t.mets.TrapsTotal.WithLabelValues("queue_full").Inc()
		}
	}

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

	// Retries held traps for as long as the receiver runs. A trap that arrives
	// before the first assignment has nothing to resolve against, and this is
	// what delivers it once there is.
	go t.drainHeld(ctx)

	for {
		// The listener about to be built reads the CURRENT user, so a change
		// signalled before this point is already in it. Left queued, it tore
		// the fresh listener straight down again, and a trap arriving in that
		// gap went to a socket that was closing.
		select {
		case <-t.reconfig:
		default:
		}
		listener := g.NewTrapListener()
		listener.Params = t.params()
		listener.OnNewTrap = onTrap
		t.listener = listener
		errCh := make(chan error, 1)
		go func() { errCh <- listener.Listen(t.listen) }()

		// Never Close a listener that has not bound yet. gosnmp's Close finds
		// no socket, marks it finished and returns; the listener then binds
		// anyway and blocks forever handing Close a done signal nobody reads -
		// a bound port that reads nothing, and a Listen that never returns.
		// Found live: a collector re-keyed seconds after its first build
		// stopped receiving every trap, v2c included.
		select {
		case <-listener.Listening():
		case err := <-errCh:
			return fmt.Errorf("trap listener on %s: %w", t.listen, err)
		case <-ctx.Done():
			listener.Close()
			wg.Wait()
			return nil
		}

		t.usmMu.Lock()
		v3 := t.usm != nil
		t.usmMu.Unlock()
		t.log.Info("trap receiver listening", "addr", t.listen,
			"mappings", t.table.Len(), "workers", t.workers, "snmpv3", v3)

		select {
		case <-ctx.Done():
			listener.Close()
			wg.Wait()
			return nil
		case err := <-errCh:
			return fmt.Errorf("trap listener on %s: %w", t.listen, err)
		case <-t.reconfig:
			// The v3 user changed: a fresh listener with the new keys. Wait
			// for the old one to give the socket back before binding again.
			listener.Close()
			if err := <-errCh; err != nil {
				return fmt.Errorf("trap listener on %s: %w", t.listen, err)
			}
		}
	}
}

func (t *TrapReceiver) handle(ctx context.Context, p *g.SnmpPacket,
	addr *net.UDPAddr, at time.Time) {

	t.received.Add(1)
	source := ""
	if addr != nil {
		source = addr.IP.String()
	}
	if !t.allow(source) {
		t.mets.TrapsTotal.WithLabelValues("rate_limited").Inc()
		return
	}

	trapOID, varbinds, uptime := splitTrap(p)
	if trapOID == "" {
		t.mets.TrapsTotal.WithLabelValues("no_trap_oid").Inc()
		t.log.Warn("trap without an snmpTrapOID varbind", "source", source)
		return
	}

	trap := &heldTrap{source: source, community: p.Community,
		trapOID: trapOID, varbinds: varbinds, uptime: uptime, at: at}

	// A trap the collector cannot attribute YET is not the same as one it
	// cannot attribute at all. Before the first assignment lands there is no
	// inventory to resolve against, and publishing now would produce an event
	// with no device on it - which raises no alarm and cannot be recovered
	// later, because nothing keeps the trap.
	if _, ok := t.resolver.Resolve(source, p.Community); !ok && t.hold(trap) {
		return
	}
	t.emit(ctx, trap)
}

// emit resolves a trap against the current inventory and publishes it.
func (t *TrapReceiver) emit(ctx context.Context, trap *heldTrap) {
	source, trapOID, varbinds := trap.source, trap.trapOID, trap.varbinds

	ev := models.Event{
		SourceIP:       source,
		Instance:       "",
		ObservedAt:     trap.at.UnixMicro(),
		CollectedAt:    models.NowMicros(),
		SourceProtocol: models.ProtocolSNMPTrap,
		RawIdentifier:  trapOID,
		Varbinds:       varbinds,
	}

	// The sender's device type disambiguates an OID that carries more than one
	// meaning; it stays empty when the trap cannot be attributed, in which case
	// only the unrestricted meanings can apply.
	senderType := ""
	if ep, ok := t.resolver.Resolve(source, trap.community); ok {
		ev.EndpointID = ep.ID
		ev.DeviceID = ep.DeviceID
		senderType = ep.DeviceType
	} else {
		// Recorded, not dropped: an unattributable trap is evidence that
		// inventory and the network disagree, which is itself worth seeing.
		t.mets.TrapsTotal.WithLabelValues("unresolved_source").Inc()
		t.log.Warn("trap from an unknown source", "source", source, "oid", trapOID)
	}

	def, known := t.table.Lookup(trapOID, senderType, varbinds)
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
		// The measurement the notification arrived with, when it has one. The
		// alarm it raises can then be checked against polled telemetry later,
		// which is the only way a lost recovery trap ever resolves itself.
		if def.Metric != "" {
			value, gotValue := varbindFloat(varbinds, def.ValueVarbind)
			limit, gotLimit := varbindFloat(varbinds, def.ThresholdVarbind)
			// Claim the measurement only if the numbers actually arrived.
			//
			// A metric with no reading behind it is worse than no metric: zero
			// is a plausible temperature and a plausible load, so it reaches
			// the console as a measurement somebody took. One did exactly that
			// - "0 C, limit 0 C" against a switch sitting at 93 - because the
			// mapping named varbinds the sending vendor does not use. The
			// mapping is fixed; this makes the same mistake harmless if it is
			// ever made again.
			if gotValue || gotLimit {
				scale := def.ValueScale
				if scale == 0 {
					scale = 1
				}
				ev.Metric = def.Metric
				ev.Value = value * scale
				ev.Threshold = limit * scale
			}
		}
		if def.InstanceFromVarbind != "" {
			if v, ok := varbindLookup(varbinds, def.InstanceFromVarbind); ok {
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

	ev.DedupKey = dedupKey(&ev, trap.uptime)

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
func splitTrap(p *g.SnmpPacket) (string, map[string]string, string) {
	out := make(map[string]string, len(p.Variables))
	trapOID, uptime := "", ""
	for _, v := range p.Variables {
		name := strings.TrimPrefix(v.Name, ".")
		switch name {
		case snmpTrapOID:
			trapOID = strings.TrimPrefix(toString(v.Value), ".")
		case trapSysUpTime:
			// sysUpTime is framing, not payload. Note that it is the device's
			// uptime, NOT a wall clock: a trap carries no timestamp, which is
			// why observed_at is stamped on arrival. It is kept for the dedup
			// key only.
			uptime = toString(v.Value)
		default:
			out[name] = toString(v.Value)
		}
	}
	return trapOID, out, uptime
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

// describe writes the line a person reads in the alarm list.
//
// It used to be `event_type (oid)` - "cpu_high_usage (1.3.6.1.4.1.99999.1.1)" -
// which repeated the alarm's own type in machine vocabulary and then spent the
// rest of the column on a number nobody reads at 3am. Worse after
// canonicalisation: the alarm said `cpu_saturated` while its message said
// `cpu_sustained`, so the two cells appeared to disagree about what had
// happened.
//
// The vendor's own words when the mapping carries them, the event type when it
// does not. The OID is not lost - it rides on the event as raw_identifier,
// which is where something automated would look for it anyway.
// varbindFloat reads a numeric varbind, tolerating the leading dot some
// agents send and the empty name of a mapping that declares no varbind.
// varbindLookup finds one varbind by OID, exactly or as a table column.
//
// A varbind that names a column of a table arrives with the row's index
// appended: an agent reporting a link on ifIndex 7 sends ifDescr as
// 1.3.6.1.2.1.2.2.1.2.7, not 1.3.6.1.2.1.2.2.1.2. Which index it will be is
// not knowable when the mapping is written - that is the whole content of the
// notification - so a mapping can only name the column, and the receiver has
// to resolve the row. Real managers do this; matching the string exactly
// would work only against an agent that happens to hard-code one index.
//
// An ambiguous column is left unresolved rather than guessed. Two ifDescr
// varbinds in one linkDown would mean the notification is about two ports,
// and picking either would name the wrong cable half the time - which is the
// failure this whole change exists to stop.
func varbindLookup(varbinds map[string]string, oid string) (string, bool) {
	if oid == "" {
		return "", false
	}
	bare := strings.TrimPrefix(oid, ".")
	if raw, ok := varbinds[bare]; ok {
		return raw, true
	}
	if raw, ok := varbinds[oid]; ok {
		return raw, true
	}
	found, prefix := "", bare+"."
	for k, v := range varbinds {
		if strings.HasPrefix(strings.TrimPrefix(k, "."), prefix) {
			if found != "" {
				return "", false
			}
			found = v
		}
	}
	return found, found != ""
}

func varbindFloat(varbinds map[string]string, oid string) (float64, bool) {
	raw, ok := varbindLookup(varbinds, oid)
	if !ok {
		return 0, false
	}
	v, err := strconv.ParseFloat(strings.TrimSpace(raw), 64)
	if err != nil {
		return 0, false
	}
	return v, true
}

func describe(def mapping.TrapDef, oid string) string {
	what := def.DisplayName
	if what == "" {
		what = def.EventType
	}
	if def.IsClear {
		return what + " cleared"
	}
	return what
}

// dedupKey lets the ingest worker discard a redelivered trap without needing a
// database constraint. Second granularity: the same condition reported twice in
// one second is a duplicate, twice in two seconds is two occurrences.
// dedupKey identifies one notification, so the platform keeps one copy.
//
// Copies are normal once there is more than one collector: a device sending
// to two trap receivers - the usual way to make trap delivery survive a
// collector outage - lands the same trap on both, each stamps its own arrival
// time, and a key built on that time split them across a second boundary one
// time in a few. The device's sysUpTime is identical in both copies and moves
// on for the next notification, so it replaces the arrival second whenever
// the trap carries one; v1 traps and senders that omit it keep the old key.
// The source address is left out for the same reason: behind NAT, or from a
// shared sending socket, two copies need not agree on it.
func dedupKey(ev *models.Event, uptime string) string {
	h := sha1.New()
	if uptime != "" && ev.DeviceID != "" {
		fmt.Fprintf(h, "%s|%s|%s|%s|u%s", ev.DeviceID, ev.RawIdentifier,
			ev.EventType, ev.Instance, uptime)
	} else {
		fmt.Fprintf(h, "%s|%s|%s|%s|%d", ev.EndpointID, ev.SourceIP, ev.EventType,
			ev.Instance, ev.ObservedAt/1_000_000)
	}
	return hex.EncodeToString(h.Sum(nil))[:32]
}
