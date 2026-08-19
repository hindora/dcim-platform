package bacnet

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// Adapter polls mechanical and electrical plant over BACnet/IP.
//
// The shape of a BACnet poll is set by two facts about real controllers:
//
//  1. POINTS ARE DISCOVERED, NOT ASSUMED. The object-list is read once and the
//     object NAMES cached. Names are what a mapping can rely on; instance
//     numbers shift whenever an integrator inserts a point.
//
//  2. ONLY MAPPED POINTS ARE READ. An EV2 panel exposes 233 objects. Reading
//     all of them every 30 seconds is how a BACnet integration takes a
//     controller offline - the controller answers slowly, the client retries,
//     and the trunk saturates. Everything else is discovered, counted, and
//     left alone.
type Adapter struct {
	client *Client
	maps   *mapping.BACnetMap
	log    *slog.Logger
	mets   *obs.Metrics

	// Objects per RPM request. Kept well under a 1476-byte APDU: an
	// oversized request comes back as an abort, not a short answer.
	batchSize int

	mu        sync.Mutex
	discovery map[string]*deviceProfile
}

// deviceProfile is what one device looks like, discovered once.
type deviceProfile struct {
	deviceObj ObjectID
	points    []discoveredPoint
	unmapped  int
	at        time.Time
}

type discoveredPoint struct {
	object   ObjectID
	name     string
	metric   string
	instance string
	scale    float64
	binary   bool
}

func New(maps *mapping.BACnetMap, client *Client, log *slog.Logger,
	mets *obs.Metrics, batchSize int) *Adapter {
	if batchSize <= 0 {
		batchSize = 16
	}
	return &Adapter{
		client: client, maps: maps, log: log, mets: mets,
		batchSize: batchSize,
		discovery: make(map[string]*deviceProfile),
	}
}

func (a *Adapter) Protocol() string { return "bacnet" }

func (a *Adapter) Init(_ context.Context) error { return a.client.Open() }

func (a *Adapter) Close(_ context.Context) error { return a.client.Close() }

// Forget drops a device's cached profile so the next poll rediscovers it.
// Called when an endpoint changes or a device reports itself restarted: a
// controller that has been reprogrammed has different points, and polling the
// old ones returns unknown-object for every one of them.
func (a *Adapter) Forget(endpointID string) {
	a.mu.Lock()
	delete(a.discovery, endpointID)
	a.mu.Unlock()
}

// addressOf builds the BACnet address from the endpoint.
//
// A field device behind an MS/TP router carries network and MAC in its
// addressing rather than an IP of its own - the router's IP is what the
// packet is sent to.
func addressOf(ep *models.Endpoint) (Address, uint32, error) {
	addr := Address{IP: ep.Address, Port: ep.Port}
	if v, ok := ep.Addressing["network"]; ok {
		if n, ok := toInt(v); ok {
			addr.Net = uint16(n)
		}
	}
	if v, ok := ep.Addressing["mac"]; ok {
		if n, ok := toInt(v); ok {
			addr.MAC = []byte{byte(n)}
		}
	}
	var instance uint32
	if v, ok := ep.Addressing["device_instance"]; ok {
		if n, ok := toInt(v); ok {
			instance = uint32(n)
		}
	}
	if addr.IP == "" {
		return addr, 0, fmt.Errorf("%w: endpoint has no address", models.ErrConfig)
	}
	if instance == 0 {
		// Without the device instance there is nothing to read the object-list
		// from. BACnet identity is the instance number, not the IP, and
		// guessing it would read a neighbour's points.
		return addr, 0, fmt.Errorf("%w: addressing.device_instance is required",
			models.ErrConfig)
	}
	return addr, instance, nil
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
	addr, instance, err := addressOf(ep)
	if err != nil {
		return nil, err
	}

	profile, err := a.profileFor(ctx, ep, addr, instance)
	if err != nil {
		return nil, err
	}
	if len(profile.points) == 0 {
		// Nothing mapped is a configuration fault, not a device fault. Saying
		// so is better than an endpoint that reports ONLINE and no data.
		return nil, fmt.Errorf("%w: no mapped points on %s (device type %q, "+
			"%d objects discovered)", models.ErrConfig, ep.Address,
			ep.DeviceType, profile.unmapped)
	}

	outcome := &models.PollOutcome{}
	now := models.NowMicros()
	timeouts, other := 0, 0

	for start := 0; start < len(profile.points); start += a.batchSize {
		end := start + a.batchSize
		if end > len(profile.points) {
			end = len(profile.points)
		}
		batch := profile.points[start:end]

		specs := make([]ReadSpec, 0, len(batch))
		for _, p := range batch {
			specs = append(specs, ReadSpec{Object: p.object,
				Props: []uint32{PropPresentValue}})
		}

		results, err := a.client.ReadPropertyMultiple(ctx, addr, specs)
		if err != nil {
			// One failed batch must not cost the others. A controller that
			// rejects a large RPM still answers the next, smaller one.
			if errors.Is(err, ErrTimeout) {
				timeouts++
			} else {
				other++
			}
			for _, p := range batch {
				outcome.Misses = append(outcome.Misses, models.Miss{
					Metric: p.metric, Reason: missReason(err)})
			}
			continue
		}
		a.collect(ep, batch, results, outcome, now)
	}

	outcome.LatencyMs = int(time.Since(started).Milliseconds())
	outcome.Partial = len(outcome.Misses) > 0
	if len(outcome.Samples) == 0 {
		return outcome, emptyPollError(ep, timeouts, other)
	}
	a.mets.SamplesTotal.WithLabelValues("bacnet").Add(float64(len(outcome.Samples)))
	return outcome, nil
}

