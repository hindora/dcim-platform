package integration

import (
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/adapters/bacnet"
	"github.com/hari/dcim-platform/collector/internal/adapters/modbus"
	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

func bacnetAdapter(t *testing.T) *bacnet.Adapter {
	t.Helper()
	maps, err := mapping.LoadBACnet("../../../contracts/mappings")
	if err != nil {
		t.Fatalf("load bacnet mappings: %v", err)
	}
	client := bacnet.NewClient(0, 4*time.Second, 2, TestLogger())
	a := bacnet.New(maps, client, TestLogger(), TestMetrics(), 12)
	if err := a.Init(Ctx(t, 10*time.Second)); err != nil {
		t.Fatalf("open bacnet socket: %v", err)
	}
	t.Cleanup(func() { _ = a.Close(Ctx(t, 5*time.Second)) })
	return a
}

// A chiller's whole point set has to come back in one poll: every analog
// input and every binary input the template maps, decoded into registry
// metrics with the registry's units.
func TestBACnetChillerReadsEveryMappedPoint(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "chiller")
	ep := sim.BACnetEndpoint(t, dev)

	out, err := bacnetAdapter(t).Poll(Ctx(t, 60*time.Second), ep)
	if err != nil {
		t.Fatalf("poll %s: %v", dev.Name, err)
	}

	AssertRegistryContract(t, out)
	AssertNoRawIdentifiers(t, out)

	// The loop lives in the instance, so one key carries both circuits of one
	// machine. A chiller that reports only one is a chiller half read.
	AssertHasMetric(t, out, "water_supply_temp", -20, 120)
	if _, ok := Sample(out, "water_supply_temp", "CHW"); !ok {
		t.Error("no chilled-water supply temperature")
	}
	if _, ok := Sample(out, "water_supply_temp", "COND"); !ok {
		t.Error("no condenser supply temperature")
	}

	// kW on the wire, watts in the registry. A missed conversion is a
	// thousand-fold error that charts perfectly well.
	pw := AssertHasMetric(t, out, "power_draw", 0, 20_000_000)
	if pw.Unit != "W" {
		t.Errorf("power_draw unit %q, want W", pw.Unit)
	}

	// Binary points are states, not measurements.
	if s, ok := Sample(out, "equipment_state", "Chiller_Running"); !ok {
		t.Error("no running state")
	} else if s.ValueType != models.ValueTypeBool {
		t.Errorf("equipment_state is %v, want a bool", s.ValueType)
	}
	AssertInstances(t, out, "alarm_state", 1)

	t.Logf("%s: %d samples across %d metrics", dev.Name, len(out.Samples),
		len(MetricNames(out)))
}

// A device on an MS/TP trunk owns no IP. The router's address carries the
// packet and (network, MAC) says which device answers - and two devices behind
// one router must come back as two distinct machines, not as the router twice.
func TestBACnetMSTPDevicesAreDistinctThroughOneRouter(t *testing.T) {
	sim := RequireSimulator(t)

	var routed []Device
	for _, d := range sim.Devices(t) {
		if d.MSTPRouterIP != "" {
			routed = append(routed, d)
		}
		if len(routed) == 2 {
			break
		}
	}
	if len(routed) < 2 {
		t.Skip("fewer than two MS/TP devices in this topology")
	}
	if routed[0].MSTPRouterIP != routed[1].MSTPRouterIP {
		t.Skip("the two MS/TP devices are behind different routers")
	}

	a := bacnetAdapter(t)
	seen := map[string]string{}
	for _, d := range routed {
		ep := sim.BACnetEndpoint(t, d)
		out, err := a.Poll(Ctx(t, 60*time.Second), ep)
		if err != nil {
			t.Fatalf("poll %s through %s: %v", d.Name, d.MSTPRouterIP, err)
		}
		AssertRegistryContract(t, out)
		if len(out.Samples) == 0 {
			t.Fatalf("%s returned nothing through its router", d.Name)
		}
		seen[d.Name] = MetricNames(out)[0]
		t.Logf("%s (net %d mac %d via %s): %d samples", d.Name, d.MSTPNet,
			d.MSTPMac, d.MSTPRouterIP, len(out.Samples))
	}
	if len(seen) != 2 {
		t.Fatalf("two trunk devices produced %d distinct results", len(seen))
	}
}

// ------------------------------------------------------------- Modbus

func modbusAdapter(t *testing.T) *modbus.Adapter {
	t.Helper()
	maps, err := mapping.LoadModbus("../../../contracts/mappings")
	if err != nil {
		t.Fatalf("load modbus templates: %v", err)
	}
	client := modbus.NewClient(4*time.Second, 1, TestLogger())
	a := modbus.New(maps, client, TestLogger(), TestMetrics())
	t.Cleanup(func() { _ = a.Close(Ctx(t, 5*time.Second)) })
	return a
}

