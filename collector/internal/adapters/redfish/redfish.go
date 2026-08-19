// Package redfish polls server BMCs.
//
// Three decisions carry most of the value here:
//
//  1. DISCOVER ONCE. The service root, Systems and Chassis collections are
//     walked on the first poll and the resolved URLs cached. Afterwards a cycle
//     is three GETs - Thermal, Power, the system - instead of a crawl. At a few
//     hundred BMCs that difference dominates the collector's cost.
//
//  2. SESSION AUTH, NOT BASIC-PER-REQUEST. One POST to SessionService yields a
//     token reused until it is rejected. Real BMCs are slow to authenticate and
//     some firmware rate-limits repeated basic auth.
//
//  3. REUSE CONNECTIONS. Without keepalive every poll pays a TLS handshake per
//     BMC, which at 310 servers is the single largest cost in the process.
package redfish

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

type Adapter struct {
	maps *mapping.RedfishMap
	log  *slog.Logger
	mets *obs.Metrics

	mu       sync.Mutex
	sessions map[string]*session // endpoint id -> auth + discovered URLs
	clients  map[bool]*http.Client
}

type session struct {
	token      string
	sessionURI string
	thermal    string
	power      string
	system     string
	base       string
	verifyTLS  bool
}

func New(maps *mapping.RedfishMap, log *slog.Logger, mets *obs.Metrics) *Adapter {
	return &Adapter{
		maps: maps, log: log, mets: mets,
		sessions: make(map[string]*session),
		clients:  make(map[bool]*http.Client),
	}
}

func (a *Adapter) Protocol() string              { return "redfish" }
func (a *Adapter) Init(_ context.Context) error  { return nil }
func (a *Adapter) Close(_ context.Context) error { return nil }

func (a *Adapter) Forget(endpointID string) {
	a.mu.Lock()
	delete(a.sessions, endpointID)
	a.mu.Unlock()
}

// client returns a pooled client. Verified and unverified TLS get separate
// clients so a lab BMC with a self-signed certificate cannot weaken the
// transport used for a real one.
func (a *Adapter) client(verify bool, timeout time.Duration) *http.Client {
	a.mu.Lock()
	defer a.mu.Unlock()
	if c, ok := a.clients[verify]; ok {
		return c
	}
	c := &http.Client{
		Timeout: timeout,
		Transport: &http.Transport{
			MaxIdleConns:        256,
			MaxIdleConnsPerHost: 2,
			IdleConnTimeout:     90 * time.Second,
			TLSClientConfig:     &tls.Config{InsecureSkipVerify: !verify}, //nolint:gosec // per-endpoint, see addressing.verify_tls
			TLSHandshakeTimeout: 10 * time.Second,
		},
	}
	a.clients[verify] = c
	return c
}

func (a *Adapter) Poll(ctx context.Context, ep *models.Endpoint) (*models.PollOutcome, error) {
	started := time.Now()
	s, err := a.ensureSession(ctx, ep)
	if err != nil {
		return nil, err
	}

	outcome := &models.PollOutcome{}
	now := models.NowMicros()

	// An auth failure ends the poll immediately rather than being folded into
	// the misses. Reporting it as "no metrics" would classify a rejected
	// credential as a decode fault, and the health tracker would then retry it
	// like a timeout - which on real hardware locks accounts.
	for _, res := range []struct {
		name  string
		url   string
		parse func(map[string]any, *models.Endpoint, *models.PollOutcome, int64)
	}{
		{"thermal", s.thermal, a.parseThermal},
		{"power", s.power, a.parsePower},
	} {
		if res.url == "" {
			continue
		}
		body, err := a.get(ctx, ep, s, res.url)
		if err != nil {
			if models.ClassifyError(err) == models.ErrClassAuth {
				return outcome, err
			}
			outcome.Misses = append(outcome.Misses,
				models.Miss{Metric: res.name, Reason: missReason(err)})
			continue
		}
		res.parse(body, ep, outcome, now)
	}

	outcome.LatencyMs = int(time.Since(started).Milliseconds())
	outcome.Partial = len(outcome.Misses) > 0
	if len(outcome.Samples) == 0 {
		return outcome, fmt.Errorf("%w: no metrics from %s", models.ErrDecode, ep.Address)
	}
	a.mets.SamplesTotal.WithLabelValues("redfish").Add(float64(len(outcome.Samples)))
	return outcome, nil
}