// collect turns RPM results into samples, matching each result back to the
// point that asked for it.
func (a *Adapter) collect(ep *models.Endpoint, batch []discoveredPoint,
	results []RPMResult, outcome *models.PollOutcome, now int64) {

	byObject := make(map[ObjectID]*discoveredPoint, len(batch))
	for i := range batch {
		byObject[batch[i].object] = &batch[i]
	}

	for _, r := range results {
		p, ok := byObject[r.Object]
		if !ok {
			// A result for something we did not ask about. Ignored rather
			// than attributed to whatever point happens to be next.
			continue
		}
		if r.Err != nil {
			reason := models.MissNoSuchObject
			if r.Err.IsUnknownProperty() {
				reason = models.MissUnsupported
			}
			outcome.Misses = append(outcome.Misses,
				models.Miss{Metric: p.metric, Reason: reason})
			continue
		}
		if len(r.Values) == 0 {
			outcome.Misses = append(outcome.Misses,
				models.Miss{Metric: p.metric, Reason: models.MissNoSuchObject})
			continue
		}
		v := r.Values[0]
		if v.IsNull {
			// A null present-value means the point is not in service. Emitting
			// zero would read as a real measurement of nothing.
			outcome.Misses = append(outcome.Misses,
				models.Miss{Metric: p.metric, Reason: models.MissNoSuchObject})
			continue
		}
		if p.binary && v.Kind != tagEnumerated && v.Kind != tagBoolean {
			outcome.Misses = append(outcome.Misses,
				models.Miss{Metric: p.metric, Reason: models.MissDecode})
			continue
		}
		outcome.Samples = append(outcome.Samples,
			a.sample(ep, *p, p.scaleValue(v.Num), now))
	}
}

func (p discoveredPoint) scaleValue(v float64) float64 {
	if p.scale != 0 {
		return v * p.scale
	}
	return v
}

func (a *Adapter) sample(ep *models.Endpoint, p discoveredPoint, value float64,
	now int64) models.Telemetry {

	def, _ := models.ValidateMetric(p.metric)
	vt := models.ValueTypeGauge
	switch def.ValueType {
	case "bool":
		vt = models.ValueTypeBool
	case "counter":
		vt = models.ValueTypeCounter
	}
	return models.Telemetry{
		EndpointID:     ep.ID,
		DeviceID:       ep.DeviceID,
		Metric:         p.metric,
		Instance:       p.instance,
		ValueType:      vt,
		DoubleValue:    value,
		Unit:           def.Unit,
		ObservedAt:     now,
		CollectedAt:    now,
		SourceProtocol: models.ProtocolBacnet,
		Quality:        models.QualityGood,
		// The object name and identifier, so a value can be traced back to the
		// exact point on the controller without re-reading the device.
		Metadata: map[string]string{"point": p.name, "object": p.object.String()},
	}
}

// --------------------------------------------------------- discovery

func (a *Adapter) profileFor(ctx context.Context, ep *models.Endpoint,
	addr Address, instance uint32) (*deviceProfile, error) {

	a.mu.Lock()
	p, ok := a.discovery[ep.ID]
	a.mu.Unlock()
	if ok {
		return p, nil
	}

	p, err := a.discover(ctx, ep, addr, instance)
	if err != nil {
		return nil, err
	}

	a.mu.Lock()
	a.discovery[ep.ID] = p
	a.mu.Unlock()

	a.log.Info("bacnet device discovered", "endpoint", ep.ID,
		"address", addr.IP, "device_instance", instance,
		"device_type", ep.DeviceType, "mapped_points", len(p.points),
		"unmapped_objects", p.unmapped)
	return p, nil
}

