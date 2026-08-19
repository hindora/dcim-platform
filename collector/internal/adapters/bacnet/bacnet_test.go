package bacnet

import (
	"context"
	"encoding/binary"
	"io"
	"log/slog"
	"math"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// fakeDevice is a BACnet controller on a real UDP socket.
//
// It answers with this package's encoder, which is legitimate only because the
// encoder is separately pinned against the simulator's independent decoders in
// codec_test.go. Without that anchor a fake built on our own encoder would
// prove nothing.
type fakeDevice struct {
	conn     *net.UDPConn
	objects  []fakeObject
	instance uint32
	mute     bool // answers everything except Who-Is

	mu       sync.Mutex
	reads    int  // ReadProperty requests served
	rpms     int  // ReadPropertyMultiple requests served
	silent   bool // drop everything, to exercise the timeout path
	routedAs *routing
	done     chan struct{}
}

type routing struct {
	net uint16
	mac byte
}

type fakeObject struct {
	id      ObjectID
	name    string
	value   float64
	binary  bool
	absent  bool // answers unknown-object, like a point that was deleted
	noValue bool // present but with a null present-value: out of service
}

func newFakeDevice(t *testing.T, objects []fakeObject) *fakeDevice {
	t.Helper()
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	d := &fakeDevice{conn: conn, objects: objects, instance: 40007,
		done: make(chan struct{})}
	go d.serve()
	t.Cleanup(func() {
		close(d.done)
		_ = conn.Close()
	})
	return d
}

func (d *fakeDevice) addr() Address {
	ua := d.conn.LocalAddr().(*net.UDPAddr)
	return Address{IP: "127.0.0.1", Port: ua.Port}
}

func (d *fakeDevice) counts() (int, int) {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.reads, d.rpms
}

func (d *fakeDevice) find(id ObjectID) *fakeObject {
	for i := range d.objects {
		if d.objects[i].id == id {
			return &d.objects[i]
		}
	}
	return nil
}

func (d *fakeDevice) serve() {
	buf := make([]byte, 65535)
	for {
		n, from, err := d.conn.ReadFromUDP(buf)
		if err != nil {
			select {
			case <-d.done:
				return
			default:
				continue
			}
		}
		d.mu.Lock()
		silent := d.silent
		d.mu.Unlock()
		if silent {
			continue
		}
		req := make([]byte, n)
		copy(req, buf[:n])
		if reply := d.handle(req); reply != nil {
			_, _ = d.conn.WriteToUDP(reply, from)
		}
	}
}

func (d *fakeDevice) handle(raw []byte) []byte {
	info, err := unframe(raw)
	if err != nil {
		return nil
	}
	a, err := parseAPDU(info.APDU)
	if err != nil {
		return nil
	}
	if a.Kind == kindUnconfirmed && a.Service == svcWhoIs {
		d.mu.Lock()
		mute := d.mute
		d.mu.Unlock()
		if mute {
			return nil
		}
		return d.wrap(d.iAm())
	}
	switch a.Service {
	case svcReadProperty:
		d.mu.Lock()
		d.reads++
		d.mu.Unlock()
		return d.wrap(d.readProperty(a))
	case svcReadPropertyMultiple:
		d.mu.Lock()
		d.rpms++
		d.mu.Unlock()
		return d.wrap(d.readPropertyMultiple(a))
	}
	return nil
}

// wrap adds the source route when the device is configured as an MS/TP child,
// which is how a real router re-emits a reply from behind it.
func (d *fakeDevice) wrap(apdu []byte) []byte {
	if apdu == nil {
		return nil
	}
	npdu := []byte{0x01, 0x00}
	if d.routedAs != nil {
		npdu[1] |= 0x08
		npdu = append(npdu, byte(d.routedAs.net>>8), byte(d.routedAs.net), 1,
			d.routedAs.mac)
	}
	npdu = append(npdu, apdu...)
	length := 4 + len(npdu)
	out := []byte{bvllType, bvlcOriginalUnicast, byte(length >> 8), byte(length)}
	return append(out, npdu...)
}

// iAm is how a device announces the identity BACnet actually uses. The
// instance is assigned by the integrator, so it cannot be derived from the IP.
func (d *fakeDevice) iAm() []byte {
	e := &encoder{}
	e.raw(pduUnconfirmedRequest<<4, svcIAm)
	e.tagged(tagObjectID, false, oidWord(ObjectID{ObjDevice, d.instance}))
	e.appUint(1476)
	e.tagged(tagEnumerated, false, []byte{3}) // no-segmentation
	e.appUint(999)
	return e.bytes()
}

func (d *fakeDevice) readProperty(a apdu) []byte {
	dec := &decoder{buf: a.Payload}
	_, oidBytes, err := dec.nextTag()
	if err != nil || len(oidBytes) != 4 {
		return nil
	}
	obj := unpackObjectID(binary.BigEndian.Uint32(oidBytes))
	_, propBytes, err := dec.nextTag()
	if err != nil {
		return nil
	}
	prop := beUint(propBytes)

	index := -1
	if !dec.eof() {
		if _, idxBytes, err := dec.nextTag(); err == nil {
			index = int(beUint(idxBytes))
		}
	}

	if obj.Type != ObjDevice || prop != PropObjectList {
		return nil
	}

	e := &encoder{}
	e.raw(pduComplexAck<<4, a.InvokeID, svcReadProperty)
	e.ctxObjectID(0, obj)
	e.ctxUint(1, prop)
	if index >= 0 {
		e.ctxUint(2, uint32(index))
	}
	e.open(3)
	switch {
	case index == 0:
		// Element 0 is the count, and it includes the device object itself.
		e.appUint(uint32(len(d.objects) + 1))
	case index == 1:
		e.tagged(tagObjectID, false, oidWord(obj))
	case index > 1 && index <= len(d.objects)+1:
		e.tagged(tagObjectID, false, oidWord(d.objects[index-2].id))
	default:
		return nil
	}
	e.close(3)
	return e.bytes()
}

func oidWord(id ObjectID) []byte {
	var b [4]byte
	binary.BigEndian.PutUint32(b[:], id.packed())
	return b[:]
}

func (d *fakeDevice) readPropertyMultiple(a apdu) []byte {
	dec := &decoder{buf: a.Payload}
	e := &encoder{}
	e.raw(pduComplexAck<<4, a.InvokeID, svcReadPropertyMultiple)

	for !dec.eof() {
		_, oidBytes, err := dec.nextTag()
		if err != nil || len(oidBytes) != 4 {
			break
		}
		obj := unpackObjectID(binary.BigEndian.Uint32(oidBytes))
		if _, _, err := dec.nextTag(); err != nil { // opening [1]
			break
		}
		var props []uint32
		for {
			t, data, err := dec.nextTag()
			if err != nil {
				break
			}
			if t.Closing && t.Num == 1 {
				break
			}
			props = append(props, beUint(data))
		}

		e.ctxObjectID(0, obj)
		e.open(1)
		for _, prop := range props {
			o := d.find(obj)
			e.open(2)
			e.ctxUint(0, prop)
			switch {
			case o == nil || o.absent:
				e.open(5)
				e.tagged(tagEnumerated, false, []byte{1})  // object
				e.tagged(tagEnumerated, false, []byte{31}) // unknown-object
				e.close(5)
			case prop == PropObjectName:
				e.open(4)
				e.tagged(tagCharString, false,
					append([]byte{0}, []byte(o.name)...))
				e.close(4)
			case prop == PropPresentValue && o.noValue:
				e.open(4)
				e.tagged(tagNull, false, nil)
				e.close(4)
			case prop == PropPresentValue && o.binary:
				e.open(4)
				e.tagged(tagEnumerated, false, []byte{byte(int(o.value))})
				e.close(4)
			case prop == PropPresentValue:
				var b [4]byte
				binary.BigEndian.PutUint32(b[:], math.Float32bits(float32(o.value)))
				e.open(4)
				e.tagged(tagReal, false, b[:])
				e.close(4)
			default:
				e.open(5)
				e.tagged(tagEnumerated, false, []byte{2})  // property
				e.tagged(tagEnumerated, false, []byte{32}) // unknown-property
				e.close(5)
			}
			e.close(2)
		}
		e.close(1)
	}
	return e.bytes()
}

// ------------------------------------------------------------- harness

func testLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func loadMap(t *testing.T) *mapping.BACnetMap {
	t.Helper()
	m, err := mapping.LoadBACnet("../../../../contracts/mappings")
	if err != nil {
		t.Fatalf("load bacnet mappings: %v", err)
	}
	return m
}

func newAdapter(t *testing.T, batch int) *Adapter {
	t.Helper()
	c := NewClient(0, 700*time.Millisecond, 1, testLogger())
	a := New(loadMap(t), c, testLogger(), obs.NewMetrics(), batch)
	if err := a.Init(context.Background()); err != nil {
		t.Fatalf("init: %v", err)
	}
	t.Cleanup(func() { _ = a.Close(context.Background()) })
	return a
}

func endpointFor(d *fakeDevice, deviceType string) *models.Endpoint {
	addr := d.addr()
	return &models.Endpoint{
		ID: "ep-1", DeviceID: "dev-1", DeviceType: deviceType, Protocol: "bacnet",
		Address: addr.IP, Port: addr.Port,
		Addressing: map[string]any{"device_instance": 40001},
		Poll:       models.PollProfile{IntervalS: 30, TimeoutMs: 2000},
	}
}

// chillerObjects mirrors the simulator's chiller point names.
func chillerObjects() []fakeObject {
	return []fakeObject{
		{id: ObjectID{ObjAnalogInput, 1}, name: "CHW_Supply_Temp", value: 7.2},
		{id: ObjectID{ObjAnalogInput, 2}, name: "CHW_Return_Temp", value: 12.4},
		{id: ObjectID{ObjAnalogInput, 5}, name: "Cond_Supply_Temp", value: 30.5},
		{id: ObjectID{ObjAnalogInput, 7}, name: "Compressor_Load", value: 68.0},
		{id: ObjectID{ObjAnalogInput, 8}, name: "Active_Power", value: 412.5},
		{id: ObjectID{ObjAnalogInput, 11}, name: "Evap_Pressure", value: 351.0},
		{id: ObjectID{ObjAnalogInput, 12}, name: "Cond_Pressure", value: 902.0},
		// Not in the mapping: discovered, counted, never polled.
		{id: ObjectID{ObjAnalogInput, 99}, name: "Vendor_Internal_Diag", value: 1},
		{id: ObjectID{ObjBinaryInput, 1}, name: "Chiller_Running", value: 1, binary: true},
		{id: ObjectID{ObjBinaryInput, 2}, name: "Alarm_HighPressure", value: 0, binary: true},
	}
}

func collect(out *models.PollOutcome) map[string]models.Telemetry {
	m := map[string]models.Telemetry{}
	for _, s := range out.Samples {
		key := s.Metric
		if s.Instance != "" {
			key += "{" + s.Instance + "}"
		}
		m[key] = s
	}
	return m
}

// --------------------------------------------------------------- tests

func TestDiscoveryMapsPointsByName(t *testing.T) {
	d := newFakeDevice(t, chillerObjects())
	a := newAdapter(t, 8)

	out, err := a.Poll(context.Background(), endpointFor(d, "chiller"))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	got := collect(out)

	// One key, two loops, distinguished by instance - which is the whole
	// reason the loop lives in `instance` rather than in the metric name.
	if s, ok := got["water_supply_temp{CHW}"]; !ok || math.Abs(s.DoubleValue-7.2) > 0.01 {
		t.Errorf("CHW supply: %v (present=%v)", s.DoubleValue, ok)
	}
	if s, ok := got["water_supply_temp{COND}"]; !ok || math.Abs(s.DoubleValue-30.5) > 0.01 {
		t.Errorf("COND supply: %v (present=%v)", s.DoubleValue, ok)
	}
	if s, ok := got["water_pressure{EVAP}"]; !ok || math.Abs(s.DoubleValue-351) > 0.01 {
		t.Errorf("evap pressure: %v (present=%v)", s.DoubleValue, ok)
	}
	if _, ok := got["water_pressure{COND}"]; !ok {
		t.Error("condenser pressure missing")
	}
}

// kW on the wire, watts in the registry. A key carries one unit forever, so
// the adapter converts - and a missed conversion is a 1000x error that looks
// entirely plausible on a chart.
func TestUnitConversionAtTheAdapter(t *testing.T) {
	d := newFakeDevice(t, chillerObjects())
	a := newAdapter(t, 8)

	out, err := a.Poll(context.Background(), endpointFor(d, "chiller"))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	s, ok := collect(out)["power_draw"]
	if !ok {
		t.Fatal("power_draw missing")
	}
	if math.Abs(s.DoubleValue-412500) > 1 {
		t.Fatalf("power_draw %v W, want 412500 (412.5 kW)", s.DoubleValue)
	}
	if s.Unit != "W" {
		t.Fatalf("unit %q, want W", s.Unit)
	}
}

// A binary point's present-value is an ENUMERATED state, not a measurement.
func TestBinaryPointsDecodeAsState(t *testing.T) {
	d := newFakeDevice(t, chillerObjects())
	a := newAdapter(t, 8)

	out, err := a.Poll(context.Background(), endpointFor(d, "chiller"))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	got := collect(out)
	run, ok := got["equipment_state{Chiller_Running}"]
	if !ok {
		t.Fatal("running state missing")
	}
	if run.ValueType != models.ValueTypeBool || run.DoubleValue != 1 {
		t.Errorf("running: type %v value %v", run.ValueType, run.DoubleValue)
	}
	// Every alarm lands on one key with its own name as the instance.
	alarm, ok := got["alarm_state{Alarm_HighPressure}"]
	if !ok {
		t.Fatal("alarm point missing")
	}
	if alarm.DoubleValue != 0 {
		t.Errorf("alarm value %v, want 0", alarm.DoubleValue)
	}
}

// Only mapped points are polled. An unmapped object is counted and left alone:
// reading all 233 objects on a panel every cycle is how a BACnet integration
// takes a controller offline.
func TestUnmappedObjectsAreNotPolled(t *testing.T) {
	d := newFakeDevice(t, chillerObjects())
	a := newAdapter(t, 8)
	ep := endpointFor(d, "chiller")

	if _, err := a.Poll(context.Background(), ep); err != nil {
		t.Fatalf("poll: %v", err)
	}
	a.mu.Lock()
	profile := a.discovery[ep.ID]
	a.mu.Unlock()

	if profile.unmapped != 1 {
		t.Errorf("unmapped %d, want 1", profile.unmapped)
	}
	for _, p := range profile.points {
		if strings.HasPrefix(p.name, "Vendor_") {
			t.Fatalf("unmapped point %q was selected for polling", p.name)
		}
	}
}

// Discovery is the expensive half and must happen once. Re-reading the object
// list and every object name each cycle triples the cost of a poll.
func TestDiscoveryHappensOnce(t *testing.T) {
	d := newFakeDevice(t, chillerObjects())
	a := newAdapter(t, 8)
	ep := endpointFor(d, "chiller")

	if _, err := a.Poll(context.Background(), ep); err != nil {
		t.Fatalf("first poll: %v", err)
	}
	readsAfterFirst, rpmsAfterFirst := d.counts()
	if readsAfterFirst == 0 {
		t.Fatal("object list was never read")
	}

	for i := 0; i < 3; i++ {
		if _, err := a.Poll(context.Background(), ep); err != nil {
			t.Fatalf("poll %d: %v", i, err)
		}
	}
	reads, rpms := d.counts()
	if reads != readsAfterFirst {
		t.Errorf("object list re-read %d times after discovery",
			reads-readsAfterFirst)
	}
	if rpms <= rpmsAfterFirst {
		t.Error("later polls issued no reads at all")
	}

	// Forget forces rediscovery, which is what makes a reprogrammed
	// controller recoverable without restarting the collector.
	a.Forget(ep.ID)
	if _, err := a.Poll(context.Background(), ep); err != nil {
		t.Fatalf("poll after forget: %v", err)
	}
	if again, _ := d.counts(); again <= reads {
		t.Error("Forget did not trigger rediscovery")
	}
}

// A point deleted AFTER discovery must not cost the caller the rest of the
// panel: an integrator removing one alarm from a controller should show up as
// one gap, not as a chiller that went dark.
//
// The point is removed between polls on purpose. An object already absent at
// discovery is simply never selected, which is a different - and already
// correct - path.
func TestPointDeletedAfterDiscoveryIsAMissNotAFailure(t *testing.T) {
	d := newFakeDevice(t, chillerObjects())
	a := newAdapter(t, 8)
	ep := endpointFor(d, "chiller")

	if _, err := a.Poll(context.Background(), ep); err != nil {
		t.Fatalf("discovery poll: %v", err)
	}
	d.mu.Lock()
	for i := range d.objects {
		if d.objects[i].name == "Cond_Pressure" {
			d.objects[i].absent = true
		}
	}
	d.mu.Unlock()

	out, err := a.Poll(context.Background(), ep)
	if err != nil {
		t.Fatalf("poll failed for one missing point: %v", err)
	}
	if !out.Partial {
		t.Error("outcome not marked partial")
	}
	if len(out.Misses) == 0 {
		t.Error("no miss recorded")
	}
	if _, ok := collect(out)["water_supply_temp{CHW}"]; !ok {
		t.Error("a working point was lost with the missing one")
	}
}

// A null present-value means out of service. Emitting zero would read as a
// real measurement - a chiller at 0 kW that is actually running.
func TestNullPresentValueIsAMissNotZero(t *testing.T) {
	objects := chillerObjects()
	for i := range objects {
		if objects[i].name == "Active_Power" {
			objects[i].noValue = true
		}
	}
	d := newFakeDevice(t, objects)
	a := newAdapter(t, 8)

	out, err := a.Poll(context.Background(), endpointFor(d, "chiller"))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	if s, ok := collect(out)["power_draw"]; ok {
		t.Fatalf("out-of-service point emitted %v", s.DoubleValue)
	}
	found := false
	for _, m := range out.Misses {
		if m.Metric == "power_draw" {
			found = true
		}
	}
	if !found {
		t.Error("no miss recorded for the out-of-service point")
	}
}

// A silent device is a TIMEOUT, not a decode failure. The health tracker
// branches on that: one means the controller is unreachable, the other means
// it answered with something we could not read.
func TestSilentDeviceReportsTimeout(t *testing.T) {
	d := newFakeDevice(t, chillerObjects())
	a := newAdapter(t, 8)
	ep := endpointFor(d, "chiller")

	if _, err := a.Poll(context.Background(), ep); err != nil {
		t.Fatalf("first poll: %v", err)
	}
	d.mu.Lock()
	d.silent = true
	d.mu.Unlock()

	_, err := a.Poll(context.Background(), ep)
	if err == nil {
		t.Fatal("a silent device produced no error")
	}
	if class := models.ClassifyError(err); class != models.ErrClassTimeout {
		t.Fatalf("error class %q, want timeout", class)
	}
}

// An unknown device instance is DISCOVERED, not configured.
//
// The instance is BACnet identity and it is not derivable from the IP: the
// integrator assigns it in commissioning order, and a device on an MS/TP trunk
// has no IP at all. A directed Who-Is is what a BMS tool does against a known
// address, and it avoids the broadcast storm of a global one.
func TestUnknownDeviceInstanceIsDiscovered(t *testing.T) {
	d := newFakeDevice(t, chillerObjects())
	d.instance = 40023
	a := newAdapter(t, 8)
	ep := endpointFor(d, "chiller")
	ep.Addressing = map[string]any{} // nothing known but the address

	out, err := a.Poll(context.Background(), ep)
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	if _, ok := collect(out)["water_supply_temp{CHW}"]; !ok {
		t.Fatal("no telemetry after identifying the device")
	}
	a.mu.Lock()
	got := a.discovery[ep.ID].deviceObj.Instance
	a.mu.Unlock()
	if got != 40023 {
		t.Fatalf("identified instance %d, want 40023", got)
	}
}

// A controller that will not answer Who-Is is UNREACHABLE, not misconfigured.
// Classifying it as a protocol fault would send someone to debug a decoder for
// a device that is simply off.
func TestDeviceThatWillNotIdentifyIsATimeout(t *testing.T) {
	d := newFakeDevice(t, chillerObjects())
	d.mute = true
	a := newAdapter(t, 8)
	ep := endpointFor(d, "chiller")
	ep.Addressing = map[string]any{}

	_, err := a.Poll(context.Background(), ep)
	if err == nil {
		t.Fatal("polled a device that never identified itself")
	}
	if class := models.ClassifyError(err); class != models.ErrClassTimeout {
		t.Fatalf("error class %q, want timeout", class)
	}
}

// The importer writes device_instance; older rows carry instance. Both are
// accepted, because an endpoint's addressing is a data contract and renaming a
// key silently would strand every row written before the change.
func TestLegacyInstanceKeyIsAccepted(t *testing.T) {
	d := newFakeDevice(t, chillerObjects())
	a := newAdapter(t, 8)
	ep := endpointFor(d, "chiller")
	ep.Addressing = map[string]any{"instance": 40001}

	if _, err := a.Poll(context.Background(), ep); err != nil {
		t.Fatalf("poll: %v", err)
	}
}

// A device silent from the very first poll must read as a timeout too. Before
// this was classified at the boundary, the discovery path reported a generic
// protocol error and a dead controller looked like a broken decoder.
func TestSilentBeforeDiscoveryIsATimeout(t *testing.T) {
	d := newFakeDevice(t, chillerObjects())
	d.mu.Lock()
	d.silent = true
	d.mu.Unlock()
	a := newAdapter(t, 8)

	_, err := a.Poll(context.Background(), endpointFor(d, "chiller"))
	if err == nil {
		t.Fatal("polled a silent device without error")
	}
	if class := models.ClassifyError(err); class != models.ErrClassTimeout {
		t.Fatalf("error class %q, want timeout", class)
	}
}

func TestUnmappedDeviceTypeIsConfig(t *testing.T) {
	d := newFakeDevice(t, chillerObjects())
	a := newAdapter(t, 8)

	_, err := a.Poll(context.Background(), endpointFor(d, "something_else"))
	if err == nil {
		t.Fatal("polled a device type with no mapping")
	}
	if class := models.ClassifyError(err); class != models.ErrClassConfig {
		t.Fatalf("error class %q, want config", class)
	}
}

// A device on an MS/TP trunk answers through its router, so the reply carries
// SNET/SADR instead of arriving from an IP of its own. Without matching on
// those, every device on a trunk looks like the router.
func TestRoutedDeviceIsPolled(t *testing.T) {
	d := newFakeDevice(t, chillerObjects())
	d.routedAs = &routing{net: 2001, mac: 12}
	a := newAdapter(t, 8)

	ep := endpointFor(d, "chiller")
	ep.Addressing["network"] = 2001
	ep.Addressing["mac"] = 12

	out, err := a.Poll(context.Background(), ep)
	if err != nil {
		t.Fatalf("routed poll: %v", err)
	}
	if _, ok := collect(out)["water_supply_temp{CHW}"]; !ok {
		t.Fatal("no telemetry from the routed device")
	}
}

// A reply from the wrong device on the trunk must not be accepted for the
// device we asked. The value would be real, plausible, and attributed to the
// wrong machine.
func TestReplyFromAnotherTrunkDeviceIsRejected(t *testing.T) {
	d := newFakeDevice(t, chillerObjects())
	d.routedAs = &routing{net: 2001, mac: 99} // a different MS/TP MAC
	a := newAdapter(t, 8)

	ep := endpointFor(d, "chiller")
	ep.Addressing["network"] = 2001
	ep.Addressing["mac"] = 12

	if _, err := a.Poll(context.Background(), ep); err == nil {
		t.Fatal("accepted a reply from a different device on the trunk")
	}
}

// Batching is what makes a large panel pollable. Twenty points in batches of
// five is four exchanges, not twenty.
func TestBatchingReducesExchanges(t *testing.T) {
	d := newFakeDevice(t, chillerObjects())
	a := newAdapter(t, 4)
	ep := endpointFor(d, "chiller")

	if _, err := a.Poll(context.Background(), ep); err != nil {
		t.Fatalf("discovery poll: %v", err)
	}
	_, before := d.counts()
	if _, err := a.Poll(context.Background(), ep); err != nil {
		t.Fatalf("poll: %v", err)
	}
	_, after := d.counts()

	a.mu.Lock()
	points := len(a.discovery[ep.ID].points)
	a.mu.Unlock()

	exchanges := after - before
	want := (points + 3) / 4
	if exchanges != want {
		t.Fatalf("%d exchanges for %d points in batches of 4, want %d",
			exchanges, points, want)
	}
}

func TestInvokeIDPoolIsBounded(t *testing.T) {
	c := NewClient(0, time.Second, 0, testLogger())
	seen := map[byte]bool{}
	for i := 0; i < 256; i++ {
		id, err := c.takeInvokeID()
		if err != nil {
			t.Fatalf("exhausted after %d ids", i)
		}
		if seen[id] {
			t.Fatalf("invoke id %d handed out twice while still in flight", id)
		}
		seen[id] = true
	}
	if _, err := c.takeInvokeID(); err == nil {
		t.Fatal("handed out a 257th invoke id")
	}
	if c.InFlight() != 256 {
		t.Fatalf("in flight %d, want 256", c.InFlight())
	}
}
