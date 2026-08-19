package gnmi

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net"
	"sync"
	"testing"
	"time"

	gpb "github.com/openconfig/gnmi/proto/gnmi"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/hari/dcim-platform/collector/internal/health"
	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// fakeTarget is a gNMI server built on the same generated stubs the adapter
// uses, so the test exercises real gRPC and real protobuf rather than a
// hand-rolled stand-in.
//
// It reproduces the two encoding decisions that matter: subtrees are returned
// as JSON_IETF blobs, and 64-bit counters are STRINGS inside them, per RFC
// 7951.
type fakeTarget struct {
	gpb.UnimplementedGNMIServer

	srv *grpc.Server
	ln  net.Listener

	mu          sync.Mutex
	doc         map[string]any
	failPaths   map[string]codes.Code
	getCalls    int
	subCalls    int
	streamEvery time.Duration
	silent      bool
}

func newFakeTarget(t *testing.T) *fakeTarget {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	f := &fakeTarget{
		srv: grpc.NewServer(), ln: ln,
		doc:         defaultDoc(),
		failPaths:   map[string]codes.Code{},
		streamEvery: 40 * time.Millisecond,
	}
	gpb.RegisterGNMIServer(f.srv, f)
	go func() { _ = f.srv.Serve(ln) }()
	t.Cleanup(f.srv.Stop)
	return f
}

func (f *fakeTarget) addrPort() (string, int) {
	a := f.ln.Addr().(*net.TCPAddr)
	return "127.0.0.1", a.Port
}

// defaultDoc mirrors the simulator's OpenConfig document closely enough to
// exercise the mapping: module-qualified top-level keys, a keyed interface
// list, counters as strings, symbolic status and speed.
func defaultDoc() map[string]any {
	iface := func(name string, in, out int64, oper string) map[string]any {
		return map[string]any{
			"name": name,
			"state": map[string]any{
				"name":         name,
				"admin-status": "UP",
				"oper-status":  oper,
				"counters": map[string]any{
					// Strings, not numbers - RFC 7951.
					"in-octets":    fmt.Sprint(in),
					"out-octets":   fmt.Sprint(out),
					"in-errors":    "3",
					"out-errors":   "0",
					"in-discards":  "1",
					"out-discards": "0",
				},
			},
			"ethernet": map[string]any{
				"state": map[string]any{"port-speed": "SPEED_25GB"},
			},
		}
	}
	return map[string]any{
		"openconfig-interfaces:interfaces": map[string]any{
			"interface": []any{
				iface("Ethernet1", 9007199254740993, 42, "UP"),
				iface("Ethernet2", 100, 200, "DOWN"),
			},
		},
		"openconfig-system:system": map[string]any{
			"state": map[string]any{
				// Centiseconds, as the simulator serves and as SNMP's
				// sysUpTime reports.
				"uptime":   360000,
				"hostname": "sw1",
			},
			"memory": map[string]any{
				"state": map[string]any{
					"physical": "34359738368",
					"reserved": "8589934592",
					"free":     "25769803776",
					"utilized": 25.0,
				},
			},
			"cpus": map[string]any{
				"cpu": []any{
					map[string]any{
						"index": "ALL",
						"state": map[string]any{
							"total": map[string]any{"instant": 37.0},
						},
					},
				},
			},
		},
		"openconfig-platform:components": map[string]any{
			"component": []any{
				map[string]any{
					"name": "CHASSIS",
					"state": map[string]any{
						"temperature": map[string]any{"instant": 41.5},
					},
				},
				map[string]any{
					"name": "CPU",
					"state": map[string]any{
						"temperature": map[string]any{"instant": 58.25},
					},
				},
			},
		},
	}
}

func pathString(p *gpb.Path) string {
	out := ""
	for _, e := range p.GetElem() {
		out += "/" + e.GetName()
	}
	if out == "" {
		return "/"
	}
	return out
}

// subtree resolves a gNMI path against the document, stripping module
// prefixes exactly as the simulator does.
func (f *fakeTarget) subtree(p *gpb.Path) (any, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var cur any = f.doc
	for _, e := range p.GetElem() {
		obj, ok := cur.(map[string]any)
		if !ok {
			return nil, false
		}
		found := false
		for k, v := range obj {
			if mapping.LocalName(k) == e.GetName() {
				cur, found = v, true
				break
			}
		}
		if !found {
			return nil, false
		}
	}
	return cur, true
}

