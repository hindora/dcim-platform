// Package gnmi implements the gNMI client for network devices.
//
// gNMI is the only protocol here that is a TREE rather than a table, and the
// only one that can push. Both matter:
//
//   - A subscription names a SUBTREE and the device returns everything under
//     it, so the mapping has two halves - which subtrees to ask for, and which
//     leaves inside them are telemetry. There is no OID list to walk.
//
//   - STREAM mode inverts the relationship. The device decides when to send,
//     on a schedule it was asked for but does not guarantee, and the collector
//     stops being a poller. That is the entire reason gNMI exists on a switch:
//     an SNMP walk of ifXTable on a 48-port device is dozens of round trips
//     against the agent's CPU, every cycle, forever.
//
// The Get path implemented here fits the existing scheduler. The Subscribe
// path lives in stream.go and does not - it is a long-lived session, like the
// trap and Redfish event receivers.
package gnmi

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"time"

	gpb "github.com/openconfig/gnmi/proto/gnmi"

	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

type Adapter struct {
	conns *ConnPool
	maps  *mapping.GNMIMap
	log   *slog.Logger
	mets  *obs.Metrics
}

func New(maps *mapping.GNMIMap, conns *ConnPool, log *slog.Logger,
	mets *obs.Metrics) *Adapter {
	return &Adapter{conns: conns, maps: maps, log: log, mets: mets}
}

func (a *Adapter) Protocol() string { return "gnmi" }

func (a *Adapter) Init(_ context.Context) error { return nil }

func (a *Adapter) Close(_ context.Context) error { return a.conns.Close() }

// Forget drops the connection to an endpoint's device.
func (a *Adapter) Forget(endpointID string) { a.conns.ForgetEndpoint(endpointID) }

// target is the addressing gNMI needs: where to dial, and which device the
// server should answer for.
type target struct {
	addr   string
	target string
	tls    bool
}

func targetOf(ep *models.Endpoint) (target, error) {
	t := target{tls: true}
	if ep.Address == "" {
		return t, fmt.Errorf("%w: endpoint has no address", models.ErrConfig)
	}
	port := ep.Port
	if port == 0 {
		port = 57400
	}
	t.addr = fmt.Sprintf("%s:%d", ep.Address, port)

	if v, ok := ep.Addressing["target"].(string); ok && v != "" {
		t.target = v
	}
	if v, ok := ep.Addressing["insecure"].(bool); ok && v {
		t.tls = false
	}
	if v, ok := ep.Addressing["tls"].(bool); ok {
		t.tls = v
	}
	// The target names the DEVICE the server should answer for, which is not
	// always the address dialled: a single collector-facing gNMI service can
	// front many devices. Falling back to the address is right for gear that
	// serves only itself, and harmless for gear that ignores the field.
	if t.target == "" {
		t.target = ep.Address
	}
	return t, nil
}

// Poll fetches every mapped subtree with Get.
//
// Get is a snapshot, which is the wrong shape for gNMI's strengths but the
// right shape for a scheduler that also drives SNMP and Redfish. It is what
// makes a device usable the moment it is imported, before anyone decides
// whether it should stream.
func (a *Adapter) Poll(ctx context.Context, ep *models.Endpoint) (*models.PollOutcome, error) {
	started := time.Now()
	tgt, err := targetOf(ep)
	if err != nil {
		return nil, err
	}

	client, err := a.conns.Client(ctx, ep.ID, tgt)
	if err != nil {
		return nil, classify(err)
	}

	outcome := &models.PollOutcome{}
	now := models.NowMicros()
	var firstErr error

	for _, sub := range a.maps.Subscriptions {
		req := &gpb.GetRequest{
			Prefix:   &gpb.Path{Target: tgt.target},
			Path:     []*gpb.Path{pathOf(sub.Path)},
			Encoding: gpb.Encoding_JSON_IETF,
		}
		resp, err := client.Get(ctx, req)
		if err != nil {
			// One subtree failing must not cost the others: a device may not
			// implement openconfig-platform and still serve interfaces
			// perfectly.
			if firstErr == nil {
				firstErr = err
			}
			outcome.Misses = append(outcome.Misses, missesFor(sub, models.MissUnsupported)...)
			continue
		}
		for _, n := range resp.GetNotification() {
			a.collect(ep, sub, n, outcome, now)
		}
	}

	outcome.LatencyMs = int(time.Since(started).Milliseconds())
	outcome.Partial = len(outcome.Misses) > 0
	if len(outcome.Samples) == 0 {
		if firstErr != nil {
			return outcome, classify(firstErr)
		}
		return outcome, fmt.Errorf("%w: no leaves decoded from %s",
			models.ErrDecode, ep.Address)
	}
	a.mets.SamplesTotal.WithLabelValues("gnmi").Add(float64(len(outcome.Samples)))
	return outcome, nil
}

