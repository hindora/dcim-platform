// Package integration runs the adapters against a live device plane.
//
// These tests assert the CONTRACT, not values. A chiller's supply temperature
// moves, an interface counter only goes up, and a test that pins either is a
// test that fails for the wrong reason every afternoon. What they pin is what
// must not change: that a metric exists, that it is in the registry, that its
// unit is the registry's unit, that a value is physically possible, and that
// identity - which device, which port, which loop - is what it claims to be.
//
// They are skipped unless DCIM_INTEGRATION_SIM names a simulator, so `go test
// ./...` stays hermetic:
//
//	DCIM_INTEGRATION_SIM=http://127.0.0.1:8001 \
//	DCIM_INTEGRATION_USER=admin DCIM_INTEGRATION_PASS=admin1234 \
//	  go test ./internal/integration/ -v
//
// Some tests MUTATE the simulator: pointing its trap receiver at a listener
// the test owns, creating a Redfish subscription, firing a test event. Every
// one of them restores what it changed, and each says so at the top.
package integration

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// Sim is a live simulator: its API, and the topology it is running.
type Sim struct {
	BaseURL string
	token   string
	client  *http.Client

	once    sync.Once
	devices []Device
	loadErr error
}

// Device is one node of the topology export, with the fields the adapters
// need to address it. The field names mirror the export rather than the
// platform's own model, because this is the plane's vocabulary.
type Device struct {
	ID         string `json:"id"`
	Name       string `json:"name"`
	DeviceType string `json:"device_type"`
	IPAddress  string `json:"ip_address"`
	MgmtIP     string `json:"mgmt_ip"`
	Model      string `json:"model_name"`
	SNMPPort   int    `json:"snmp_port"`

	MSTPRouterIP string `json:"mstp_router_ip"`
	MSTPNet      int    `json:"mstp_net"`
	MSTPMac      int    `json:"mstp_mac"`

	ModbusRole      string `json:"modbus_role"`
	ModbusUnitID    int    `json:"modbus_unit_id"`
	ModbusGatewayIP string `json:"modbus_gateway_ip"`
}

var (
	simOnce sync.Once
	simInst *Sim
	simErr  error
)

