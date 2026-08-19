package modbus

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"strings"
	"sync"
	"time"

	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// Adapter polls electrical gear and field instruments over Modbus/TCP.
//
// The shape of the poll follows from what Modbus does not provide:
//
//  1. THE TEMPLATE IS CHOSEN BEFORE THE FIRST REQUEST, from the device type,
//     because there is nothing to discover. FC43 is then used to CHECK that
//     choice - the only order the protocol allows. A wrong template does not
//     fail, it returns numbers.
//
//  2. READS ARE PLANNED INTO CONTIGUOUS BLOCKS. Real maps are sparse and a
//     read that crosses an unimplemented address is refused in its entirety,
//     so a blind span loses every point either side of the hole.
//
//  3. THE VALIDITY BIT GATES EVERYTHING. A register carries no quality flag:
//     two bytes reading zero are indistinguishable from two bytes never
//     sampled. When a device says its data is not valid, the poll reports
//     misses rather than a meter reading zero watts.
type Adapter struct {
	client *Client
	maps   *mapping.ModbusMap
	log    *slog.Logger
	mets   *obs.Metrics

	mu       sync.Mutex
	verified map[string]*deviceProfile
}

type deviceProfile struct {
	template *mapping.ModbusTemplate
	identity DeviceIdentity
	// Points selected for this endpoint, with the probe role already applied.
	points []mapping.ModbusPoint
	at     time.Time
}

func New(maps *mapping.ModbusMap, client *Client, log *slog.Logger,
	mets *obs.Metrics) *Adapter {
	return &Adapter{
		client: client, maps: maps, log: log, mets: mets,
		verified: make(map[string]*deviceProfile),
	}
}

func (a *Adapter) Protocol() string { return "modbus" }

func (a *Adapter) Init(_ context.Context) error { return nil }

func (a *Adapter) Close(_ context.Context) error { return a.client.Close() }

// Forget drops a device's profile and its connection, so the next poll starts
// clean. A re-templated or replaced device has different registers, and
// reading the old ones returns exception 02 for every one of them.
func (a *Adapter) Forget(endpointID string) {
	a.mu.Lock()
	delete(a.verified, endpointID)
	a.mu.Unlock()
}

// target is where to send and who to ask for.
type target struct {
	addr      string // host:port
	unit      byte
	probeRole string
	viaGW     bool
}

func targetOf(ep *models.Endpoint) (target, error) {
	t := target{unit: 1}
	if ep.Address == "" {
		return t, fmt.Errorf("%w: endpoint has no address", models.ErrConfig)
	}
	port := ep.Port
	if port == 0 {
		port = 502
	}
	t.addr = net.JoinHostPort(ep.Address, fmt.Sprint(port))

	if v, ok := ep.Addressing["unit_id"]; ok {
		if n, ok := toInt(v); ok {
			// Unit 0 is the broadcast address on a serial line and is not a
			// device. Gear that ignores the unit id answers anyway, which is
			// exactly why a real 0 here has to be treated as unset rather
			// than trusted.
			if n > 0 && n <= 247 {
				t.unit = byte(n)
			}
		}
	}
	if v, ok := ep.Addressing["probe_role"].(string); ok {
		t.probeRole = v
	}
	if ep.Role == "field_device" {
		t.viaGW = true
	}
	return t, nil
}

func toInt(v any) (int, bool) {
	switch n := v.(type) {
	case int:
		return n, true
	case int64:
		return int(n), true
	case float64:
		return int(n), true
	case string:
		var out int
		if _, err := fmt.Sscanf(n, "%d", &out); err == nil {
			return out, true
		}
	}
	return 0, false
}