func (f *fakeTarget) Get(_ context.Context, req *gpb.GetRequest) (*gpb.GetResponse, error) {
	f.mu.Lock()
	f.getCalls++
	silent := f.silent
	fails := make(map[string]codes.Code, len(f.failPaths))
	for k, v := range f.failPaths {
		fails[k] = v
	}
	f.mu.Unlock()

	if silent {
		return nil, status.Error(codes.Unavailable, "target down")
	}
	var updates []*gpb.Update
	for _, p := range req.GetPath() {
		if code, bad := fails[pathString(p)]; bad {
			return nil, status.Error(code, "path not served")
		}
		node, ok := f.subtree(p)
		if !ok {
			continue
		}
		raw, err := json.Marshal(node)
		if err != nil {
			return nil, status.Error(codes.Internal, err.Error())
		}
		updates = append(updates, &gpb.Update{
			Path: p,
			Val:  &gpb.TypedValue{Value: &gpb.TypedValue_JsonIetfVal{JsonIetfVal: raw}},
		})
	}
	if len(updates) == 0 {
		return &gpb.GetResponse{}, nil
	}
	return &gpb.GetResponse{Notification: []*gpb.Notification{{
		Timestamp: time.Now().UnixNano(),
		Prefix:    &gpb.Path{Target: req.GetPrefix().GetTarget()},
		Update:    updates,
	}}}, nil
}

func (f *fakeTarget) Subscribe(stream gpb.GNMI_SubscribeServer) error {
	req, err := stream.Recv()
	if err != nil {
		return err
	}
	list := req.GetSubscribe()
	if list == nil {
		return status.Error(codes.InvalidArgument, "expected a subscription list")
	}
	f.mu.Lock()
	f.subCalls++
	every := f.streamEvery
	silent := f.silent
	f.mu.Unlock()

	send := func() error {
		for _, sub := range list.GetSubscription() {
			node, ok := f.subtree(sub.GetPath())
			if !ok {
				continue
			}
			raw, err := json.Marshal(node)
			if err != nil {
				return err
			}
			if err := stream.Send(&gpb.SubscribeResponse{
				Response: &gpb.SubscribeResponse_Update{Update: &gpb.Notification{
					Timestamp: time.Now().UnixNano(),
					Prefix:    &gpb.Path{Target: list.GetPrefix().GetTarget()},
					Update: []*gpb.Update{{
						Path: sub.GetPath(),
						Val: &gpb.TypedValue{
							Value: &gpb.TypedValue_JsonIetfVal{JsonIetfVal: raw}},
					}},
				}},
			}); err != nil {
				return err
			}
		}
		return nil
	}

	if silent {
		// Connected and delivering nothing, which is the failure a stream
		// cannot report about itself.
		<-stream.Context().Done()
		return nil
	}

	if err := send(); err != nil {
		return err
	}
	if err := stream.Send(&gpb.SubscribeResponse{
		Response: &gpb.SubscribeResponse_SyncResponse{SyncResponse: true},
	}); err != nil {
		return err
	}
	if list.GetMode() == gpb.SubscriptionList_ONCE {
		return nil
	}

	tick := time.NewTicker(every)
	defer tick.Stop()
	for {
		select {
		case <-stream.Context().Done():
			return nil
		case <-tick.C:
			if err := send(); err != nil {
				return err
			}
		}
	}
}

func (f *fakeTarget) counts() (int, int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.getCalls, f.subCalls
}

// ------------------------------------------------------------- harness

func testLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func loadMaps(t *testing.T) *mapping.GNMIMap {
	t.Helper()
	m, err := mapping.LoadGNMI("../../../../contracts/mappings")
	if err != nil {
		t.Fatalf("load gnmi mappings: %v", err)
	}
	return m
}

func newAdapter(t *testing.T) *Adapter {
	t.Helper()
	pool := NewConnPool(3*time.Second, testLogger())
	a := New(loadMaps(t), pool, testLogger(), obs.NewMetrics())
	t.Cleanup(func() { _ = a.Close(context.Background()) })
	return a
}

