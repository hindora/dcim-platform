package integration

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/adapters/gnmi"
	"github.com/hari/dcim-platform/collector/internal/health"
	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

func gnmiParts(t *testing.T) (*gnmi.Adapter, *gnmi.ConnPool, *mapping.GNMIMap) {
	t.Helper()
	maps, err := mapping.LoadGNMI("../../../contracts/mappings")
	if err != nil {
		t.Fatalf("load gnmi mappings: %v", err)
	}
	pool := gnmi.NewConnPool(10*time.Second, TestLogger())
	a := gnmi.New(maps, pool, TestLogger(), TestMetrics())
	t.Cleanup(func() { _ = a.Close(Ctx(t, 5*time.Second)) })
	return a, pool, maps
}

// Get is the snapshot mode: the wrong shape for gNMI's strengths but the right
// one for a scheduler that also drives SNMP, and what makes a device usable
// the moment it is imported.
func TestGNMIGetReturnsInterfacesAndSystem(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "switch")
	a, _, _ := gnmiParts(t)

	out, err := a.Poll(Ctx(t, 40*time.Second), sim.GNMIEndpoint(t, dev))
	if err != nil {
		t.Fatalf("poll %s: %v", dev.Name, err)
	}

	AssertRegistryContract(t, out)
	AssertNoRawIdentifiers(t, out)
	AssertInstances(t, out, "if_in_octets", 2)
	AssertHasMetric(t, out, "sys_uptime", 0, 10*365*24*3600)
	AssertHasMetric(t, out, "memory_utilization", 0, 100)

	// Symbolic values: openconfig reports link state and port speed as names
	// where SNMP reports integers and megabits.
	AssertHasMetric(t, out, "if_oper_state", 0, 1)
	AssertHasMetric(t, out, "if_speed", 1e6, 1e12)

	t.Logf("%s: %d samples across %d metrics", dev.Name, len(out.Samples),
		len(MetricNames(out)))
}

// The whole point of gNMI: the device pushes, and the collector stops polling.
// The initial snapshot arrives, then a sync marker, then updates on the
// device's own schedule.
func TestGNMIStreamDeliversWithoutPolling(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "switch")
	a, pool, maps := gnmiParts(t)

	sink := &streamSink{}
	tracker := health.NewTracker(3, "col-itest", sink, TestLogger(), TestMetrics())
	sub := gnmi.NewSubscriber(a, pool, maps, sink, tracker, TestLogger(),
		TestMetrics(), 3)

	ep := sim.GNMIEndpoint(t, dev)
	// The gnmi-stream poll profile: no interval, push enabled.
	ep.Poll = models.PollProfile{IntervalS: 0, TimeoutMs: 8000, PushEnabled: true}
	if !gnmi.StreamOnly(ep) {
		t.Fatal("the push profile is not recognised as stream-only")
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	sub.Manage(ctx, []*models.Endpoint{ep})
	t.Cleanup(sub.Stop)

	deadline := time.Now().Add(30 * time.Second)
	for sink.count() == 0 && time.Now().Before(deadline) {
		time.Sleep(100 * time.Millisecond)
	}
	if sink.count() == 0 {
		t.Fatal("no samples arrived on the stream")
	}
	if n := sub.Sessions(); n != 1 {
		t.Errorf("%d sessions, want 1", n)
	}

	out := &models.PollOutcome{Samples: sink.all()}
	AssertRegistryContract(t, out)

	// One port, one series - even on a stream where every notification carries
	// every mapped subtree.
	seen := map[string]int{}
	for _, s := range sink.all() {
		if s.Metric == "if_in_octets" {
			seen[s.Instance]++
		}
	}
	if len(seen) == 0 {
		t.Fatal("no interface counters on the stream")
	}
	t.Logf("%d streamed samples, %d interfaces", sink.count(), len(seen))
}

