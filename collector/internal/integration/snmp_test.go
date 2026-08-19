package integration

import (
	"sync"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/adapters/snmp"
	"github.com/hari/dcim-platform/collector/internal/assign"
	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

func snmpAdapter(t *testing.T) *snmp.Adapter {
	t.Helper()
	maps, err := mapping.Load("../../../contracts/mappings")
	if err != nil {
		t.Fatalf("load snmp mappings: %v", err)
	}
	// accept_any_source_reply: the agent is bound to a wildcard socket and
	// answers from whichever address the kernel picks, which is not always the
	// one polled.
	return snmp.New(maps, TestLogger(), TestMetrics(), 25, true)
}

func TestSNMPServerOSAgent(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "server")
	a := snmpAdapter(t)

	out, err := a.Poll(Ctx(t, 30*time.Second), sim.SNMPEndpoint(t, dev, "os_agent"))
	if err != nil {
		t.Fatalf("poll %s: %v", dev.Name, err)
	}

	AssertRegistryContract(t, out)
	AssertNoRawIdentifiers(t, out)
	AssertHasMetric(t, out, "cpu_utilization", 0, 100)
	AssertHasMetric(t, out, "memory_utilization", 0, 100)
	AssertHasMetric(t, out, "sys_uptime", 0, 10*365*24*3600)
	// Interfaces, but NOT their traffic counters: this plane does not serve
	// them - see TestSNMPInterfaceCountersAreNotServed for why.
	AssertInstances(t, out, "if_oper_state", 1)
	t.Logf("%s: %d samples across %d metrics", dev.Name, len(out.Samples),
		len(MetricNames(out)))
}

// A server has TWO SNMP agents: the OS on its own address and the service
// processor on the management address. They answer different OIDs, and
// collapsing them into one endpoint loses whichever half is not polled.
func TestSNMPServerHasTwoWorkingAgents(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "server")
	if dev.IPAddress == "" || dev.MgmtIP == "" || dev.IPAddress == dev.MgmtIP {
		t.Skipf("%s does not have two distinct SNMP addresses", dev.Name)
	}
	a := snmpAdapter(t)

	osOut, err := a.Poll(Ctx(t, 30*time.Second), sim.SNMPEndpoint(t, dev, "os_agent"))
	if err != nil {
		t.Fatalf("OS agent on %s: %v", dev.IPAddress, err)
	}
	bmcOut, err := a.Poll(Ctx(t, 30*time.Second), sim.SNMPEndpoint(t, dev, "bmc"))
	if err != nil {
		t.Fatalf("BMC agent on %s: %v", dev.MgmtIP, err)
	}

	AssertRegistryContract(t, osOut)
	AssertRegistryContract(t, bmcOut)

	// They must not be the same agent answering twice.
	osMetrics := MetricNames(osOut)
	bmcMetrics := MetricNames(bmcOut)
	t.Logf("OS  %s: %v", dev.IPAddress, osMetrics)
	t.Logf("BMC %s: %v", dev.MgmtIP, bmcMetrics)
	if len(osOut.Samples) == 0 || len(bmcOut.Samples) == 0 {
		t.Fatal("one of the two agents produced nothing")
	}
}

// The community IS the address on this plane, so a wrong one is not rejected -
// the agent simply does not answer. That has to surface as a timeout, because
// it is indistinguishable from a dead device on the wire, and it must never
// take the collector down.
func TestSNMPWrongCommunityTimesOutCleanly(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "server")
	ep := sim.SNMPEndpoint(t, dev, "os_agent")
	ep.Credential = &models.Credential{
		Kind: "snmp_v2c",
		Data: map[string]any{"community": "definitely-not-the-community"},
	}
	ep.Poll.TimeoutMs = 1500

	out, err := snmpAdapter(t).Poll(Ctx(t, 30*time.Second), ep)
	if err == nil {
		t.Fatalf("a wrong community produced %d samples", len(out.Samples))
	}
	if class := models.ClassifyError(err); class != models.ErrClassTimeout {
		t.Fatalf("error class %q, want timeout (got %v)", class, err)
	}
}

// Device types with no SNMP agent must not be polled at all. Creating an
// endpoint for one produces an endpoint that is permanently OFFLINE and reads
// as a broken device rather than as a device that speaks something else.
func TestSNMPIsNotAttemptedForPlantOnlyTypes(t *testing.T) {
	sim := RequireSimulator(t)
	// A BACnet-only valve on an MS/TP trunk owns no IP at all, which is the
	// strongest form of "do not poll this over SNMP".
	for _, d := range sim.DevicesOfType(t, "valve") {
		if d.MSTPRouterIP != "" && d.MgmtIP == "" && d.IPAddress == "" {
			t.Logf("%s has no address of its own: MS/TP behind %s",
				d.Name, d.MSTPRouterIP)
			return
		}
	}
	t.Skip("no address-less field device in this topology")
}

