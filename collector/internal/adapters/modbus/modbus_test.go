package modbus

import (
	"context"
	"encoding/binary"
	"errors"
	"io"
	"log/slog"
	"math"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// fakeDevice is a Modbus/TCP server that behaves like real gear in the ways
// that matter: a sparse map that refuses any read crossing a hole, a validity
// bit, optional FC43, and the two gateway exceptions.
type fakeDevice struct {
	ln net.Listener

	mu        sync.Mutex
	input     map[uint16]uint16
	holding   map[uint16]uint16
	discrete  map[uint16]bool
	identity  *DeviceIdentity // nil = FC43 not implemented
	failUnits map[byte]byte   // unit id -> exception code
	reads     int
	maxSpan   int // largest register count seen in one request
}

func newFakeDevice(t *testing.T) *fakeDevice {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	d := &fakeDevice{
		ln:       ln,
		input:    map[uint16]uint16{},
		holding:  map[uint16]uint16{},
		discrete: map[uint16]bool{},
	}
	go d.serve()
	t.Cleanup(func() { _ = ln.Close() })
	return d
}

func (d *fakeDevice) addr() (string, int) {
	a := d.ln.Addr().(*net.TCPAddr)
	return "127.0.0.1", a.Port
}

func (d *fakeDevice) serve() {
	for {
		c, err := d.ln.Accept()
		if err != nil {
			return
		}
		go d.handle(c)
	}
}

func (d *fakeDevice) handle(c net.Conn) {
	defer c.Close()
	header := make([]byte, mbapLen)
	for {
		if _, err := io.ReadFull(c, header); err != nil {
			return
		}
		total, ok := aduLength(header)
		if !ok {
			return
		}
		buf := make([]byte, total)
		copy(buf, header)
		if _, err := io.ReadFull(c, buf[mbapLen:]); err != nil {
			return
		}
		txn, unit, pdu, err := decodeADU(buf)
		if err != nil {
			return
		}
		resp := d.respond(unit, pdu)
		if resp == nil {
			return
		}
		if _, err := c.Write(encodeADU(txn, unit, resp)); err != nil {
			return
		}
	}
}

func (d *fakeDevice) respond(unit byte, pdu []byte) []byte {
	d.mu.Lock()
	if code, bad := d.failUnits[unit]; bad {
		d.mu.Unlock()
		return []byte{pdu[0] | 0x80, code}
	}
	d.mu.Unlock()

	switch pdu[0] {
	case fcReadInput, fcReadHolding:
		return d.readRegs(pdu)
	case fcReadDiscreteInputs:
		return d.readBits(pdu)
	case fcReadDeviceID:
		return d.readIdentity(pdu)
	default:
		return []byte{pdu[0] | 0x80, exIllegalFunction}
	}
}

func (d *fakeDevice) readRegs(pdu []byte) []byte {
	addr := binary.BigEndian.Uint16(pdu[1:])
	count := binary.BigEndian.Uint16(pdu[3:])
	if count < 1 || count > maxReadRegisters {
		return []byte{pdu[0] | 0x80, exIllegalValue}
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	d.reads++
	if int(count) > d.maxSpan {
		d.maxSpan = int(count)
	}
	bank := d.input
	if pdu[0] == fcReadHolding {
		bank = d.holding
	}
	out := []byte{pdu[0], byte(count * 2)}
	for a := addr; a < addr+count; a++ {
		v, ok := bank[a]
		if !ok {
			// A real sparse map refuses the WHOLE request rather than padding
			// the hole with a zero.
			return []byte{pdu[0] | 0x80, exIllegalAddress}
		}
		out = append(out, byte(v>>8), byte(v))
	}
	return out
}

func (d *fakeDevice) readBits(pdu []byte) []byte {
	addr := binary.BigEndian.Uint16(pdu[1:])
	count := binary.BigEndian.Uint16(pdu[3:])
	d.mu.Lock()
	defer d.mu.Unlock()
	d.reads++
	nbytes := int((count + 7) / 8)
	out := make([]byte, 2+nbytes)
	out[0], out[1] = pdu[0], byte(nbytes)
	for i := 0; i < int(count); i++ {
		v, ok := d.discrete[addr+uint16(i)]
		if !ok {
			return []byte{pdu[0] | 0x80, exIllegalAddress}
		}
		if v {
			out[2+i/8] |= 1 << uint(i%8)
		}
	}
	return out
}

func (d *fakeDevice) readIdentity(pdu []byte) []byte {
	d.mu.Lock()
	id := d.identity
	d.mu.Unlock()
	if id == nil {
		return []byte{pdu[0] | 0x80, exIllegalFunction}
	}
	objs := [][2]string{{"\x00", id.Vendor}, {"\x01", id.Product}, {"\x02", id.Revision}}
	body := []byte{fcReadDeviceID, meiReadDeviceID, 0x01, 0x01, 0x00, 0x00, byte(len(objs))}
	for _, o := range objs {
		body = append(body, o[0][0], byte(len(o[1])))
		body = append(body, []byte(o[1])...)
	}
	return body
}

func (d *fakeDevice) setReg(addr, v uint16)  { d.mu.Lock(); d.input[addr] = v; d.mu.Unlock() }
func (d *fakeDevice) setHold(addr, v uint16) { d.mu.Lock(); d.holding[addr] = v; d.mu.Unlock() }
func (d *fakeDevice) setBit(addr uint16, v bool) {
	d.mu.Lock()
	d.discrete[addr] = v
	d.mu.Unlock()
}

func (d *fakeDevice) setReg32(addr uint16, raw uint32, order string) {
	hi, lo := uint16(raw>>16), uint16(raw&0xFFFF)
	if order == WordSwap {
		hi, lo = lo, hi
	}
	d.setReg(addr, hi)
	d.setReg(addr+1, lo)
}

func (d *fakeDevice) counts() (int, int) {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.reads, d.maxSpan
}

// ------------------------------------------------------------ harness

func testLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func loadMaps(t *testing.T) *mapping.ModbusMap {
	t.Helper()
	m, err := mapping.LoadModbus("../../../../contracts/mappings")
	if err != nil {
		t.Fatalf("load modbus templates: %v", err)
	}
	return m
}

func newAdapter(t *testing.T) *Adapter {
	t.Helper()
	c := NewClient(900*time.Millisecond, 1, testLogger())
	a := New(loadMaps(t), c, testLogger(), obs.NewMetrics())
	t.Cleanup(func() { _ = a.Close(context.Background()) })
	return a
}

func endpointFor(d *fakeDevice, deviceType string) *models.Endpoint {
	host, port := d.addr()
	return &models.Endpoint{
		ID: "ep-1", DeviceID: "dev-1", DeviceType: deviceType, Protocol: "modbus",
		Address: host, Port: port, Role: "native_card",
		Addressing: map[string]any{"unit_id": 1},
		Poll:       models.PollProfile{IntervalS: 30, TimeoutMs: 2000},
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

// loadTemplate populates a fake device with plausible values for every point
// in a template, so a poll exercises the real address layout.
func loadTemplate(t *testing.T, d *fakeDevice, mapID string) *mapping.ModbusTemplate {
	t.Helper()
	tpl, ok := loadMaps(t).TemplateByID(mapID)
	if !ok {
		t.Fatalf("template %s not found", mapID)
	}
	for _, p := range tpl.Points {
		switch p.Space {
		case "discrete", "coil":
			d.setBit(p.Addr, p.Role == "validity")
		case "input", "holding":
			switch RegisterWidth(p.Dtype) {
			case 2:
				d.setReg32(p.Addr, 1000, tpl.WordOrder)
			default:
				d.setReg(p.Addr, 100)
			}
		}
	}
	return tpl
}

// --------------------------------------------------------------- tests

// Word order is per vendor, and getting it wrong does not fail - it returns a
// number 65536 times too large. This is the single most common Modbus
// integration bug.
func TestWordOrderIsPerTemplate(t *testing.T) {
	big, ok := loadMaps(t).TemplateByID("SIM-ION9000-v1")
	if !ok {
		t.Fatal("ION template missing")
	}
	swap, ok := loadMaps(t).TemplateByID("SIM-EATON-PXG-MAGNUM-v1")
	if !ok {
		t.Fatal("Eaton template missing")
	}
	if big.WordOrder != WordBig {
		t.Errorf("ION word order %q, want big", big.WordOrder)
	}
	if swap.WordOrder != WordSwap {
		t.Errorf("Eaton word order %q, want swap", swap.WordOrder)
	}

	// 0x0001_0000 is 65536 read one way and 1 read the other.
	regs := []uint16{0x0001, 0x0000}
	hiFirst, _ := DecodeValue("u32", regs, WordBig)
	loFirst, _ := DecodeValue("u32", regs, WordSwap)
	if hiFirst != 65536 || loFirst != 1 {
		t.Fatalf("big=%v swap=%v, want 65536 and 1", hiFirst, loFirst)
	}
}

func TestDecodeValueTypes(t *testing.T) {
	f := math.Float32bits(415.25)
	cases := []struct {
		dtype string
		regs  []uint16
		order string
		want  float64
	}{
		{"u16", []uint16{0xFFFF}, WordBig, 65535},
		{"s16", []uint16{0xFFFF}, WordBig, -1},
		{"u32", []uint16{0xFFFF, 0xFFFF}, WordBig, 4294967295},
		{"s32", []uint16{0xFFFF, 0xFFFF}, WordBig, -1},
		{"f32", []uint16{uint16(f >> 16), uint16(f)}, WordBig, 415.25},
		{"f32", []uint16{uint16(f), uint16(f >> 16)}, WordSwap, 415.25},
	}
	for _, c := range cases {
		got, err := DecodeValue(c.dtype, c.regs, c.order)
		if err != nil {
			t.Errorf("%s: %v", c.dtype, err)
			continue
		}
		if math.Abs(got-c.want) > 0.001 {
			t.Errorf("%s %v %s: got %v, want %v", c.dtype, c.regs, c.order, got, c.want)
		}
	}
}

// Real maps are sparse and a read crossing an unimplemented address is refused
// in its entirety, so a blind span loses every point either side of the hole.
func TestPlanBlocksNeverSpansAGap(t *testing.T) {
	// The ION layout: scalars at 0x00, phases at 0x10, power at 0x20.
	addrs := []uint16{0x0000, 0x0002, 0x0004, 0x0010, 0x0012, 0x0020, 0x0030}
	widths := map[uint16]int{}
	for _, a := range addrs {
		widths[a] = 2
	}
	blocks := PlanBlocks(addrs, widths, maxReadRegisters)

	if len(blocks) != 4 {
		t.Fatalf("got %d blocks, want 4 (one per populated run): %+v", len(blocks), blocks)
	}
	for _, b := range blocks {
		for a := b.Start; a < b.Start+b.Count; a++ {
			covered := false
			for _, want := range addrs {
				if a >= want && a < want+uint16(widths[want]) {
					covered = true
				}
			}
			if !covered {
				t.Errorf("block %+v spans unimplemented address 0x%04X", b, a)
			}
		}
	}
}

func TestPlanBlocksRespectsTheReadLimit(t *testing.T) {
	addrs := make([]uint16, 300)
	widths := map[uint16]int{}
	for i := range addrs {
		addrs[i] = uint16(i)
		widths[uint16(i)] = 1
	}
	for _, b := range PlanBlocks(addrs, widths, maxReadRegisters) {
		if int(b.Count) > maxReadRegisters {
			t.Fatalf("block of %d registers exceeds the %d limit",
				b.Count, maxReadRegisters)
		}
	}
}

func TestPollDecodesAWholeTemplate(t *testing.T) {
	d := newFakeDevice(t)
	tpl := loadTemplate(t, d, "SIM-PM5000-v1")
	// Volts scale by 10: 2300 raw is 230.0 V.
	d.setReg(0x0010, 2300)
	// Active power is s32 kW, and the registry stores watts.
	d.setReg32(0x0020, 812, tpl.WordOrder)

	out, err := newAdapter(t).Poll(context.Background(), endpointFor(d, "mpp"))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	got := collect(out)

	if v, ok := got["voltage_ln{A}"]; !ok || math.Abs(v.DoubleValue-230.0) > 0.01 {
		t.Errorf("phase A volts %v (present=%v), want 230", v.DoubleValue, ok)
	}
	if v, ok := got["power_draw"]; !ok || math.Abs(v.DoubleValue-812000) > 1 {
		t.Errorf("power_draw %v W (present=%v), want 812000", v.DoubleValue, ok)
	}
	if v := got["power_draw"]; v.Unit != "W" {
		t.Errorf("power_draw unit %q, want W", v.Unit)
	}
	if _, ok := got["equipment_state{Panel_Energized}"]; !ok {
		t.Error("status bit missing")
	}
}

// A device that says its data is not valid has not measured anything. Emitting
// the registers anyway publishes a meter reading zero watts, which is
// indistinguishable from a de-energised panel.
func TestInvalidDataIsMissesNotZeros(t *testing.T) {
	d := newFakeDevice(t)
	loadTemplate(t, d, "SIM-PM5000-v1")
	d.setBit(0x0001, false) // Data_Valid low

	out, err := newAdapter(t).Poll(context.Background(), endpointFor(d, "mpp"))
	if err == nil {
		t.Fatal("expected an error when the device reports invalid data")
	}
	if class := models.ClassifyError(err); class != models.ErrClassNotReady {
		t.Fatalf("error class %q, want not_ready", class)
	}
	if len(out.Samples) != 0 {
		t.Fatalf("%d samples published from data the device called invalid",
			len(out.Samples))
	}
	if len(out.Misses) == 0 {
		t.Error("no misses recorded")
	}
}

// Exception 0x0B is the one thing no other protocol here can say: the gateway
// answered and the field device behind it did not. The path is fine and the
// instrument is off.
func TestGatewayTargetFailureIsUnreachable(t *testing.T) {
	d := newFakeDevice(t)
	loadTemplate(t, d, "SIM-RTD-TX-v1")
	d.mu.Lock()
	d.failUnits = map[byte]byte{7: exGatewayNoRespond}
	d.mu.Unlock()

	ep := endpointFor(d, "sensor")
	ep.Role = "field_device"
	ep.Addressing = map[string]any{"unit_id": 7, "probe_role": "chw_supply"}

	_, err := newAdapter(t).Poll(context.Background(), ep)
	if err == nil {
		t.Fatal("expected an error")
	}
	if class := models.ClassifyError(err); class != models.ErrClassUnreachable {
		t.Fatalf("error class %q, want unreachable", class)
	}
}

// Exception 0x0A means the gateway is fine and nothing is configured at that
// unit id - our inventory is wrong, not the plant.
func TestUnknownUnitIsConfig(t *testing.T) {
	d := newFakeDevice(t)
	loadTemplate(t, d, "SIM-RTD-TX-v1")
	d.mu.Lock()
	d.failUnits = map[byte]byte{9: exGatewayPath}
	d.mu.Unlock()

	ep := endpointFor(d, "sensor")
	ep.Role = "field_device"
	ep.Addressing = map[string]any{"unit_id": 9, "probe_role": "chw_supply"}

	_, err := newAdapter(t).Poll(context.Background(), ep)
	if err == nil {
		t.Fatal("expected an error")
	}
	if class := models.ClassifyError(err); class != models.ErrClassConfig {
		t.Fatalf("error class %q, want config", class)
	}
}

// FC43 exists to answer "is this template the right one for this device". A
// mismatch must stop the poll: a wrong template does not error at read time,
// it decodes whatever is there into plausible numbers.
func TestTemplateMismatchIsRefused(t *testing.T) {
	d := newFakeDevice(t)
	loadTemplate(t, d, "SIM-PM5000-v1")
	d.mu.Lock()
	d.identity = &DeviceIdentity{Vendor: "Schneider Electric",
		Product: "PowerLogic PM5000", Revision: "SIM-SOMETHING-ELSE-v9"}
	d.mu.Unlock()

	_, err := newAdapter(t).Poll(context.Background(), endpointFor(d, "mpp"))
	if err == nil {
		t.Fatal("polled a device whose identity contradicts the template")
	}
	if class := models.ClassifyError(err); class != models.ErrClassConfig {
		t.Fatalf("error class %q, want config", class)
	}
}

func TestMatchingIdentityIsAccepted(t *testing.T) {
	d := newFakeDevice(t)
	loadTemplate(t, d, "SIM-PM5000-v1")
	d.mu.Lock()
	d.identity = &DeviceIdentity{Vendor: "Schneider Electric",
		Product: "PowerLogic PM5000", Revision: "SIM-PM5000-v1"}
	d.mu.Unlock()

	if _, err := newAdapter(t).Poll(context.Background(), endpointFor(d, "mpp")); err != nil {
		t.Fatalf("poll: %v", err)
	}
}

// FC43 is optional and plenty of real gear rejects it. Refusing to poll a
// working meter over an optional function would be the integration choosing
// its own convenience over the data.
func TestMissingFC43StillPolls(t *testing.T) {
	d := newFakeDevice(t)
	loadTemplate(t, d, "SIM-PM5000-v1") // identity stays nil: FC43 unimplemented

	out, err := newAdapter(t).Poll(context.Background(), endpointFor(d, "mpp"))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	if len(out.Samples) == 0 {
		t.Fatal("no samples from a device that does not implement FC43")
	}
}

// An enumerated state word is text, not a number. "2" on a UPS is not a
// measurement of anything.
func TestEnumeratedStateBecomesText(t *testing.T) {
	d := newFakeDevice(t)
	loadTemplate(t, d, "SIM-ISUNITY-EXL-v1")
	d.setHold(0x0100, 2) // battery

	out, err := newAdapter(t).Poll(context.Background(), endpointFor(d, "ups"))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	s, ok := collect(out)["operating_mode"]
	if !ok {
		t.Fatal("operating_mode missing")
	}
	if s.ValueType != models.ValueTypeText {
		t.Fatalf("value type %v, want text", s.ValueType)
	}
	if s.TextValue != "battery" {
		t.Fatalf("operating mode %q, want battery", s.TextValue)
	}
}

// A state the template does not list is reported as unknown rather than
// dropped: firmware that adds a mode should be visible as an unknown mode, not
// as no mode at all.
func TestUnlistedEnumIsReportedNotDropped(t *testing.T) {
	d := newFakeDevice(t)
	loadTemplate(t, d, "SIM-ISUNITY-EXL-v1")
	d.setHold(0x0100, 42)

	out, err := newAdapter(t).Poll(context.Background(), endpointFor(d, "ups"))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	s, ok := collect(out)["operating_mode"]
	if !ok {
		t.Fatal("operating_mode missing")
	}
	if s.TextValue != "unknown(42)" {
		t.Fatalf("got %q, want unknown(42)", s.TextValue)
	}
}

// A transmitter's reading means nothing without knowing where it is installed.
func TestProbeRoleSelectsTheMetric(t *testing.T) {
	d := newFakeDevice(t)
	loadTemplate(t, d, "SIM-RTD-TX-v1")
	d.setReg(0x0000, 72) // 7.2 degC at scale 10

	ep := endpointFor(d, "sensor")
	ep.Addressing = map[string]any{"unit_id": 1, "probe_role": "chw_supply"}

	out, err := newAdapter(t).Poll(context.Background(), ep)
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	s, ok := collect(out)["water_supply_temp{CHW}"]
	if !ok {
		t.Fatalf("expected water_supply_temp{CHW}, got %v", collect(out))
	}
	if math.Abs(s.DoubleValue-7.2) > 0.01 {
		t.Fatalf("value %v, want 7.2", s.DoubleValue)
	}
}

func TestUnknownProbeRoleIsConfigNotAGuess(t *testing.T) {
	d := newFakeDevice(t)
	loadTemplate(t, d, "SIM-RTD-TX-v1")

	ep := endpointFor(d, "sensor")
	ep.Addressing = map[string]any{"unit_id": 1} // no probe role

	_, err := newAdapter(t).Poll(context.Background(), ep)
	if err == nil {
		t.Fatal("polled a transmitter with no installed role")
	}
	if class := models.ClassifyError(err); class != models.ErrClassConfig {
		t.Fatalf("error class %q, want config", class)
	}
}

// Modbus/TCP's only correlation is a 16-bit transaction id. A stale reply
// carries a real value for a register nobody asked about now.
func TestTransactionMismatchIsRejected(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		defer c.Close()
		header := make([]byte, mbapLen)
		if _, err := io.ReadFull(c, header); err != nil {
			return
		}
		total, _ := aduLength(header)
		rest := make([]byte, total-mbapLen)
		_, _ = io.ReadFull(c, rest)
		// Answer with a different transaction id.
		bad := encodeADU(0xBEEF, 1, []byte{fcReadInput, 2, 0x01, 0x02})
		_, _ = c.Write(bad)
	}()

	c := NewClient(700*time.Millisecond, 0, testLogger())
	defer c.Close()
	_, err = c.ReadRegisters(context.Background(), ln.Addr().String(), 1, "input", 0, 1)
	if !errors.Is(err, ErrMismatch) {
		t.Fatalf("got %v, want a transaction mismatch", err)
	}
}

// A device that answered with an exception answered. Repeating the request
// produces the same exception and costs a slot the working points need.
func TestExceptionsAreNotRetried(t *testing.T) {
	d := newFakeDevice(t)
	d.mu.Lock()
	d.failUnits = map[byte]byte{1: exIllegalAddress}
	d.mu.Unlock()

	c := NewClient(700*time.Millisecond, 3, testLogger())
	defer c.Close()
	host, port := d.addr()
	addr := net.JoinHostPort(host, itoa(port))

	_, err := c.ReadRegisters(context.Background(), addr, 1, "input", 0, 1)
	var ex *Exception
	if !errors.As(err, &ex) {
		t.Fatalf("got %v, want an exception", err)
	}
	if reads, _ := d.counts(); reads != 0 {
		t.Fatalf("device saw %d register reads; the exception path counts none", reads)
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}

// One refused block must not cost the rest of the device.
func TestOneRefusedBlockKeepsTheOthers(t *testing.T) {
	d := newFakeDevice(t)
	tpl := loadTemplate(t, d, "SIM-PM5000-v1")
	// Remove the energy accumulator, as a firmware revision that moved it
	// would: the block covering 0x0030 now answers exception 02.
	d.mu.Lock()
	delete(d.input, 0x0030)
	delete(d.input, 0x0031)
	d.mu.Unlock()
	_ = tpl

	out, err := newAdapter(t).Poll(context.Background(), endpointFor(d, "mpp"))
	if err != nil {
		t.Fatalf("poll failed because one block was refused: %v", err)
	}
	if !out.Partial {
		t.Error("outcome not marked partial")
	}
	got := collect(out)
	if _, ok := got["voltage_ln{A}"]; !ok {
		t.Error("a working point was lost with the refused block")
	}
	if _, ok := got["energy_consumed"]; ok {
		t.Error("the refused point produced a sample")
	}
	found := false
	for _, m := range out.Misses {
		if m.Metric == "energy_consumed" && m.Reason == models.MissNoSuchObject {
			found = true
		}
	}
	if !found {
		t.Errorf("expected a no_such_object miss, got %+v", out.Misses)
	}
}

// Blind-scanning a sparse map is what block planning exists to avoid: the poll
// must not issue one wide read across the holes.
func TestPollDoesNotBlindScan(t *testing.T) {
	d := newFakeDevice(t)
	loadTemplate(t, d, "SIM-ION9000-v1")

	if _, err := newAdapter(t).Poll(context.Background(), endpointFor(d, "utility_feed")); err != nil {
		t.Fatalf("poll: %v", err)
	}
	_, maxSpan := d.counts()
	// The ION map spans 0x0000..0x0031; a blind read would be ~50 registers.
	if maxSpan > 24 {
		t.Fatalf("largest single read was %d registers, which spans the gaps",
			maxSpan)
	}
}

func TestUnmappedDeviceTypeIsConfig(t *testing.T) {
	d := newFakeDevice(t)
	_, err := newAdapter(t).Poll(context.Background(), endpointFor(d, "toaster"))
	if err == nil {
		t.Fatal("polled a device type with no template")
	}
	if class := models.ClassifyError(err); class != models.ErrClassConfig {
		t.Fatalf("error class %q, want config", class)
	}
}

func TestConnectionIsReused(t *testing.T) {
	d := newFakeDevice(t)
	loadTemplate(t, d, "SIM-PM5000-v1")
	a := newAdapter(t)
	ep := endpointFor(d, "mpp")

	for i := 0; i < 3; i++ {
		if _, err := a.Poll(context.Background(), ep); err != nil {
			t.Fatalf("poll %d: %v", i, err)
		}
	}
	if n := a.client.Connections(); n != 1 {
		t.Fatalf("%d connections for one device across three polls, want 1", n)
	}
}