// Poll reads every mapped point on one device.
func (a *Adapter) Poll(ctx context.Context, ep *models.Endpoint) (*models.PollOutcome, error) {
	started := time.Now()
	tgt, err := targetOf(ep)
	if err != nil {
		return nil, err
	}

	profile, err := a.profileFor(ctx, ep, tgt)
	if err != nil {
		return nil, err
	}

	outcome := &models.PollOutcome{}
	now := models.NowMicros()

	// The validity bit first. Reading it after the registers would report a
	// batch of zeros and then discover they meant nothing.
	valid, err := a.readValidity(ctx, profile, tgt)
	if err != nil {
		return outcome, classify(err, tgt)
	}
	if !valid {
		// The device is answering and saying its measurements are not ready.
		// Every point is a miss; none is a zero.
		for _, p := range profile.points {
			outcome.Misses = append(outcome.Misses,
				models.Miss{Metric: p.Metric, Reason: models.MissNotReady})
		}
		outcome.Partial = true
		outcome.LatencyMs = int(time.Since(started).Milliseconds())
		return outcome, fmt.Errorf("%w: %s reports its data as not valid",
			models.ErrNotReady, ep.Address)
	}

	transportErr := a.readSpaces(ctx, profile, tgt, ep, outcome, now)

	outcome.LatencyMs = int(time.Since(started).Milliseconds())
	outcome.Partial = len(outcome.Misses) > 0
	if len(outcome.Samples) == 0 {
		if transportErr != nil {
			return outcome, classify(transportErr, tgt)
		}
		return outcome, fmt.Errorf("%w: no registers decoded from %s",
			models.ErrDecode, ep.Address)
	}
	a.mets.SamplesTotal.WithLabelValues("modbus").Add(float64(len(outcome.Samples)))
	return outcome, nil
}

// readSpaces reads each address space in planned blocks.
func (a *Adapter) readSpaces(ctx context.Context, profile *deviceProfile,
	tgt target, ep *models.Endpoint, outcome *models.PollOutcome, now int64) error {

	var firstErr error
	wordOrder := profile.template.WordOrder

	for _, space := range []string{"input", "holding", "discrete", "coil"} {
		points := pointsIn(profile.points, space)
		if len(points) == 0 {
			continue
		}
		bits := space == "discrete" || space == "coil"

		widths := make(map[uint16]int, len(points))
		addrs := make([]uint16, 0, len(points))
		for _, p := range points {
			w := 1
			if !bits {
				w = RegisterWidth(p.Dtype)
			}
			if prev, ok := widths[p.Addr]; !ok || w > prev {
				widths[p.Addr] = w
			}
			addrs = append(addrs, p.Addr)
		}
		limit := maxReadRegisters
		if bits {
			limit = maxReadBits
		}

		for _, block := range PlanBlocks(addrs, widths, limit) {
			if bits {
				vals, err := a.client.ReadBits(ctx, tgt.addr, tgt.unit, space,
					block.Start, block.Count)
				if err != nil {
					firstErr = a.noteBlockFailure(err, points, block, outcome, firstErr)
					continue
				}
				a.collectBits(ep, points, block, vals, outcome, now)
				continue
			}
			regs, err := a.client.ReadRegisters(ctx, tgt.addr, tgt.unit, space,
				block.Start, block.Count)
			if err != nil {
				firstErr = a.noteBlockFailure(err, points, block, outcome, firstErr)
				continue
			}
			a.collectRegisters(ep, points, block, regs, wordOrder, outcome, now)
		}
	}
	return firstErr
}

// noteBlockFailure records a miss per point in the failed block. One block
// failing must not cost the others: a device may implement most of a template
// and refuse one register a firmware revision moved.
func (a *Adapter) noteBlockFailure(err error, points []mapping.ModbusPoint,
	block Block, outcome *models.PollOutcome, firstErr error) error {

	reason := models.MissProtocolError
	var ex *Exception
	if errors.As(err, &ex) {
		switch {
		case ex.IsAddressFault():
			reason = models.MissNoSuchObject
		case ex.IsFieldDeviceDown(), ex.IsUnitAbsent():
			reason = models.MissTimeout
		}
	} else if errors.Is(err, ErrShort) || errors.Is(err, ErrProtocol) ||
		errors.Is(err, ErrMismatch) {
		reason = models.MissDecode
	}

	for _, p := range points {
		if p.Addr < block.Start || p.Addr >= block.Start+block.Count {
			continue
		}
		outcome.Misses = append(outcome.Misses,
			models.Miss{Metric: p.Metric, Reason: reason})
	}
	if firstErr == nil {
		return err
	}
	return firstErr
}

func pointsIn(points []mapping.ModbusPoint, space string) []mapping.ModbusPoint {
	out := make([]mapping.ModbusPoint, 0, len(points))
	for _, p := range points {
		if p.Space == space {
			out = append(out, p)
		}
	}
	return out
}