func missesFor(sub mapping.GNMISubscription, reason string) []models.Miss {
	var out []models.Miss
	add := func(leaves []mapping.GNMILeaf) {
		for _, l := range leaves {
			out = append(out, models.Miss{Metric: l.Metric, Reason: reason})
		}
	}
	add(sub.Leaves)
	if sub.List != nil {
		add(sub.List.Leaves)
	}
	for _, l := range sub.Lists {
		add(l.Leaves)
	}
	return out
}

func pathOf(p string) *gpb.Path {
	elems := mapping.PathElems(p)
	out := &gpb.Path{Elem: make([]*gpb.PathElem, 0, len(elems))}
	for _, e := range elems {
		out.Elem = append(out.Elem, &gpb.PathElem{Name: e})
	}
	return out
}

// ------------------------------------------------------------- decoding

// collect turns one notification into samples.
func (a *Adapter) collect(ep *models.Endpoint, sub mapping.GNMISubscription,
	n *gpb.Notification, outcome *models.PollOutcome, now int64) {

	// The device's own timestamp, when it sends one. It is the moment the
	// value was true, which is not the moment it arrived - and on a stream
	// those can differ by a whole sample interval.
	observed := now
	if n.GetTimestamp() > 0 {
		observed = n.GetTimestamp() / 1000 // nanoseconds -> microseconds
	}

	for _, upd := range n.GetUpdate() {
		node, ok := decodeValue(upd.GetVal())
		if !ok {
			continue
		}
		// An update's own path may be deeper than the subscription's, in which
		// case the returned node is already inside the subtree.
		a.walk(ep, sub, node, outcome, observed)
	}
}

func (a *Adapter) walk(ep *models.Endpoint, sub mapping.GNMISubscription, node any,
	outcome *models.PollOutcome, observed int64) {

	// A conformant device answers a request for /interfaces with the subtree
	// UNDER it, so the mapping paths are already relative to `node`. A device
	// that returns more than was asked for - the whole document, say, because
	// it did not read the request path - is still perfectly usable, provided
	// the leaves are located rather than assumed. Descending the
	// subscription's own path first costs one map lookup and makes both
	// shapes work.
	if scoped, ok := mapping.Descend(node, sub.Path); ok {
		node = scoped
	}

	for _, leaf := range sub.Leaves {
		if v, ok := mapping.Descend(node, leaf.At); ok {
			if f, ok := toFloat(v, leaf); ok {
				outcome.Samples = append(outcome.Samples,
					a.sample(ep, leaf.Metric, leaf.Instance, leaf.Value(f), observed,
						sub.Path+"/"+leaf.At))
			}
		}
	}

	lists := sub.Lists
	if sub.List != nil {
		lists = append([]mapping.GNMIList{*sub.List}, lists...)
	}
	for _, list := range lists {
		listNode, ok := mapping.Descend(node, list.At)
		if !ok {
			continue
		}
		for _, entry := range mapping.AsList(listNode) {
			key := ""
			if raw, ok := entry[list.Key]; ok {
				key = fmt.Sprint(raw)
			} else if v, ok := mapping.Descend(entry, list.Key); ok {
				key = fmt.Sprint(v)
			}
			for _, leaf := range list.Leaves {
				v, ok := mapping.Descend(entry, leaf.At)
				if !ok {
					continue
				}
				f, ok := toFloat(v, leaf)
				if !ok {
					continue
				}
				metric, instance := leaf.Metric, key
				if leaf.Instance != "" {
					instance = leaf.Instance
				}
				// A per-entry override retargets one member of the list. A CPU
				// component's temperature is the same quantity a server
				// publishes over Redfish, so it belongs on the same key.
				if o, ok := sub.Entries[key]; ok {
					if o.Metric != "" {
						metric = o.Metric
					}
					if o.Instance != "" {
						instance = o.Instance
					}
				}
				outcome.Samples = append(outcome.Samples,
					a.sample(ep, metric, instance, leaf.Value(f), observed,
						sub.Path+"/"+list.At+"["+key+"]/"+leaf.At))
			}
		}
	}
}