func endpointFor(f *fakeTarget) *models.Endpoint {
	host, port := f.addrPort()
	return &models.Endpoint{
		ID: "ep-1", DeviceID: "dev-1", DeviceType: "switch", Protocol: "gnmi",
		Address: host, Port: port, Role: "native_card",
		Addressing: map[string]any{"target": "10.51.21.11", "insecure": true},
		Poll:       models.PollProfile{IntervalS: 30, TimeoutMs: 4000},
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

type captureSink struct {
	mu      sync.Mutex
	samples []models.Telemetry
}

func (c *captureSink) Telemetry(_ context.Context, s []models.Telemetry) error {
	c.mu.Lock()
	c.samples = append(c.samples, s...)
	c.mu.Unlock()
	return nil
}
func (c *captureSink) Events(context.Context, []models.Event) error { return nil }
func (c *captureSink) EndpointState(context.Context, models.EndpointState) error {
	return nil
}
func (c *captureSink) count() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.samples)
}
func (c *captureSink) all() []models.Telemetry {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]models.Telemetry, len(c.samples))
	copy(out, c.samples)
	return out
}

// --------------------------------------------------------------- tests

// RFC 7951 requires 64-bit integers to be JSON STRINGS, because a JSON number
// is a double and loses precision above 2^53. A decoder that only accepts
// numbers silently drops every interface counter while the device looks
// perfectly healthy.
func TestCountersArriveAsStrings(t *testing.T) {
	f := newFakeTarget(t)
	out, err := newAdapter(t).Poll(context.Background(), endpointFor(f))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	got := collect(out)

	s, ok := got["if_in_octets{Ethernet1}"]
	if !ok {
		t.Fatalf("in-octets missing; got %v", keysOf(got))
	}
	// 9007199254740993 is 2^53+1: the smallest integer a float64 cannot hold.
	// It is here to prove the value travelled as a string, not to assert that
	// float64 can represent it - which it cannot, and the registry stores
	// doubles regardless.
	if s.UintValue != 9007199254740992 && s.UintValue != 9007199254740993 {
		t.Fatalf("in-octets uint %d, want ~9007199254740993", s.UintValue)
	}
	if s.ValueType != models.ValueTypeCounter {
		t.Fatalf("value type %v, want counter", s.ValueType)
	}
}

func keysOf(m map[string]models.Telemetry) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

// openconfig reports link state and port speed as NAMES, where SNMP reports
// integers and megabits. Both adapters have to converge on the registry's
// units from different shapes.
func TestSymbolicEnumsAreDecoded(t *testing.T) {
	f := newFakeTarget(t)
	out, err := newAdapter(t).Poll(context.Background(), endpointFor(f))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	got := collect(out)

	if s := got["if_oper_state{Ethernet1}"]; s.DoubleValue != 1 {
		t.Errorf("Ethernet1 oper state %v, want 1", s.DoubleValue)
	}
	if s := got["if_oper_state{Ethernet2}"]; s.DoubleValue != 0 {
		t.Errorf("Ethernet2 oper state %v, want 0", s.DoubleValue)
	}
	if s, ok := got["if_speed{Ethernet1}"]; !ok || s.DoubleValue != 25e9 {
		t.Errorf("speed %v (present=%v), want 25000000000", s.DoubleValue, ok)
	}
}

// The simulator's system/state/uptime carries CENTISECONDS, the same value
// SNMP's sysUpTime returns. Without the scale the two planes disagree by a
// factor of a hundred for the same device.
func TestUptimeIsScaledToSeconds(t *testing.T) {
	f := newFakeTarget(t)
	out, err := newAdapter(t).Poll(context.Background(), endpointFor(f))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	s, ok := collect(out)["sys_uptime"]
	if !ok {
		t.Fatal("sys_uptime missing")
	}
	if math.Abs(s.DoubleValue-3600) > 0.5 {
		t.Fatalf("uptime %v s, want 3600 (360000 centiseconds)", s.DoubleValue)
	}
}

func TestSystemAndCPUAreDecoded(t *testing.T) {
	f := newFakeTarget(t)
	out, err := newAdapter(t).Poll(context.Background(), endpointFor(f))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	got := collect(out)

	if s, ok := got["memory_total"]; !ok || s.DoubleValue != 34359738368 {
		t.Errorf("memory_total %v (present=%v)", s.DoubleValue, ok)
	}
	if s, ok := got["memory_utilization"]; !ok || s.DoubleValue != 25 {
		t.Errorf("memory_utilization %v (present=%v)", s.DoubleValue, ok)
	}
	// The CPU list is keyed by index, so a multi-CPU device keeps them apart.
	if s, ok := got["cpu_utilization{ALL}"]; !ok || s.DoubleValue != 37 {
		t.Errorf("cpu_utilization %v (present=%v)", s.DoubleValue, ok)
	}
}