func (a *Adapter) collectRegisters(ep *models.Endpoint, points []mapping.ModbusPoint,
	block Block, regs []uint16, wordOrder string,
	outcome *models.PollOutcome, now int64) {

	for _, p := range points {
		if p.Addr < block.Start {
			continue
		}
		offset := int(p.Addr - block.Start)
		width := RegisterWidth(p.Dtype)
		if offset+width > len(regs) {
			continue
		}
		raw, err := DecodeValue(p.Dtype, regs[offset:offset+width], wordOrder)
		if err != nil {
			outcome.Misses = append(outcome.Misses,
				models.Miss{Metric: p.Metric, Reason: models.MissDecode})
			continue
		}
		if len(p.Enum) > 0 {
			text, ok := p.Enum[int(raw)]
			if !ok {
				// An unlisted state is reported as its raw value rather than
				// dropped: a firmware that added a mode should be visible as
				// an unknown mode, not as no mode at all.
				text = fmt.Sprintf("unknown(%d)", int(raw))
			}
			outcome.Samples = append(outcome.Samples, a.textSample(ep, p, text, now))
			continue
		}
		outcome.Samples = append(outcome.Samples,
			a.sample(ep, p, p.Value(raw), now))
	}
}

func (a *Adapter) collectBits(ep *models.Endpoint, points []mapping.ModbusPoint,
	block Block, vals []bool, outcome *models.PollOutcome, now int64) {

	for _, p := range points {
		if p.Addr < block.Start {
			continue
		}
		offset := int(p.Addr - block.Start)
		if offset >= len(vals) {
			continue
		}
		v := 0.0
		if vals[offset] {
			v = 1.0
		}
		outcome.Samples = append(outcome.Samples, a.sample(ep, p, v, now))
	}
}

func (a *Adapter) sample(ep *models.Endpoint, p mapping.ModbusPoint, value float64,
	now int64) models.Telemetry {

	def, _ := models.ValidateMetric(p.Metric)
	vt := models.ValueTypeGauge
	var uintValue uint64
	switch def.ValueType {
	case "bool":
		vt = models.ValueTypeBool
	case "counter":
		vt = models.ValueTypeCounter
		if value > 0 {
			uintValue = uint64(value + 0.5)
		}
	}
	return models.Telemetry{
		EndpointID:     ep.ID,
		DeviceID:       ep.DeviceID,
		Metric:         p.Metric,
		Instance:       p.Instance,
		ValueType:      vt,
		DoubleValue:    value,
		UintValue:      uintValue,
		BoolValue:      value != 0,
		Unit:           def.Unit,
		ObservedAt:     now,
		CollectedAt:    now,
		SourceProtocol: models.ProtocolModbus,
		Quality:        models.QualityGood,
		// The register this came from, so a suspicious number can be traced to
		// an address without re-reading the device.
		Metadata: map[string]string{
			"point":    p.Name,
			"register": fmt.Sprintf("%s:0x%04X", p.Space, p.Addr),
		},
	}
}

func (a *Adapter) textSample(ep *models.Endpoint, p mapping.ModbusPoint, text string,
	now int64) models.Telemetry {

	def, _ := models.ValidateMetric(p.Metric)
	return models.Telemetry{
		EndpointID:     ep.ID,
		DeviceID:       ep.DeviceID,
		Metric:         p.Metric,
		Instance:       p.Instance,
		ValueType:      models.ValueTypeText,
		TextValue:      text,
		Unit:           def.Unit,
		ObservedAt:     now,
		CollectedAt:    now,
		SourceProtocol: models.ProtocolModbus,
		Quality:        models.QualityGood,
		Metadata: map[string]string{
			"point":    p.Name,
			"register": fmt.Sprintf("%s:0x%04X", p.Space, p.Addr),
		},
	}
}

// ------------------------------------------------------------ profile

