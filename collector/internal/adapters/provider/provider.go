package provider

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// Adapter polls one cabinet's telemetry from a colo provider's own API.
//
// Every other adapter in this collector speaks a device protocol to
// something on a network it can reach. This one cannot: the provider's own
// smart PDUs and sensors are on the PROVIDER's management network, not the
// tenant's, and the only thing a tenant gets is whatever the provider's
// portal exposes over REST, rate-limited and retained on the provider's own
// terms (docs/26 Phase 10). Samples still flow through the same pipeline as
// every other protocol, tagged with a distinct source_protocol - an operator
// comparing a provider-reported cabinet power draw against this tenant's own
// SNMP-polled PDU reading for the same rack needs to tell which is which.
type Adapter struct {
	log     *slog.Logger
	mets    *obs.Metrics
	client  *http.Client
	timeout time.Duration

	mu   sync.Mutex
	auth map[string]*TokenSource // endpoint id -> its own OAuth2 client
}

func New(log *slog.Logger, mets *obs.Metrics, timeout time.Duration) *Adapter {
	if timeout <= 0 {
		timeout = 15 * time.Second // a provider's own portal API, not a LAN device
	}
	return &Adapter{
		log: log, mets: mets, timeout: timeout,
		client: &http.Client{
			Timeout: timeout,
			Transport: &http.Transport{
				MaxIdleConnsPerHost: 4,
				TLSClientConfig:     &tls.Config{MinVersion: tls.VersionTLS12},
			},
		},
		auth: make(map[string]*TokenSource),
	}
}

func (a *Adapter) Protocol() string              { return "provider" }
func (a *Adapter) Init(_ context.Context) error  { return nil }
func (a *Adapter) Close(_ context.Context) error { return nil }

func (a *Adapter) Forget(endpointID string) {
	a.mu.Lock()
	delete(a.auth, endpointID)
	a.mu.Unlock()
}

// reading is the shape this adapter expects a cabinet telemetry response to
// carry. This is a representative shape, not a verified transcription of any
// specific provider's actual response - see docs/26 for why: build against
// the real API once one is reachable, adjust this decoder to match.
type reading struct {
	PowerW       *float64 `json:"power_w"`
	TemperatureC *float64 `json:"temperature_c"`
	HumidityPct  *float64 `json:"humidity_pct"`
}

func (a *Adapter) Poll(ctx context.Context, ep *models.Endpoint) (*models.PollOutcome, error) {
	started := time.Now()

	ts, err := a.tokenSource(ep)
	if err != nil {
		return nil, err
	}
	token, err := ts.Token(ctx)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", models.ErrAuth, err)
	}

	reqURL, err := endpointURL(ep)
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, reqURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/json")

	resp, err := a.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", models.ErrUnreachable, err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		// The token may simply have been revoked server-side before its own
		// expiry - the only way to find out is to ask for a fresh one next
		// time rather than repeat a request the provider has already refused.
		ts.Invalidate()
		return nil, fmt.Errorf("%w: %s replied %s", models.ErrAuth, reqURL, resp.Status)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%w: %s replied %s: %s", models.ErrProtocolStatus,
			reqURL, resp.Status, strings.TrimSpace(string(body)))
	}

	var r reading
	if err := json.Unmarshal(body, &r); err != nil {
		return nil, fmt.Errorf("%w: %s: %v", models.ErrDecode, reqURL, err)
	}

	outcome := &models.PollOutcome{}
	now := models.NowMicros()
	a.sample(outcome, ep, "power_draw", r.PowerW, now)
	a.sample(outcome, ep, "inlet_temperature", r.TemperatureC, now)
	a.sample(outcome, ep, "relative_humidity", r.HumidityPct, now)

	outcome.LatencyMs = int(time.Since(started).Milliseconds())
	outcome.Partial = len(outcome.Misses) > 0
	if len(outcome.Samples) == 0 {
		return outcome, fmt.Errorf("%w: %s returned no recognised fields",
			models.ErrDecode, reqURL)
	}
	a.mets.SamplesTotal.WithLabelValues("provider").Add(float64(len(outcome.Samples)))
	return outcome, nil
}

func (a *Adapter) sample(outcome *models.PollOutcome, ep *models.Endpoint,
	metric string, v *float64, now int64) {

	if v == nil {
		outcome.Misses = append(outcome.Misses,
			models.Miss{Metric: metric, Reason: models.MissNoSuchObject})
		return
	}
	def, _ := models.ValidateMetric(metric)
	outcome.Samples = append(outcome.Samples, models.Telemetry{
		EndpointID:     ep.ID,
		DeviceID:       ep.DeviceID,
		Metric:         metric,
		ValueType:      models.ValueTypeGauge,
		DoubleValue:    *v,
		Unit:           def.Unit,
		ObservedAt:     now,
		CollectedAt:    now,
		SourceProtocol: models.ProtocolProvider,
		Quality:        models.QualityGood,
	})
}

// ------------------------------------------------------------- auth + URL

func (a *Adapter) tokenSource(ep *models.Endpoint) (*TokenSource, error) {
	a.mu.Lock()
	ts, ok := a.auth[ep.ID]
	a.mu.Unlock()
	if ok {
		return ts, nil
	}

	if ep.Credential == nil || ep.Credential.Data == nil {
		return nil, fmt.Errorf("%w: %s has no credential", models.ErrConfig, ep.ID)
	}
	clientID, _ := ep.Credential.Data["client_id"].(string)
	clientSecret, _ := ep.Credential.Data["client_secret"].(string)
	tokenURL, _ := ep.Credential.Data["token_url"].(string)
	if clientID == "" || clientSecret == "" || tokenURL == "" {
		return nil, fmt.Errorf(
			"%w: %s's credential needs client_id, client_secret and token_url",
			models.ErrConfig, ep.ID)
	}

	ts = NewTokenSource(tokenURL, clientID, clientSecret, a.client)
	a.mu.Lock()
	a.auth[ep.ID] = ts
	a.mu.Unlock()
	return ts, nil
}

// endpointURL builds the cabinet telemetry request.
//
// addressing.path, given whole, overrides the default template entirely -
// providers do not agree on a URL shape, and guessing one for every API
// would be wrong for all but the one it was guessed from. Without it, the
// default template needs addressing.cabinet_id.
func endpointURL(ep *models.Endpoint) (string, error) {
	if ep.Address == "" {
		return "", fmt.Errorf("%w: endpoint has no address", models.ErrConfig)
	}
	base := ep.Address
	if !strings.Contains(base, "://") {
		base = "https://" + base
	}
	base = strings.TrimRight(base, "/")

	if p, ok := ep.Addressing["path"].(string); ok && p != "" {
		return base + p, nil
	}
	cabinetID, ok := ep.Addressing["cabinet_id"]
	if !ok {
		return "", fmt.Errorf(
			"%w: %s needs addressing.cabinet_id (or a full addressing.path)",
			models.ErrConfig, ep.ID)
	}
	return fmt.Sprintf("%s/v1/cabinets/%s/telemetry", base, toPathSegment(cabinetID)), nil
}

func toPathSegment(v any) string {
	switch n := v.(type) {
	case string:
		return n
	case float64:
		return strconv.FormatInt(int64(n), 10)
	case int:
		return strconv.Itoa(n)
	default:
		return fmt.Sprint(v)
	}
}