// A CPU component's temperature is the same physical quantity a server
// publishes over Redfish, so it lands on the same key; anything else keeps its
// component name.
func TestComponentTemperatureOverride(t *testing.T) {
	f := newFakeTarget(t)
	out, err := newAdapter(t).Poll(context.Background(), endpointFor(f))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	got := collect(out)

	if s, ok := got["cpu_temperature{CPU}"]; !ok || math.Abs(s.DoubleValue-58.25) > 0.01 {
		t.Errorf("cpu temperature %v (present=%v), want 58.25", s.DoubleValue, ok)
	}
	if s, ok := got["component_temperature{CHASSIS}"]; !ok ||
		math.Abs(s.DoubleValue-41.5) > 0.01 {
		t.Errorf("chassis temperature %v (present=%v), want 41.5", s.DoubleValue, ok)
	}
	if _, wrong := got["component_temperature{CPU}"]; wrong {
		t.Error("the CPU component was not retargeted")
	}
}

// A device that does not implement openconfig-platform must still yield its
// interfaces. One unserved subtree is not an outage.
func TestOneUnservedSubtreeKeepsTheRest(t *testing.T) {
	f := newFakeTarget(t)
	f.mu.Lock()
	f.failPaths["/components"] = codes.Unimplemented
	f.mu.Unlock()

	out, err := newAdapter(t).Poll(context.Background(), endpointFor(f))
	if err != nil {
		t.Fatalf("poll failed because one subtree is unimplemented: %v", err)
	}
	if !out.Partial {
		t.Error("outcome not marked partial")
	}
	got := collect(out)
	if _, ok := got["if_in_octets{Ethernet1}"]; !ok {
		t.Error("interfaces were lost with the unimplemented subtree")
	}
	if _, ok := got["cpu_temperature{CPU}"]; ok {
		t.Error("the unimplemented subtree produced samples")
	}
}

// A wrong credential must never be retried like a timeout.
func TestUnauthenticatedIsAuth(t *testing.T) {
	f := newFakeTarget(t)
	f.mu.Lock()
	f.failPaths["/interfaces"] = codes.Unauthenticated
	f.failPaths["/system"] = codes.Unauthenticated
	f.failPaths["/components"] = codes.Unauthenticated
	f.mu.Unlock()

	_, err := newAdapter(t).Poll(context.Background(), endpointFor(f))
	if err == nil {
		t.Fatal("expected an error")
	}
	if class := models.ClassifyError(err); class != models.ErrClassAuth {
		t.Fatalf("error class %q, want auth", class)
	}
}

func TestUnavailableIsUnreachable(t *testing.T) {
	f := newFakeTarget(t)
	f.mu.Lock()
	f.silent = true
	f.mu.Unlock()

	_, err := newAdapter(t).Poll(context.Background(), endpointFor(f))
	if err == nil {
		t.Fatal("expected an error")
	}
	if class := models.ClassifyError(err); class != models.ErrClassUnreachable {
		t.Fatalf("error class %q, want unreachable", class)
	}
}

// One gRPC connection carries every RPC to a device, including a Subscribe
// stream that stays open for days.
func TestConnectionIsReusedAcrossPolls(t *testing.T) {
	f := newFakeTarget(t)
	a := newAdapter(t)
	ep := endpointFor(f)
	for i := 0; i < 3; i++ {
		if _, err := a.Poll(context.Background(), ep); err != nil {
			t.Fatalf("poll %d: %v", i, err)
		}
	}
	if n := a.conns.Connections(); n != 1 {
		t.Fatalf("%d connections for one device across three polls, want 1", n)
	}
}