// RequireSimulator returns the live plane, skipping the test when there is
// none. One login and one topology fetch are shared by every test in the
// package: the export is large and fetching it per test would dominate the
// run.
func RequireSimulator(t *testing.T) *Sim {
	t.Helper()
	simOnce.Do(func() {
		base := os.Getenv("DCIM_INTEGRATION_SIM")
		if base == "" {
			return
		}
		user := envOr("DCIM_INTEGRATION_USER", "admin")
		pass := envOr("DCIM_INTEGRATION_PASS", "admin1234")
		s := &Sim{
			BaseURL: strings.TrimRight(base, "/"),
			client:  &http.Client{Timeout: 90 * time.Second},
		}
		if err := s.login(user, pass); err != nil {
			simErr = err
			return
		}
		simInst = s
	})
	if simInst == nil {
		if simErr != nil {
			t.Fatalf("simulator at %s is configured but unusable: %v",
				os.Getenv("DCIM_INTEGRATION_SIM"), simErr)
		}
		t.Skip("set DCIM_INTEGRATION_SIM to run integration tests against a live plane")
	}
	return simInst
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func (s *Sim) login(user, pass string) error {
	body, _ := json.Marshal(map[string]string{"username": user, "password": pass})
	resp, err := s.client.Post(s.BaseURL+"/api/auth/login", "application/json",
		bytes.NewReader(body))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("login returned %d", resp.StatusCode)
	}
	var out struct {
		Token string `json:"token"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return err
	}
	if out.Token == "" {
		return fmt.Errorf("login returned no token")
	}
	s.token = out.Token
	return nil
}

// Get calls the simulator API and decodes the result.
func (s *Sim) Get(t *testing.T, path string, into any) {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, s.BaseURL+path, nil)
	if err != nil {
		t.Fatalf("build request: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+s.token)
	resp, err := s.client.Do(req)
	if err != nil {
		t.Fatalf("GET %s: %v", path, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		t.Fatalf("GET %s returned %d: %s", path, resp.StatusCode, raw)
	}
	if into != nil {
		if err := json.NewDecoder(resp.Body).Decode(into); err != nil {
			t.Fatalf("decode %s: %v", path, err)
		}
	}
}

// Post calls the simulator API. It returns the status so a test can assert on
// a rejection rather than only on success.
func (s *Sim) Post(t *testing.T, path string, payload any, into any) int {
	t.Helper()
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("encode payload: %v", err)
	}
	req, err := http.NewRequest(http.MethodPost, s.BaseURL+path, bytes.NewReader(body))
	if err != nil {
		t.Fatalf("build request: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+s.token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := s.client.Do(req)
	if err != nil {
		t.Fatalf("POST %s: %v", path, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if into != nil && resp.StatusCode < 300 {
		if err := json.Unmarshal(raw, into); err != nil {
			t.Fatalf("decode %s: %v (body %s)", path, err, raw)
		}
	}
	return resp.StatusCode
}

// Devices returns the topology, fetched once.
func (s *Sim) Devices(t *testing.T) []Device {
	t.Helper()
	s.once.Do(func() {
		var doc struct {
			Nodes []struct {
				Device Device `json:"device"`
			} `json:"nodes"`
		}
		req, err := http.NewRequest(http.MethodGet,
			s.BaseURL+"/api/topology/export", nil)
		if err != nil {
			s.loadErr = err
			return
		}
		req.Header.Set("Authorization", "Bearer "+s.token)
		resp, err := s.client.Do(req)
		if err != nil {
			s.loadErr = err
			return
		}
		defer resp.Body.Close()
		if resp.StatusCode >= 300 {
			s.loadErr = fmt.Errorf("topology export returned %d", resp.StatusCode)
			return
		}
		if err := json.NewDecoder(resp.Body).Decode(&doc); err != nil {
			s.loadErr = err
			return
		}
		for _, n := range doc.Nodes {
			s.devices = append(s.devices, n.Device)
		}
	})
	if s.loadErr != nil {
		t.Fatalf("load topology: %v", s.loadErr)
	}
	if len(s.devices) == 0 {
		t.Fatal("the topology export is empty - is a topology loaded?")
	}
	return s.devices
}

// DeviceOfType returns the first device of a type, skipping when the plane has
// none. A plane without chillers is a smaller plane, not a failing one.
func (s *Sim) DeviceOfType(t *testing.T, deviceType string) Device {
	t.Helper()
	for _, d := range s.Devices(t) {
		if d.DeviceType == deviceType {
			return d
		}
	}
	t.Skipf("no %s in this topology", deviceType)
	return Device{}
}

// DevicesOfType returns every device of a type.
func (s *Sim) DevicesOfType(t *testing.T, deviceType string) []Device {
	t.Helper()
	var out []Device
	for _, d := range s.Devices(t) {
		if d.DeviceType == deviceType {
			out = append(out, d)
		}
	}
	return out
}

// ------------------------------------------------------------- endpoints
//
// These mirror backend/app/importer/endpoints.py. They are duplicated rather
// than shared because that code is Python and this is the collector's own
// test; the risk is that they drift, so each carries the rule it implements
// and the live tests fail loudly when the plane stops matching.

// SNMPEndpoint builds an endpoint for a device's SNMP agent.
//
// role "os_agent" is the OS agent on the device's own address; "bmc" is the
// service processor on the management address. A server has BOTH, and they are
// genuinely different agents answering different OIDs.
func (s *Sim) SNMPEndpoint(t *testing.T, d Device, role string) *models.Endpoint {
	t.Helper()
	addr := d.MgmtIP
	if role == "os_agent" {
		if d.DeviceType == "server" && d.IPAddress != "" {
			addr = d.IPAddress
		} else if d.MgmtIP == "" {
			addr = d.IPAddress
		}
	}
	if addr == "" {
		t.Skipf("%s has no address for an SNMP %s endpoint", d.Name, role)
	}
	port := d.SNMPPort
	if port == 0 {
		port = 161
	}
	return &models.Endpoint{
		ID: "itest-snmp-" + role + "-" + addr, DeviceID: d.ID, DeviceType: d.DeviceType,
		Protocol: "snmp", Address: addr, Port: port, Role: role,
		// The community IS the address on this plane. A wrong one is not
		// rejected - the agent simply does not answer, which is why a
		// credential mistake here looks exactly like a dead device.
		Credential: &models.Credential{
			Kind: "snmp_v2c",
			Data: map[string]any{"community": addr},
		},
		Poll: models.PollProfile{IntervalS: 30, TimeoutMs: 6000},
	}
}

// RedfishEndpoint builds an endpoint for a server's BMC.
func (s *Sim) RedfishEndpoint(t *testing.T, d Device) *models.Endpoint {
	t.Helper()
	if d.MgmtIP == "" {
		t.Skipf("%s has no management address", d.Name)
	}
	return &models.Endpoint{
		ID: "itest-redfish-" + d.MgmtIP, DeviceID: d.ID, DeviceType: d.DeviceType,
		Protocol: "redfish", Address: d.MgmtIP, Port: 8443, Role: "bmc",
		// Plain HTTP on 8443 despite the port - see docs/16.
		Addressing: map[string]any{"base": "/redfish/v1", "scheme": "http",
			"verify_tls": false},
		Credential: &models.Credential{
			Kind: "http_basic",
			Data: map[string]any{"username": "admin", "password": "password"},
		},
		Poll: models.PollProfile{IntervalS: 60, TimeoutMs: 8000},
	}
}

// GNMIEndpoint builds an endpoint for a fabric device.
func (s *Sim) GNMIEndpoint(t *testing.T, d Device) *models.Endpoint {
	t.Helper()
	addr := d.MgmtIP
	if addr == "" {
		addr = d.IPAddress
	}
	if addr == "" {
		t.Skipf("%s has no address", d.Name)
	}
	return &models.Endpoint{
		ID: "itest-gnmi-" + addr, DeviceID: d.ID, DeviceType: d.DeviceType,
		Protocol: "gnmi", Address: addr, Port: 50051, Role: "native_card",
		Addressing: map[string]any{"target": addr, "insecure": true},
		Poll:       models.PollProfile{IntervalS: 30, TimeoutMs: 8000},
	}
}

// BACnetEndpoint builds an endpoint for plant. A device on an MS/TP trunk has
// no address of its own: the router's carries the packet and (network, MAC)
// says which device on the trunk answers.
func (s *Sim) BACnetEndpoint(t *testing.T, d Device) *models.Endpoint {
	t.Helper()
	ep := &models.Endpoint{
		DeviceID: d.ID, DeviceType: d.DeviceType, Protocol: "bacnet",
		Port: 47808, Role: "native_card",
		// No device instance: the adapter finds it with a directed Who-Is,
		// which is how a real commissioning run works.
		Addressing: map[string]any{},
		Poll:       models.PollProfile{IntervalS: 30, TimeoutMs: 6000},
	}
	if d.MSTPRouterIP != "" {
		ep.Address = d.MSTPRouterIP
		ep.Role = "field_device"
		ep.Addressing["network"] = d.MSTPNet
		ep.Addressing["mac"] = d.MSTPMac
	} else {
		ep.Address = d.MgmtIP
		if ep.Address == "" {
			ep.Address = d.IPAddress
		}
	}
	if ep.Address == "" {
		t.Skipf("%s has no reachable BACnet address", d.Name)
	}
	ep.ID = "itest-bacnet-" + ep.Address + fmt.Sprint(d.MSTPMac)
	return ep
}

// ModbusEndpoint builds an endpoint for electrical gear or a field
// transmitter. A transmitter also needs its probe role: the instrument
// publishes one nameless process value and where it is installed is what says
// what it measures.
func (s *Sim) ModbusEndpoint(t *testing.T, d Device) *models.Endpoint {
	t.Helper()
	ep := &models.Endpoint{
		DeviceID: d.ID, DeviceType: d.DeviceType, Protocol: "modbus",
		Port: 502, Role: "native_card",
		Addressing: map[string]any{"unit_id": 1},
		Poll:       models.PollProfile{IntervalS: 30, TimeoutMs: 5000},
	}
	if d.ModbusRole == "rtu_slave" && d.ModbusGatewayIP != "" {
		ep.Address = d.ModbusGatewayIP
		ep.Role = "field_device"
		ep.Addressing["unit_id"] = d.ModbusUnitID
		if role := ProbeRole(d.Name); role != "" {
			ep.Addressing["probe_role"] = role
		}
	} else {
		ep.Address = d.MgmtIP
		if ep.Address == "" {
			ep.Address = d.IPAddress
		}
	}
	if ep.Address == "" {
		t.Skipf("%s has no reachable Modbus address", d.Name)
	}
	ep.ID = fmt.Sprintf("itest-modbus-%s-%d", ep.Address, d.ModbusUnitID)
	return ep
}

// ProbeRole derives the plant point a transmitter measures from its tag, the
// same way the plane does. The instrument's identity is in its name, exactly
// as it is on a real drawing.
func ProbeRole(name string) string {
	prefix, _, _ := strings.Cut(name, "-")
	switch strings.ToUpper(prefix) {
	case "CHWS":
		return "chw_supply"
	case "CHWR":
		return "chw_return"
	case "CWS":
		return "cw_supply"
	case "CWR":
		return "cw_return"
	case "CTB":
		return "ct_basin"
	case "FLOW":
		return "chw_flow"
	default:
		return ""
	}
}

// ------------------------------------------------------------ assertions

func TestLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func TestMetrics() *obs.Metrics { return obs.NewMetrics() }

// Sample finds one sample by metric and instance.
func Sample(out *models.PollOutcome, metric, instance string) (models.Telemetry, bool) {
	for _, s := range out.Samples {
		if s.Metric == metric && s.Instance == instance {
			return s, true
		}
	}
	return models.Telemetry{}, false
}

// AssertHasMetric requires a metric to be present, in range, and carrying the
// registry's unit.
//
// The unit check is the one that catches real mistakes. A value can look
// entirely plausible and still be a hundred or a thousand times wrong - kW
// where watts were meant, centiseconds where seconds were - and the only
// thing that notices is the contract saying which one this key carries.
func AssertHasMetric(t *testing.T, out *models.PollOutcome, metric string,
	lo, hi float64) models.Telemetry {
	t.Helper()
	def, known := models.ValidateMetric(metric)
	if !known {
		t.Fatalf("%s is not in the registry", metric)
	}
	for _, s := range out.Samples {
		if s.Metric != metric {
			continue
		}
		if s.Unit != def.Unit {
			t.Errorf("%s carries unit %q, the registry says %q",
				metric, s.Unit, def.Unit)
		}
		if s.DoubleValue < lo || s.DoubleValue > hi {
			t.Errorf("%s = %v, outside the plausible range [%v, %v]",
				metric, s.DoubleValue, lo, hi)
		}
		return s
	}
	t.Fatalf("%s not collected; got %v", metric, MetricNames(out))
	return models.Telemetry{}
}

// AssertInstances requires at least n distinct instances of a metric, which is
// how a per-port or per-phase quantity proves it is actually per-something.
func AssertInstances(t *testing.T, out *models.PollOutcome, metric string, n int) []string {
	t.Helper()
	seen := map[string]bool{}
	for _, s := range out.Samples {
		if s.Metric == metric {
			seen[s.Instance] = true
		}
	}
	if len(seen) < n {
		t.Fatalf("%s has %d instances, want at least %d", metric, len(seen), n)
	}
	out2 := make([]string, 0, len(seen))
	for k := range seen {
		out2 = append(out2, k)
	}
	return out2
}

// AssertRegistryContract is the check every adapter must pass: nothing is
// emitted that the registry does not define, every unit matches, and no value
// is outside the bounds the registry declares.
//
// A decoder that reads the wrong field, the wrong word order or the wrong
// scale still returns numbers. This is what tells the difference.
func AssertRegistryContract(t *testing.T, out *models.PollOutcome) {
	t.Helper()
	if len(out.Samples) == 0 {
		t.Fatal("no samples at all")
	}
	for _, s := range out.Samples {
		def, known := models.ValidateMetric(s.Metric)
		if !known {
			t.Errorf("%s is not in the registry", s.Metric)
			continue
		}
		if s.Unit != def.Unit {
			t.Errorf("%s{%s} carries unit %q, the registry says %q",
				s.Metric, s.Instance, s.Unit, def.Unit)
		}
		if s.ValueType == models.ValueTypeText {
			continue
		}
		if def.HasMin && s.DoubleValue < def.MinValid {
			t.Errorf("%s{%s} = %v, below the registry minimum %v",
				s.Metric, s.Instance, s.DoubleValue, def.MinValid)
		}
		if def.HasMax && s.DoubleValue > def.MaxValid {
			t.Errorf("%s{%s} = %v, above the registry maximum %v",
				s.Metric, s.Instance, s.DoubleValue, def.MaxValid)
		}
		if s.ObservedAt == 0 {
			t.Errorf("%s{%s} carries no observation time", s.Metric, s.Instance)
		}
		if s.DeviceID == "" {
			t.Errorf("%s{%s} is not attributed to a device", s.Metric, s.Instance)
		}
	}
}

// AssertNoRawIdentifiers catches a metric name that is really an OID, a
// register address or a path - the sign that a mapping fell through to
// whatever the protocol called it.
func AssertNoRawIdentifiers(t *testing.T, out *models.PollOutcome) {
	t.Helper()
	for _, s := range out.Samples {
		switch {
		case strings.HasPrefix(s.Metric, "1.3.6"):
			t.Errorf("metric %q is a raw OID", s.Metric)
		case strings.HasPrefix(s.Metric, "0x"), strings.HasPrefix(s.Metric, "/"):
			t.Errorf("metric %q is a raw protocol identifier", s.Metric)
		case strings.ContainsAny(s.Metric, " :"):
			t.Errorf("metric %q is not a canonical key", s.Metric)
		}
	}
}

// MetricNames lists what was collected, for a failure message that says what
// WAS there rather than only what was missing.
func MetricNames(out *models.PollOutcome) []string {
	seen := map[string]bool{}
	var names []string
	for _, s := range out.Samples {
		if !seen[s.Metric] {
			seen[s.Metric] = true
			names = append(names, s.Metric)
		}
	}
	return names
}

// Ctx is a context with a timeout suited to a live device.
func Ctx(t *testing.T, d time.Duration) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), d)
	t.Cleanup(cancel)
	return ctx
}