// discover reads the object list and every object's name, then keeps the ones
// the mapping recognises.
//
// The object list is read ELEMENT BY ELEMENT rather than whole. Reading it
// whole is one request instead of two hundred, but on a large panel the reply
// exceeds the maximum APDU and comes back segmented - and a partially decoded
// object list is indistinguishable from a device with fewer points, which
// would silently drop real telemetry for the ones that fell off the end.
func (a *Adapter) discover(ctx context.Context, ep *models.Endpoint,
	addr Address, instance uint32) (*deviceProfile, error) {

	if !a.maps.HasDeviceType(ep.DeviceType) {
		return nil, fmt.Errorf("%w: no bacnet mapping for device type %q",
			models.ErrConfig, ep.DeviceType)
	}
	deviceObj := ObjectID{Type: ObjDevice, Instance: instance}

	// Element 0 of an array property is its length.
	vals, err := a.client.ReadPropertyIndex(ctx, addr, deviceObj, PropObjectList, 0)
	if err != nil {
		return nil, fmt.Errorf("read object-list length from %s: %w", addr.IP, err)
	}
	if len(vals) == 0 {
		return nil, fmt.Errorf("%w: empty object-list length", ErrUnexpected)
	}
	count := int(vals[0].Num)
	if count <= 0 || count > 10000 {
		return nil, fmt.Errorf("%w: implausible object-list length %d",
			ErrUnexpected, count)
	}

	objects := make([]ObjectID, 0, count)
	for i := 1; i <= count; i++ {
		vals, err := a.client.ReadPropertyIndex(ctx, addr, deviceObj,
			PropObjectList, uint32(i))
		if err != nil {
			return nil, fmt.Errorf("read object-list[%d] from %s: %w", i, addr.IP, err)
		}
		if len(vals) == 0 || vals[0].Kind != tagObjectID {
			continue
		}
		obj := vals[0].Object
		if obj.Type == ObjDevice {
			continue // the device object carries no present-value
		}
		objects = append(objects, obj)
	}

	profile := &deviceProfile{deviceObj: deviceObj, at: time.Now().UTC()}

	// Names are read in RPM batches. This is the expensive half of discovery
	// and the reason it happens once rather than every poll.
	for start := 0; start < len(objects); start += a.batchSize {
		end := start + a.batchSize
		if end > len(objects) {
			end = len(objects)
		}
		specs := make([]ReadSpec, 0, end-start)
		for _, o := range objects[start:end] {
			specs = append(specs, ReadSpec{Object: o, Props: []uint32{PropObjectName}})
		}
		results, err := a.client.ReadPropertyMultiple(ctx, addr, specs)
		if err != nil {
			return nil, fmt.Errorf("read object names from %s: %w", addr.IP, err)
		}
		for _, r := range results {
			if r.Err != nil || len(r.Values) == 0 {
				profile.unmapped++
				continue
			}
			name := strings.TrimSpace(r.Values[0].Text)
			point, ok := a.maps.Match(ep.DeviceType, name)
			if !ok {
				profile.unmapped++
				continue
			}
			profile.points = append(profile.points, discoveredPoint{
				object:   r.Object,
				name:     name,
				metric:   point.Metric,
				instance: point.InstanceFor(name),
				scale:    point.Scale,
				binary:   r.Object.Type == ObjBinaryInput || r.Object.Type == ObjBinaryValue,
			})
		}
	}
	return profile, nil
}

// --------------------------------------------------------- error shaping

// emptyPollError classifies a poll that produced nothing by WHY it produced
// nothing. A device that timed out is unreachable; one that answered with
// errors for every point is reachable with a stale mapping. Reporting both as
// the same failure either hides a dead controller or raises one for a
// renamed point.
func emptyPollError(ep *models.Endpoint, timeouts, other int) error {
	switch {
	case timeouts > 0 && other == 0:
		return fmt.Errorf("%w: no reply from %s", models.ErrTimeout, ep.Address)
	case other > 0:
		return fmt.Errorf("%w: %s answered but returned no usable value",
			models.ErrDecode, ep.Address)
	default:
		return fmt.Errorf("%w: every point on %s was refused or absent",
			models.ErrDecode, ep.Address)
	}
}

func missReason(err error) string {
	switch {
	case errors.Is(err, ErrTimeout):
		return models.MissTimeout
	case errors.Is(err, ErrNoInvokeID):
		return models.MissShed
	}
	var ae *APDUError
	if errors.As(err, &ae) {
		switch {
		case ae.IsUnknownObject():
			return models.MissNoSuchObject
		case ae.IsUnknownProperty():
			return models.MissUnsupported
		default:
			return models.MissProtocolError
		}
	}
	return models.MissProtocolError
}