// decodeValue turns a TypedValue into a Go tree.
func decodeValue(tv *gpb.TypedValue) (any, bool) {
	if tv == nil {
		return nil, false
	}
	switch v := tv.GetValue().(type) {
	case *gpb.TypedValue_JsonIetfVal:
		var out any
		if err := json.Unmarshal(v.JsonIetfVal, &out); err != nil {
			return nil, false
		}
		return out, true
	case *gpb.TypedValue_JsonVal:
		var out any
		if err := json.Unmarshal(v.JsonVal, &out); err != nil {
			return nil, false
		}
		return out, true
	case *gpb.TypedValue_ProtoBytes:
		// Officially this field carries protobuf-encoded bytes. It is decoded
		// as JSON here because a peer whose own .proto numbers json_ietf_val
		// as 13 - the field the standard assigns to proto_bytes - puts its
		// JSON here, and there is no way to tell from the wire which one
		// wrote it. Attempting a JSON parse is safe: real protobuf bytes do
		// not parse as a JSON object, and anything that does IS JSON.
		var out any
		if err := json.Unmarshal(v.ProtoBytes, &out); err != nil {
			return nil, false
		}
		return out, true
	case *gpb.TypedValue_IntVal:
		return float64(v.IntVal), true
	case *gpb.TypedValue_UintVal:
		return float64(v.UintVal), true
	case *gpb.TypedValue_BoolVal:
		return v.BoolVal, true
	case *gpb.TypedValue_DoubleVal:
		return v.DoubleVal, true
	case *gpb.TypedValue_FloatVal: //nolint:staticcheck // deprecated but still sent
		return float64(v.FloatVal), true
	case *gpb.TypedValue_StringVal:
		return v.StringVal, true
	default:
		return nil, false
	}
}

// toFloat converts a decoded JSON leaf into a number.
//
// The string case is not defensive programming, it is the common case. RFC
// 7951 requires 64-bit integers to be encoded as JSON strings, because a JSON
// number is a double and would lose precision above 2^53 - so every interface
// counter arrives quoted, and a decoder that only accepts numbers silently
// drops all of them while reporting the device healthy.
func toFloat(v any, leaf mapping.GNMILeaf) (float64, bool) {
	switch x := v.(type) {
	case float64:
		return x, true
	case bool:
		if x {
			return 1, true
		}
		return 0, true
	case string:
		if len(leaf.Enum) > 0 {
			if f, ok := leaf.Enum[x]; ok {
				return f, true
			}
			// A symbolic value the mapping does not list. Reporting it as a
			// number would invent a state.
			return 0, false
		}
		s := strings.TrimSpace(x)
		if f, err := strconv.ParseFloat(s, 64); err == nil {
			return f, true
		}
		return 0, false
	default:
		return 0, false
	}
}

func (a *Adapter) sample(ep *models.Endpoint, metric, instance string, value float64,
	observed int64, path string) models.Telemetry {

	def, _ := models.ValidateMetric(metric)
	vt := models.ValueTypeGauge
	var uintValue uint64
	switch def.ValueType {
	case "bool":
		vt = models.ValueTypeBool
	case "counter":
		vt = models.ValueTypeCounter
		if value > 0 {
			uintValue = uint64(value)
		}
	}
	return models.Telemetry{
		EndpointID:     ep.ID,
		DeviceID:       ep.DeviceID,
		Metric:         metric,
		Instance:       instance,
		ValueType:      vt,
		DoubleValue:    value,
		UintValue:      uintValue,
		BoolValue:      value != 0,
		Unit:           def.Unit,
		ObservedAt:     observed,
		CollectedAt:    models.NowMicros(),
		SourceProtocol: models.ProtocolGNMI,
		Quality:        models.QualityGood,
		Metadata:       map[string]string{"path": path},
	}
}