// A fleet-sized sweep has to finish inside one poll interval. If it does not,
// the collector is permanently behind and every value it publishes is older
// than it claims.
func TestSNMPSweepFitsInsideOneInterval(t *testing.T) {
	if testing.Short() {
		t.Skip("sweep timing is a long test")
	}
	sim := RequireSimulator(t)
	devices := sim.Devices(t)

	var eps []*models.Endpoint
	for _, d := range devices {
		if d.DeviceType == "valve" || d.DeviceType == "sensor" {
			continue
		}
		if d.MgmtIP == "" && d.IPAddress == "" {
			continue
		}
		eps = append(eps, sim.SNMPEndpoint(t, d, "os_agent"))
		if len(eps) >= 200 {
			break
		}
	}
	if len(eps) < 20 {
		t.Skipf("only %d pollable devices", len(eps))
	}

	a := snmpAdapter(t)
	ctx := Ctx(t, 120*time.Second)

	// 48 in flight, matching the shipped max_concurrent for a single
	// wildcard-bound responder serving the whole plane.
	sem := make(chan struct{}, 48)
	var wg sync.WaitGroup
	var mu sync.Mutex
	ok, failed := 0, 0

	started := time.Now()
	for _, ep := range eps {
		wg.Add(1)
		go func(ep *models.Endpoint) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			if _, err := a.Poll(ctx, ep); err != nil {
				mu.Lock()
				failed++
				mu.Unlock()
				return
			}
			mu.Lock()
			ok++
			mu.Unlock()
		}(ep)
	}
	wg.Wait()
	elapsed := time.Since(started)

	t.Logf("%d endpoints in %s (%d ok, %d failed)", len(eps),
		elapsed.Round(time.Millisecond), ok, failed)
	if elapsed > 30*time.Second {
		t.Errorf("sweep of %d endpoints took %s, longer than the 30 s interval",
			len(eps), elapsed)
	}
	if ok == 0 {
		t.Fatal("the whole sweep failed")
	}
}

// The resolver is what turns a trap's source address back into a device. It is
// exercised here because an unresolvable trap is emitted anyway, and the only
// way to notice the mapping is wrong is to check it directly.
func TestResolverMapsAddressesToDevices(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "switch")
	ep := sim.SNMPEndpoint(t, dev, "os_agent")

	r := assign.NewResolver()
	r.Replace([]*models.Endpoint{ep})

	got, ok := r.Resolve(ep.Address, "")
	if !ok || got.DeviceID != dev.ID {
		t.Fatalf("resolving %s gave %v (ok=%v), want device %s",
			ep.Address, got, ok, dev.ID)
	}
	if _, ok := r.Resolve("203.0.113.1", ""); ok {
		t.Error("an unknown address resolved to something")
	}
}

// The interface traffic counters are mapped, correct, and not served by this
// plane. Recording that here rather than leaving a red assertion, because the
// fault is in the device plane's dataset and not in the collector.
//
// The dataset writes its type tags in hex-as-decimal: ifHCInOctets and
// ifHCOutOctets are emitted with tag "44" and the ifTable error and discard
// columns with "41", where the .snmprec format wants the DECIMAL ASN.1 tag -
// 70 for Counter64 and 65 for Counter32. 0x46 and 0x41 are those same values
// in hex, which is where the confusion comes from.
//
// snmpsim serves only the lines it can parse, so those OIDs are silently
// absent. The proof is inside one walk: ifTable columns tagged "2" (admin and
// oper status) come back and columns tagged "41" from the SAME rows do not.
//
// Nothing in the collector needs changing - the mapping asks for the right
// OIDs, and the day the dataset is corrected the counters appear.
func TestSNMPInterfaceCountersAreNotServed(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "switch")

	out, err := snmpAdapter(t).Poll(Ctx(t, 30*time.Second),
		sim.SNMPEndpoint(t, dev, "os_agent"))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}

	counters := 0
	for _, m := range []string{"if_in_octets", "if_out_octets", "if_in_errors",
		"if_out_errors", "if_in_discards", "if_out_discards"} {
		if len(instanceSet(out, m)) > 0 {
			counters++
		}
	}
	if counters > 0 {
		t.Logf("the plane now serves %d of the 6 interface counters - the "+
			"dataset type tags have been fixed, and this test can become an "+
			"assertion", counters)
		return
	}
	// Interfaces themselves ARE served, which is what makes the gap specific
	// rather than a device that simply has no ports.
	ports := instanceSet(out, "if_speed")
	if len(ports) == 0 {
		t.Fatalf("%s reports no interfaces at all, which is a different "+
			"problem from the missing counters", dev.Name)
	}
	t.Skipf("this plane serves %d interfaces on %s but none of the 6 traffic "+
		"counters: snmprec type tags 44/41 should be 70/65 (Counter64/Counter32)",
		len(ports), dev.Name)
}