// The notification's own timestamp is when the value was TRUE. On a stream
// that can be a whole sample interval before it arrived.
func TestDeviceTimestampIsPreferred(t *testing.T) {
	f := newFakeTarget(t)
	out, err := newAdapter(t).Poll(context.Background(), endpointFor(f))
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	now := models.NowMicros()
	for _, s := range out.Samples {
		if s.ObservedAt == 0 {
			t.Fatal("a sample carries no observation time")
		}
		if s.ObservedAt > now+1_000_000 {
			t.Fatalf("observed_at %d is in the future", s.ObservedAt)
		}
	}
}

// ------------------------------------------------------------- streaming

func streamEndpoint(f *fakeTarget) *models.Endpoint {
	ep := endpointFor(f)
	// The gnmi-stream poll profile: no interval, push enabled.
	ep.Poll = models.PollProfile{IntervalS: 0, TimeoutMs: 5000, PushEnabled: true}
	return ep
}

func TestStreamOnlyRecognisesThePushProfile(t *testing.T) {
	f := newFakeTarget(t)
	if !StreamOnly(streamEndpoint(f)) {
		t.Error("a zero-interval push endpoint should stream")
	}
	if StreamOnly(endpointFor(f)) {
		t.Error("a 30 s polled endpoint should not stream")
	}
	ep := streamEndpoint(f)
	ep.Protocol = "snmp"
	if StreamOnly(ep) {
		t.Error("only gnmi endpoints stream here")
	}
}

func newSubscriber(t *testing.T, a *Adapter, sink models.Sink) *Subscriber {
	t.Helper()
	tracker := health.NewTracker(3, "col-test", sink, testLogger(), obs.NewMetrics())
	return NewSubscriber(a, a.conns, loadMaps(t), sink, tracker, testLogger(),
		obs.NewMetrics(), 3)
}

func TestStreamDeliversWithoutPolling(t *testing.T) {
	f := newFakeTarget(t)
	a := newAdapter(t)
	sink := &captureSink{}
	sub := newSubscriber(t, a, sink)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	sub.Manage(ctx, []*models.Endpoint{streamEndpoint(f)})
	defer sub.Stop()

	deadline := time.Now().Add(5 * time.Second)
	for sink.count() == 0 && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if sink.count() == 0 {
		t.Fatal("no samples arrived on the stream")
	}
	// Nothing was polled: the device saw a Subscribe and no Get.
	gets, subs := f.counts()
	if gets != 0 {
		t.Errorf("%d Get calls on a streamed endpoint", gets)
	}
	if subs != 1 {
		t.Errorf("%d Subscribe calls, want 1", subs)
	}

	found := false
	for _, s := range sink.all() {
		if s.Metric == "if_in_octets" && s.Instance == "Ethernet1" {
			found = true
		}
	}
	if !found {
		t.Error("interface counters did not arrive on the stream")
	}
}

// A stream that is up and silent looks exactly like one that has died, so
// silence past the grace window has to fail the session rather than leave an
// endpoint reporting healthy while delivering nothing.
func TestSilentStreamIsAFailure(t *testing.T) {
	f := newFakeTarget(t)
	f.mu.Lock()
	f.silent = true
	f.mu.Unlock()

	a := newAdapter(t)
	sink := &captureSink{}
	sub := newSubscriber(t, a, sink)
	// A short window so the test does not wait out the interval-derived one.
	sub.SetGraceWindow(300 * time.Millisecond)
	sub.minBackoff = 20 * time.Millisecond

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	ep := streamEndpoint(f)
	done := make(chan error, 1)
	go func() { done <- sub.subscribe(ctx, ep) }()

	select {
	case err := <-done:
		if err == nil {
			t.Fatal("a silent stream returned no error")
		}
	case <-time.After(10 * time.Second):
		t.Fatal("a silent stream was never failed")
	}
}

func TestManageStopsRemovedSessions(t *testing.T) {
	f := newFakeTarget(t)
	a := newAdapter(t)
	sink := &captureSink{}
	sub := newSubscriber(t, a, sink)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	ep := streamEndpoint(f)
	sub.Manage(ctx, []*models.Endpoint{ep})
	if n := sub.Sessions(); n != 1 {
		t.Fatalf("%d sessions after adding one", n)
	}
	// Re-managing with the same endpoint must not start a second session.
	sub.Manage(ctx, []*models.Endpoint{ep})
	if n := sub.Sessions(); n != 1 {
		t.Fatalf("%d sessions after re-managing the same endpoint", n)
	}
	sub.Manage(ctx, nil)
	if n := sub.Sessions(); n != 0 {
		t.Fatalf("%d sessions after removing it", n)
	}
	sub.Stop()
}