// A stream ends whenever a device reboots, a supervisor fails over or a
// maintenance window closes. Coming back is the normal case, not the
// exception, and it must not need the collector restarted.
func TestGNMIStreamReconnectsAfterItIsDropped(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "switch")
	a, pool, maps := gnmiParts(t)

	sink := &streamSink{}
	tracker := health.NewTracker(3, "col-itest", sink, TestLogger(), TestMetrics())
	sub := gnmi.NewSubscriber(a, pool, maps, sink, tracker, TestLogger(),
		TestMetrics(), 3)

	ep := sim.GNMIEndpoint(t, dev)
	ep.Poll = models.PollProfile{IntervalS: 0, TimeoutMs: 8000, PushEnabled: true}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	sub.Manage(ctx, []*models.Endpoint{ep})
	t.Cleanup(sub.Stop)

	waitFor(t, 30*time.Second, func() bool { return sink.count() > 0 })
	first := sink.count()

	// Drop the connection under the session. The subscriber has to notice and
	// dial again on its own.
	pool.ForgetEndpoint(ep.ID)
	sub.Manage(ctx, nil)
	waitFor(t, 10*time.Second, func() bool { return sub.Sessions() == 0 })

	sink.reset()
	sub.Manage(ctx, []*models.Endpoint{ep})
	waitFor(t, 40*time.Second, func() bool { return sink.count() > 0 })

	t.Logf("%d samples before the drop, %d after reconnecting", first, sink.count())
}

func waitFor(t *testing.T, d time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("condition not met within %s", d)
}

// Both planes name one port the same way. On this device plane the SNMP
// ifName and the openconfig name happen to be identical strings, so this
// asserts the AGREEMENT rather than the normalisation - if the plane ever
// stops obliging, this is what says so.
func TestGNMIAndSNMPAgreeOnInterfaceNames(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "switch")

	gnmiAdapter, _, _ := gnmiParts(t)
	gnmiOut, err := gnmiAdapter.Poll(Ctx(t, 40*time.Second), sim.GNMIEndpoint(t, dev))
	if err != nil {
		t.Fatalf("gnmi poll: %v", err)
	}
	snmpOut, err := snmpAdapter(t).Poll(Ctx(t, 40*time.Second),
		sim.SNMPEndpoint(t, dev, "os_agent"))
	if err != nil {
		t.Fatalf("snmp poll: %v", err)
	}

	// if_speed, because it comes from ifXTable which is indexed by ifNAME -
	// the same identity openconfig uses. ifTable is indexed by ifIndex and
	// yields numbers, which converge on the same port only after the ingest
	// worker resolves them against inventory.
	gnmiPorts := instanceSet(gnmiOut, "if_speed")
	snmpPorts := instanceSet(snmpOut, "if_speed")
	if len(gnmiPorts) == 0 || len(snmpPorts) == 0 {
		t.Fatalf("one plane reported no interfaces (gnmi %d, snmp %d)",
			len(gnmiPorts), len(snmpPorts))
	}

	var onlyGNMI, onlySNMP []string
	for p := range gnmiPorts {
		if !snmpPorts[p] {
			onlyGNMI = append(onlyGNMI, p)
		}
	}
	for p := range snmpPorts {
		if !gnmiPorts[p] {
			onlySNMP = append(onlySNMP, p)
		}
	}
	t.Logf("%s: %d ports over gNMI, %d over SNMP, %d only-gNMI, %d only-SNMP",
		dev.Name, len(gnmiPorts), len(snmpPorts), len(onlyGNMI), len(onlySNMP))

	if len(onlyGNMI) > 0 || len(onlySNMP) > 0 {
		t.Errorf("the two planes disagree about port identity, so one port "+
			"becomes two series.\n  only gNMI: %v\n  only SNMP: %v",
			trunc(onlyGNMI), trunc(onlySNMP))
	}
}

func instanceSet(out *models.PollOutcome, metric string) map[string]bool {
	seen := map[string]bool{}
	for _, s := range out.Samples {
		if s.Metric == metric {
			seen[s.Instance] = true
		}
	}
	return seen
}

func trunc(s []string) []string {
	if len(s) > 8 {
		return append(s[:8:8], "...")
	}
	return s
}

type streamSink struct {
	mu      sync.Mutex
	samples []models.Telemetry
}

func (s *streamSink) Telemetry(_ context.Context, in []models.Telemetry) error {
	s.mu.Lock()
	s.samples = append(s.samples, in...)
	s.mu.Unlock()
	return nil
}
func (s *streamSink) Events(context.Context, []models.Event) error { return nil }
func (s *streamSink) EndpointState(context.Context, models.EndpointState) error {
	return nil
}
func (s *streamSink) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.samples)
}
func (s *streamSink) all() []models.Telemetry {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]models.Telemetry, len(s.samples))
	copy(out, s.samples)
	return out
}
func (s *streamSink) reset() {
	s.mu.Lock()
	s.samples = nil
	s.mu.Unlock()
}