// ------------------------------------------------------------- session

func (a *Adapter) ensureSession(ctx context.Context, ep *models.Endpoint) (*session, error) {
	a.mu.Lock()
	s, ok := a.sessions[ep.ID]
	a.mu.Unlock()
	if ok && s.token != "" && s.thermal != "" {
		return s, nil
	}

	base := "/redfish/v1"
	verify := true
	if v, ok := ep.Addressing["base"].(string); ok && v != "" {
		base = v
	}
	if v, ok := ep.Addressing["verify_tls"].(bool); ok {
		verify = v
	}
	s = &session{base: base, verifyTLS: verify}

	if err := a.authenticate(ctx, ep, s); err != nil {
		return nil, err
	}
	if err := a.discover(ctx, ep, s); err != nil {
		return nil, err
	}

	a.mu.Lock()
	a.sessions[ep.ID] = s
	a.mu.Unlock()
	return s, nil
}

func (a *Adapter) authenticate(ctx context.Context, ep *models.Endpoint, s *session) error {
	user, pass := credentials(ep)
	if user == "" {
		return fmt.Errorf("%w: no credential for %s", models.ErrAuth, ep.ID)
	}
	payload, _ := json.Marshal(map[string]string{"UserName": user, "Password": pass})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		a.url(ep, s, s.base+"/SessionService/Sessions"), strings.NewReader(string(payload)))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := a.client(s.verifyTLS, ep.Poll.Timeout()).Do(req)
	if err != nil {
		return fmt.Errorf("%w: %v", models.ErrUnreachable, err)
	}
	defer func() { _, _ = io.Copy(io.Discard, resp.Body); resp.Body.Close() }()

	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		return fmt.Errorf("%w: session rejected (%d)", models.ErrAuth, resp.StatusCode)
	}
	if resp.StatusCode >= 300 {
		return fmt.Errorf("%w: session create returned %d", models.ErrProtocolStatus,
			resp.StatusCode)
	}
	s.token = resp.Header.Get("X-Auth-Token")
	s.sessionURI = resp.Header.Get("Location")
	if s.token == "" {
		return fmt.Errorf("%w: no X-Auth-Token in the session response", models.ErrDecode)
	}
	return nil
}

// discover resolves the Thermal, Power and System URLs ONCE.
func (a *Adapter) discover(ctx context.Context, ep *models.Endpoint, s *session) error {
	chassis, err := a.firstMember(ctx, ep, s, s.base+"/Chassis")
	if err != nil {
		return err
	}
	if chassis != "" {
		body, err := a.get(ctx, ep, s, chassis)
		if err == nil {
			s.thermal = linkOf(body, "Thermal")
			s.power = linkOf(body, "Power")
		}
		// Some BMCs omit the links but still serve the conventional paths.
		if s.thermal == "" {
			s.thermal = strings.TrimRight(chassis, "/") + "/Thermal"
		}
		if s.power == "" {
			s.power = strings.TrimRight(chassis, "/") + "/Power"
		}
	}
	if sys, err := a.firstMember(ctx, ep, s, s.base+"/Systems"); err == nil {
		s.system = sys
	}
	if s.thermal == "" && s.power == "" {
		return fmt.Errorf("%w: neither Thermal nor Power discovered", models.ErrDecode)
	}
	return nil
}

func (a *Adapter) firstMember(ctx context.Context, ep *models.Endpoint, s *session,
	collection string) (string, error) {
	body, err := a.get(ctx, ep, s, collection)
	if err != nil {
		return "", err
	}
	members, _ := body["Members"].([]any)
	for _, m := range members {
		if mm, ok := m.(map[string]any); ok {
			if id, ok := mm["@odata.id"].(string); ok && id != "" {
				return id, nil
			}
		}
	}
	return "", nil
}