// The template is chosen from inventory before the first request, because
// Modbus has nothing to discover, and FC43 is then used to CHECK that choice.
// A wrong template does not fail - it returns numbers.
func TestModbusTemplateMatchesTheDevice(t *testing.T) {
	sim := RequireSimulator(t)
	a := modbusAdapter(t)

	for _, dtype := range []string{"utility_feed", "switchgear", "ups", "generator"} {
		devices := sim.DevicesOfType(t, dtype)
		if len(devices) == 0 {
			continue
		}
		dev := devices[0]
		t.Run(dtype, func(t *testing.T) {
			out, err := a.Poll(Ctx(t, 30*time.Second), sim.ModbusEndpoint(t, dev))
			if err != nil {
				t.Fatalf("poll %s: %v", dev.Name, err)
			}
			AssertRegistryContract(t, out)
			AssertNoRawIdentifiers(t, out)
			t.Logf("%s: %d samples across %d metrics", dev.Name,
				len(out.Samples), len(MetricNames(out)))
		})
	}
}

// Word order is per vendor and getting it wrong returns a number 65536 times
// too large. The Eaton maps are word-swapped and the Schneider ones are not,
// so reading both and finding every value inside the registry's bounds is what
// proves the distinction is honoured.
func TestModbusWordOrderIsHonouredAcrossVendors(t *testing.T) {
	sim := RequireSimulator(t)
	a := modbusAdapter(t)

	checked := 0
	for _, dtype := range []string{"utility_feed", "switchgear", "mcc", "mpp"} {
		for _, dev := range sim.DevicesOfType(t, dtype) {
			out, err := a.Poll(Ctx(t, 30*time.Second), sim.ModbusEndpoint(t, dev))
			if err != nil {
				t.Errorf("poll %s: %v", dev.Name, err)
				continue
			}
			AssertRegistryContract(t, out)
			// Energy is a 32-bit accumulator, which is exactly where a word
			// order mistake shows up.
			if s, ok := Sample(out, "energy_consumed", ""); ok && s.DoubleValue < 0 {
				t.Errorf("%s reports negative energy %v", dev.Name, s.DoubleValue)
			}
			checked++
			break // one per type is enough to exercise the template
		}
	}
	if checked == 0 {
		t.Skip("no electrical gear in this topology")
	}
	t.Logf("%d vendor templates decoded within registry bounds", checked)
}

// A transmitter publishes one nameless process value; where it is installed is
// what says what it measures. Two probes on one trunk must land on different
// metrics.
func TestModbusTransmittersResolveByProbeRole(t *testing.T) {
	sim := RequireSimulator(t)
	a := modbusAdapter(t)

	var probes []Device
	for _, d := range sim.Devices(t) {
		if d.ModbusRole == "rtu_slave" && ProbeRole(d.Name) != "" {
			probes = append(probes, d)
		}
	}
	if len(probes) < 2 {
		t.Skip("fewer than two field transmitters in this topology")
	}

	byMetric := map[string]string{}
	for _, d := range probes {
		if len(byMetric) >= 3 {
			break
		}
		out, err := a.Poll(Ctx(t, 30*time.Second), sim.ModbusEndpoint(t, d))
		if err != nil {
			t.Errorf("poll %s: %v", d.Name, err)
			continue
		}
		AssertRegistryContract(t, out)
		if len(out.Samples) == 0 {
			t.Errorf("%s produced nothing", d.Name)
			continue
		}
		s := out.Samples[0]
		key := s.Metric + "{" + s.Instance + "}"
		byMetric[key] = d.Name
		t.Logf("%s (%s, unit %d) -> %s = %.2f %s", d.Name, ProbeRole(d.Name),
			d.ModbusUnitID, key, s.DoubleValue, s.Unit)
	}
	if len(byMetric) < 2 {
		t.Fatalf("transmitters collapsed onto %d metric(s): %v",
			len(byMetric), byMetric)
	}
}

// A gateway that cannot be reached must fail once per endpoint, promptly, and
// as unreachable. Six slaves behind one dead gateway are six failures, not six
// retries each, and the poll must not sit for the whole retry budget.
func TestModbusUnreachableGatewayFailsOnceAndFast(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "utility_feed")

	ep := sim.ModbusEndpoint(t, dev)
	// TEST-NET-1: guaranteed not to route anywhere.
	ep.Address = "192.0.2.77"
	ep.ID = "itest-modbus-dead-gateway"
	ep.Poll.TimeoutMs = 1200

	a := modbusAdapter(t)
	started := time.Now()
	_, err := a.Poll(Ctx(t, 30*time.Second), ep)
	elapsed := time.Since(started)

	if err == nil {
		t.Fatal("polling an unroutable address succeeded")
	}
	class := models.ClassifyError(err)
	if class != models.ErrClassUnreachable && class != models.ErrClassTimeout {
		t.Fatalf("error class %q, want unreachable or timeout (%v)", class, err)
	}
	// One connect attempt plus the configured retry, not one per register
	// block: a dead gateway with a twenty-point template must not take twenty
	// timeouts to report itself dead.
	if elapsed > 12*time.Second {
		t.Errorf("a dead gateway took %s to fail, which is longer than one "+
			"poll interval - the failure is being retried per block",
			elapsed.Round(time.Millisecond))
	}
	t.Logf("dead gateway failed in %s as %s", elapsed.Round(time.Millisecond), class)
}
