package models

import (
	"context"
	"errors"
	"time"
)

// Endpoint is one protocol endpoint on one device. It - not the device - is the
// unit of collection, credentials, scheduling and health: a server has an OS
// SNMP agent and a BMC agent on different addresses, and a field device has no
// address of its own at all.
type Endpoint struct {
	ID         string         `json:"id"`
	DeviceID   string         `json:"device_id"`
	DeviceName string         `json:"device_name"`
	DeviceType string         `json:"device_type"`
	Vendor     string         `json:"vendor"`
	Model      string         `json:"model"`
	Protocol   string         `json:"protocol"`
	Role       string         `json:"role"`
	Address    string         `json:"address"`
	Port       int            `json:"port"`
	Addressing map[string]any `json:"addressing"`
	// Endpoint this one is reached THROUGH: a Modbus gateway, a BACnet router.
	// Its failure is the parent's, not six independent ones.
	ViaEndpointID string      `json:"via_endpoint_id"`
	Credential    *Credential `json:"credential"`
	Poll          PollProfile `json:"poll"`
}

type Credential struct {
	Kind string         `json:"kind"`
	Data map[string]any `json:"data"`
}

// Community returns the SNMP community. In this device plane the community IS
// the agent's IP address, and a wrong one is a SILENT DROP that looks exactly
// like a dead device - so an empty result is worth failing loudly on.
func (c *Credential) Community() string {
	if c == nil || c.Data == nil {
		return ""
	}
	if v, ok := c.Data["community"].(string); ok {
		return v
	}
	return ""
}

type PollProfile struct {
	IntervalS    int      `json:"interval_s"`
	TimeoutMs    int      `json:"timeout_ms"`
	Retries      int      `json:"retries"`
	MetricGroups []string `json:"metric_groups"`
	PushEnabled  bool     `json:"push_enabled"`
}

func (p PollProfile) Interval() time.Duration {
	if p.IntervalS <= 0 {
		return 30 * time.Second
	}
	return time.Duration(p.IntervalS) * time.Second
}

func (p PollProfile) Timeout() time.Duration {
	if p.TimeoutMs <= 0 {
		return 3 * time.Second
	}
	return time.Duration(p.TimeoutMs) * time.Millisecond
}

// Miss records a metric that could not be read, with a reason. A poll that
// reads 18 of 20 OIDs is a partial success, not a failure - treating it as a
// failure is why some NMS mark healthy devices down.
type Miss struct {
	Metric string
	Reason string
}

// Miss reasons.
const (
	MissNoSuchObject = "no_such_object"
	MissTimeout      = "timeout"
	MissDecode       = "decode"
	MissUnsupported  = "not_supported"
)

type PollOutcome struct {
	Samples   []Telemetry
	Events    []Event
	Misses    []Miss
	LatencyMs int
	Partial   bool
}

// Error classes. The health tracker branches on these: an auth failure must
// never be retried like a timeout, because on real hardware that locks accounts.
const (
	ErrClassTimeout     = "timeout"
	ErrClassAuth        = "auth"
	ErrClassRefused     = "refused"
	ErrClassUnreachable = "unreachable"
	ErrClassDecode      = "decode"
	ErrClassProtocol    = "protocol"
)

var (
	ErrTimeout     = errors.New("timeout")
	ErrAuth        = errors.New("authentication failed")
	ErrUnreachable = errors.New("unreachable")
	ErrDecode      = errors.New("decode failed")
)

// ClassifyError maps an adapter error onto a health error class.
func ClassifyError(err error) string {
	switch {
	case err == nil:
		return ""
	case errors.Is(err, ErrAuth):
		return ErrClassAuth
	case errors.Is(err, ErrTimeout):
		return ErrClassTimeout
	case errors.Is(err, ErrUnreachable):
		return ErrClassUnreachable
	case errors.Is(err, ErrDecode):
		return ErrClassDecode
	default:
		return ErrClassProtocol
	}
}

// Adapter is a protocol implementation: one instance per protocol per process.
type Adapter interface {
	Protocol() string
	Init(ctx context.Context) error
	Poll(ctx context.Context, ep *Endpoint) (*PollOutcome, error)
	Close(ctx context.Context) error
}

// Sink is where normalised telemetry goes. Adapters never touch Redis or the
// database directly.
type Sink interface {
	Telemetry(ctx context.Context, samples []Telemetry) error
	Events(ctx context.Context, events []Event) error
	EndpointState(ctx context.Context, state EndpointState) error
}

// NowMicros is the contract's timestamp unit: microseconds since the Unix
// epoch, UTC.
func NowMicros() int64 { return time.Now().UTC().UnixMicro() }

// ValidateMetric reports whether a metric key exists in the registry. The
// collector refuses to emit unknown keys: the alternative is a metric that
// silently never appears and nobody notices for a week.
func ValidateMetric(key string) (MetricDef, bool) {
	d, ok := MetricDefs[key]
	return d, ok
}