func linkOf(body map[string]any, key string) string {
	if v, ok := body[key].(map[string]any); ok {
		if id, ok := v["@odata.id"].(string); ok {
			return id
		}
	}
	return ""
}

func (a *Adapter) url(ep *models.Endpoint, _ *session, path string) string {
	port := ep.Port
	if port == 0 {
		port = 443
	}
	return fmt.Sprintf("https://%s:%d%s", ep.Address, port, path)
}

func (a *Adapter) get(ctx context.Context, ep *models.Endpoint, s *session,
	path string) (map[string]any, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, a.url(ep, s, path), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	if s.token != "" {
		req.Header.Set("X-Auth-Token", s.token)
	}

	resp, err := a.client(s.verifyTLS, ep.Poll.Timeout()).Do(req)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", models.ErrUnreachable, err)
	}
	defer func() { _, _ = io.Copy(io.Discard, resp.Body); resp.Body.Close() }()

	if resp.StatusCode == http.StatusUnauthorized {
		// The token expired or the BMC restarted. Drop it so the next poll
		// re-authenticates rather than failing forever.
		a.Forget(ep.ID)
		return nil, fmt.Errorf("%w: token rejected", models.ErrAuth)
	}
	if resp.StatusCode >= 300 {
		return nil, fmt.Errorf("%w: %s returned %d", models.ErrProtocolStatus,
			path, resp.StatusCode)
	}

	var out map[string]any
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(&out); err != nil {
		return nil, fmt.Errorf("%w: %v", models.ErrDecode, err)
	}
	return out, nil
}

// -------------------------------------------------------------- parsing

func (a *Adapter) parseThermal(body map[string]any, ep *models.Endpoint,
	outcome *models.PollOutcome, now int64) {

	for _, raw := range arrayOf(body, "Temperatures") {
		name, _ := raw["Name"].(string)
		entry, ok := a.maps.MatchTemperature(name)
		if !ok {
			continue
		}
		if !a.sensorUsable(raw) {
			// Absent or disabled probes are a MISS, never a zero: a zeroed
			// absent sensor reads as "CPU at 0 C" and raises a false alarm.
			outcome.Misses = append(outcome.Misses,
				models.Miss{Metric: entry.Metric, Reason: models.MissNoSuchObject})
			continue
		}
		v, ok := floatOf(raw[a.maps.Thermal.Temperatures.ReadingField])
		if !ok {
			continue
		}
		instance := ""
		if entry.InstanceFrom == "Name" {
			instance = name
		}
		outcome.Samples = append(outcome.Samples,
			a.sample(ep, entry.Metric, instance, v, now, "/Thermal#/Temperatures/"+name))
	}

	for _, raw := range arrayOf(body, "Fans") {
		if !a.sensorUsable(raw) {
			continue
		}
		v, ok := floatOf(raw[a.maps.Thermal.Fans.ReadingField])
		if !ok {
			continue
		}
		// Percent or RPM, decided by the BMC's own units rather than assumed:
		// recording a percentage as an rpm figure makes a healthy fan look
		// stopped.
		metric := a.maps.Thermal.Fans.MetricRPM
		if units, _ := raw["ReadingUnits"].(string); strings.EqualFold(units, "Percent") {
			metric = a.maps.Thermal.Fans.MetricPct
		}
		name, _ := raw["Name"].(string)
		outcome.Samples = append(outcome.Samples,
			a.sample(ep, metric, name, v, now, "/Thermal#/Fans/"+name))
	}
}