func TestPathElemsAndLocalNames(t *testing.T) {
	if got := mapping.PathElems("/interfaces"); len(got) != 1 || got[0] != "interfaces" {
		t.Fatalf("got %v", got)
	}
	if got := mapping.LocalName("openconfig-interfaces:interfaces"); got != "interfaces" {
		t.Fatalf("got %q", got)
	}
	if got := mapping.LocalName("interfaces"); got != "interfaces" {
		t.Fatalf("got %q", got)
	}
}

// A single-entry list encoded as a bare object is legal and catches out
// anything that type-asserts to a slice.
func TestSingleEntryListIsAccepted(t *testing.T) {
	node := map[string]any{"interface": map[string]any{"name": "Ethernet1"}}
	list, ok := mapping.Descend(node, "interface")
	if !ok {
		t.Fatal("descend failed")
	}
	entries := mapping.AsList(list)
	if len(entries) != 1 || entries[0]["name"] != "Ethernet1" {
		t.Fatalf("got %v", entries)
	}
}

// A peer that returns the WHOLE document rather than the requested subtree is
// still usable, provided the leaves are located rather than assumed. The
// simulator does exactly this, because its own .proto numbers GetRequest.path
// as field 3 where the standard says 2, so it never sees the path at all.
func TestWholeDocumentResponseIsStillDecoded(t *testing.T) {
	a := newAdapter(t)
	ep := &models.Endpoint{ID: "ep-doc", DeviceID: "dev-1", Protocol: "gnmi"}
	out := &models.PollOutcome{}

	var sub mapping.GNMISubscription
	for _, s := range a.maps.Subscriptions {
		if s.Name == "interfaces" {
			sub = s
		}
	}
	// The document root, not the /interfaces subtree.
	a.walk(ep, sub, defaultDoc(), out, models.NowMicros())

	got := collect(out)
	if s, ok := got["if_in_octets{Ethernet1}"]; !ok || s.UintValue == 0 {
		t.Fatalf("counters not found in a whole-document response: %v", keysOf(got))
	}
	if s, ok := got["if_speed{Ethernet2}"]; !ok || s.DoubleValue != 25e9 {
		t.Errorf("speed %v (present=%v)", s.DoubleValue, ok)
	}
}

// The scoped shape - what a conformant device returns - must keep working.
func TestScopedSubtreeResponseIsStillDecoded(t *testing.T) {
	a := newAdapter(t)
	ep := &models.Endpoint{ID: "ep-scoped", DeviceID: "dev-1", Protocol: "gnmi"}
	out := &models.PollOutcome{}

	var sub mapping.GNMISubscription
	for _, s := range a.maps.Subscriptions {
		if s.Name == "interfaces" {
			sub = s
		}
	}
	doc := defaultDoc()
	subtree := doc["openconfig-interfaces:interfaces"]
	a.walk(ep, sub, subtree, out, models.NowMicros())

	if _, ok := collect(out)["if_in_octets{Ethernet1}"]; !ok {
		t.Fatal("counters not found in a scoped subtree response")
	}
}

// A peer whose .proto numbers json_ietf_val as 13 puts its JSON in the field
// the standard assigns to proto_bytes. Attempting a JSON parse there is safe:
// real protobuf bytes do not parse as a JSON object.
func TestJSONInProtoBytesIsAccepted(t *testing.T) {
	raw := []byte(`{"interface":[{"name":"Ethernet9"}]}`)
	node, ok := decodeValue(&gpb.TypedValue{
		Value: &gpb.TypedValue_ProtoBytes{ProtoBytes: raw}})
	if !ok {
		t.Fatal("JSON in proto_bytes was rejected")
	}
	list, ok := mapping.Descend(node, "interface")
	if !ok || len(mapping.AsList(list)) != 1 {
		t.Fatalf("decoded tree is wrong: %v", node)
	}

	// Genuine protobuf bytes must NOT be mistaken for JSON.
	if _, ok := decodeValue(&gpb.TypedValue{
		Value: &gpb.TypedValue_ProtoBytes{ProtoBytes: []byte{0x08, 0x96, 0x01}}}); ok {
		t.Error("binary protobuf bytes were decoded as JSON")
	}
}