func (a *Adapter) profileFor(ctx context.Context, ep *models.Endpoint,
	tgt target) (*deviceProfile, error) {

	a.mu.Lock()
	p, ok := a.verified[ep.ID]
	a.mu.Unlock()
	if ok {
		return p, nil
	}

	template, ok := a.maps.TemplateFor(ep.DeviceType, tgt.probeRole)
	if !ok {
		if roles := a.maps.ProbeRolesFor(ep.DeviceType); len(roles) > 0 {
			// The type is known but needs an installed role to pick between
			// the instruments that share it.
			return nil, fmt.Errorf(
				"%w: device type %q needs addressing.probe_role, one of %v",
				models.ErrConfig, ep.DeviceType, roles)
		}
		return nil, fmt.Errorf("%w: no modbus template for device type %q",
			models.ErrConfig, ep.DeviceType)
	}

	points := template.Telemetry(tgt.probeRole)
	if len(points) == 0 {
		if len(template.ProbeRoles) > 0 {
			// A transmitter whose installed location is unknown is measuring
			// something; guessing which loop would put a condenser reading on
			// a chilled-water chart.
			return nil, fmt.Errorf("%w: %s needs addressing.probe_role, one of %v",
				models.ErrConfig, template.MapID, template.ProbeRoleNames())
		}
		return nil, fmt.Errorf("%w: template %s selects no points",
			models.ErrConfig, template.MapID)
	}

	profile := &deviceProfile{template: template, points: points, at: time.Now().UTC()}

	// FC43 is optional. An identity we can read is checked against the
	// template; one we cannot is simply unknown, and refusing to poll a
	// working meter over an optional function would be the integration
	// choosing its own convenience over the data.
	identity, err := a.client.ReadIdentity(ctx, tgt.addr, tgt.unit)
	switch {
	case err == nil:
		profile.identity = identity
		if identity.Revision != "" && identity.Revision != template.MapID {
			// This is the check that a template belongs to the device in front
			// of it. A mismatched template does not error at read time - it
			// decodes whatever is there into plausible numbers.
			return nil, fmt.Errorf(
				"%w: %s reports map %q but the template for device type %q is %s",
				models.ErrConfig, ep.Address, identity.Revision,
				ep.DeviceType, template.MapID)
		}
	default:
		var ex *Exception
		if errors.As(err, &ex) {
			a.log.Debug("device does not implement FC43",
				"endpoint", ep.ID, "address", ep.Address, "exception", ex.Code)
		} else {
			// A transport failure here is a real failure - the device is not
			// answering at all.
			return nil, classify(err, tgt)
		}
	}

	a.mu.Lock()
	a.verified[ep.ID] = profile
	a.mu.Unlock()

	a.log.Info("modbus device profiled", "endpoint", ep.ID, "address", ep.Address,
		"unit", tgt.unit, "device_type", ep.DeviceType, "template", template.MapID,
		"identity", identity.Product, "points", len(points))
	return profile, nil
}

// readValidity reads the point that says whether the registers mean anything.
// A template without one is treated as always valid, which is the honest
// reading of a device that publishes no such point.
func (a *Adapter) readValidity(ctx context.Context, profile *deviceProfile,
	tgt target) (bool, error) {

	p, ok := profile.template.Validity()
	if !ok {
		return true, nil
	}
	vals, err := a.client.ReadBits(ctx, tgt.addr, tgt.unit, p.Space, p.Addr, 1)
	if err != nil {
		return false, err
	}
	if len(vals) == 0 {
		return false, fmt.Errorf("%w: empty validity read", ErrShort)
	}
	return vals[0], nil
}

// ------------------------------------------------------- error shaping

// classify maps a transport or protocol failure onto the health vocabulary.
//
// The distinction that matters is exception 0x0B. On a serial gateway it means
// the gateway answered and the field device behind it did not - the network is
// fine and the instrument is off. Reporting that as a timeout sends someone to
// look at the network; reporting it as a protocol fault sends them to look at
// the template. Neither is where the fault is.
func classify(err error, tgt target) error {
	var ex *Exception
	if errors.As(err, &ex) {
		switch {
		case ex.IsFieldDeviceDown():
			return fmt.Errorf("%w: gateway reached, unit %d did not answer",
				models.ErrUnreachable, tgt.unit)
		case ex.IsUnitAbsent():
			return fmt.Errorf("%w: nothing configured at unit %d on %s",
				models.ErrConfig, tgt.unit, tgt.addr)
		case ex.IsAddressFault():
			return fmt.Errorf("%w: %v - the template does not match the device",
				models.ErrConfig, err)
		default:
			return fmt.Errorf("%w: %v", models.ErrProtocolStatus, err)
		}
	}
	switch {
	case errors.Is(err, ErrShort), errors.Is(err, ErrProtocol), errors.Is(err, ErrMismatch):
		return fmt.Errorf("%w: %v", models.ErrDecode, err)
	case isTimeout(err):
		return fmt.Errorf("%w: %v", models.ErrTimeout, err)
	}
	return fmt.Errorf("%w: %v", models.ErrUnreachable, err)
}

func isTimeout(err error) bool {
	var ne net.Error
	if errors.As(err, &ne) {
		return ne.Timeout()
	}
	return strings.Contains(err.Error(), "timeout")
}