func (a *Adapter) parsePower(body map[string]any, ep *models.Endpoint,
	outcome *models.PollOutcome, now int64) {

	for _, raw := range arrayOf(body, "PowerControl") {
		for _, f := range a.maps.Power.PowerControl {
			if v, ok := floatOf(raw[f.Field]); ok {
				outcome.Samples = append(outcome.Samples,
					a.sample(ep, f.Metric, "", v, now, "/Power#/PowerControl/"+f.Field))
			}
		}
	}

	psu := a.maps.Power.PowerSupply
	for i, raw := range arrayOf(body, "PowerSupplies") {
		instance := fmt.Sprintf("%d", i)
		if psu.InstanceFrom != "" {
			if v, ok := raw[psu.InstanceFrom].(string); ok && v != "" {
				instance = v
			}
		}
		for _, f := range psu.Fields {
			if v, ok := floatOf(raw[f.Field]); ok {
				outcome.Samples = append(outcome.Samples,
					a.sample(ep, f.Metric, instance, v, now, "/Power#/PowerSupplies/"+f.Field))
			}
		}
		if psu.State.Metric != "" {
			state := nestedString(raw, psu.State.StatusStateField)
			health := nestedString(raw, psu.State.StatusHealthField)
			good := strings.EqualFold(state, psu.State.HealthyState) &&
				(health == "" || strings.EqualFold(health, psu.State.HealthyHealth))
			t := a.sample(ep, psu.State.Metric, instance, 0, now, "/Power#/PowerSupplies/Status")
			t.ValueType = models.ValueTypeBool
			t.BoolValue = good
			t.DoubleValue = 0
			outcome.Samples = append(outcome.Samples, t)
		}
	}
}

func (a *Adapter) sensorUsable(raw map[string]any) bool {
	if a.maps.SkipWhen.NullReading {
		// Handled by the caller's float check too, but an explicit null is
		// worth distinguishing from a malformed value.
		if _, present := raw["Reading"]; present && raw["Reading"] == nil {
			return false
		}
		if _, present := raw["ReadingCelsius"]; present && raw["ReadingCelsius"] == nil {
			return false
		}
	}
	allowed := a.maps.SkipWhen.StatusStateNotIn
	if len(allowed) == 0 {
		return true
	}
	state := nestedString(raw, "Status.State")
	if state == "" {
		return true // a BMC that reports no status is not asserting a fault
	}
	for _, ok := range allowed {
		if strings.EqualFold(state, ok) {
			return true
		}
	}
	return false
}

func (a *Adapter) sample(ep *models.Endpoint, metric, instance string, value float64,
	now int64, pointer string) models.Telemetry {
	def, _ := models.ValidateMetric(metric)
	return models.Telemetry{
		EndpointID:     ep.ID,
		DeviceID:       ep.DeviceID,
		Metric:         metric,
		Instance:       instance,
		ValueType:      models.ValueTypeGauge,
		DoubleValue:    value,
		Unit:           def.Unit,
		ObservedAt:     now,
		CollectedAt:    now,
		SourceProtocol: models.ProtocolRedfish,
		Quality:        models.QualityGood,
		Metadata:       map[string]string{"pointer": pointer},
	}
}

// -------------------------------------------------------------- helpers

func credentials(ep *models.Endpoint) (string, string) {
	if ep.Credential == nil || ep.Credential.Data == nil {
		return "", ""
	}
	user, _ := ep.Credential.Data["username"].(string)
	pass, _ := ep.Credential.Data["password"].(string)
	return user, pass
}

func arrayOf(body map[string]any, key string) []map[string]any {
	raw, _ := body[key].([]any)
	out := make([]map[string]any, 0, len(raw))
	for _, item := range raw {
		if m, ok := item.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

func floatOf(v any) (float64, bool) {
	switch x := v.(type) {
	case float64:
		return x, true
	case float32:
		return float64(x), true
	case int:
		return float64(x), true
	case int64:
		return float64(x), true
	case json.Number:
		f, err := x.Float64()
		return f, err == nil
	default:
		return 0, false
	}
}

// nestedString reads "Status.Health" style dotted paths.
func nestedString(m map[string]any, path string) string {
	cur := any(m)
	for _, part := range strings.Split(path, ".") {
		mm, ok := cur.(map[string]any)
		if !ok {
			return ""
		}
		cur = mm[part]
	}
	s, _ := cur.(string)
	return s
}

func missReason(err error) string {
	switch models.ClassifyError(err) {
	case models.ErrClassTimeout, models.ErrClassUnreachable:
		return models.MissTimeout
	case models.ErrClassDecode:
		return models.MissDecode
	default:
		return models.MissUnsupported
	}
}

var _ models.Adapter = (*Adapter)(nil)
