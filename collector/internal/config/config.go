// Package config loads PROCESS configuration only.
//
// There is deliberately no device list here. The DCIM database is the source of
// truth for what exists and the collector pulls its assignment from the API: a
// file-based device list goes stale within minutes of a fleet change, and it
// hides the very behaviour the platform exists to observe.
package config

import (
	"fmt"
	"os"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

type Config struct {
	Collector struct {
		ID      string `yaml:"id"`
		Version string `yaml:"version"`
	} `yaml:"collector"`

	DCIM struct {
		BaseURL            string        `yaml:"base_url"`
		TokenEnv           string        `yaml:"token_env"`
		AssignmentPath     string        `yaml:"assignment_path"`
		AssignmentInterval time.Duration `yaml:"assignment_interval"`
		RequestTimeout     time.Duration `yaml:"request_timeout"`
		token              string
	} `yaml:"dcim"`

	Redis struct {
		URLEnv  string `yaml:"url_env"`
		URL     string `yaml:"url"`
		Streams struct {
			Telemetry     StreamCfg `yaml:"telemetry"`
			Events        StreamCfg `yaml:"events"`
			EndpointState StreamCfg `yaml:"endpointstate"`
			Heartbeat     StreamCfg `yaml:"heartbeat"`
		} `yaml:"streams"`
	} `yaml:"redis"`

	Publisher struct {
		MaxBatch     int           `yaml:"max_batch"`
		MaxDelay     time.Duration `yaml:"max_delay"`
		RingCapacity int           `yaml:"ring_capacity"`
	} `yaml:"publisher"`

	Workers struct {
		PoolSize        int `yaml:"pool_size"`
		QueueMultiplier int `yaml:"queue_multiplier"`
	} `yaml:"workers"`

	Protocols struct {
		SNMP         ProtocolCfg     `yaml:"snmp"`
		SNMPTrap     TrapCfg         `yaml:"snmp_trap"`
		Redfish      ProtocolCfg     `yaml:"redfish"`
		RedfishEvent RedfishEventCfg `yaml:"redfish_event"`
		BACnet       BACnetCfg       `yaml:"bacnet"`
		Modbus       ProtocolCfg     `yaml:"modbus"`
		GNMI         GNMICfg         `yaml:"gnmi"`
	} `yaml:"protocols"`

	Health struct {
		OfflineThreshold int `yaml:"offline_threshold"`
		// How often an endpoint whose status has NOT changed republishes its
		// liveness row. Zero uses the default. Effective freshness is
		// max(pollInterval, this).
		RefreshInterval time.Duration `yaml:"refresh_interval"`
	} `yaml:"health"`

	Mappings struct {
		Dir string `yaml:"dir"`
	} `yaml:"mappings"`

	Observability struct {
		LogLevel       string        `yaml:"log_level"`
		LogFormat      string        `yaml:"log_format"`
		MetricsListen  string        `yaml:"metrics_listen"`
		HealthListen   string        `yaml:"health_listen"`
		HeartbeatEvery time.Duration `yaml:"heartbeat_every"`
	} `yaml:"observability"`

	Limits struct {
		MaxOpenFiles int `yaml:"max_open_files"`
	} `yaml:"limits"`
}

type StreamCfg struct {
	Name   string `yaml:"name"`
	MaxLen int64  `yaml:"maxlen"`
}

// TrapCfg configures the inbound trap path, which is deliberately separate
// from polling: a trap reports a state change and carries no value, so it must
// never be fed through the telemetry pipeline.
type TrapCfg struct {
	Enabled bool   `yaml:"enabled"`
	Listen  string `yaml:"listen"`
	Workers int    `yaml:"workers"`
	// Per-source ceiling. One flapping interface must not be able to fill the
	// stream or the disk.
	RateLimitPerMinute int `yaml:"rate_limit_per_minute"`
}

// RedfishEventCfg configures the inbound Redfish event path.
//
// Advertise has no default and must be set explicitly. The collector cannot
// work out which of its own addresses a BMC on a management VLAN can reach -
// on a multi-homed host, guessing wrong means the subscriptions are created
// successfully and then deliver nothing, with no error on either side.
type RedfishEventCfg struct {
	Enabled bool   `yaml:"enabled"`
	Listen  string `yaml:"listen"`
	// host:port the BMCs are told to POST to.
	Advertise string `yaml:"advertise"`
	// TLS on the DESTINATION url, not the listener. Off by default: firmware
	// that cannot verify the receiver's certificate drops events silently.
	TLS                bool          `yaml:"tls"`
	Workers            int           `yaml:"workers"`
	RateLimitPerMinute int           `yaml:"rate_limit_per_minute"`
	ReconcileEvery     time.Duration `yaml:"reconcile_every"`
}

// BACnetCfg configures the BACnet/IP client.
//
// BACnet has no per-endpoint socket: one UDP socket serves every device,
// because Who-Is, I-Am and COV notifications arrive unsolicited and a
// per-device socket would receive none of them.
type BACnetCfg struct {
	Enabled bool `yaml:"enabled"`
	// LocalPort 0 asks the kernel for an ephemeral port. Replies come back to
	// the source of the request, so 47808 is only needed to receive
	// broadcasts - and on a host already running something that speaks
	// BACnet, binding it fails.
	LocalPort     int           `yaml:"local_port"`
	MaxConcurrent int           `yaml:"max_concurrent"`
	PerHost       int           `yaml:"per_host"`
	Timeout       time.Duration `yaml:"timeout"`
	Retries       int           `yaml:"retries"`
	// Objects per ReadPropertyMultiple. Sized well under a 1476-byte APDU: an
	// oversized request comes back as an abort, not a short answer.
	BatchSize int `yaml:"batch_size"`
}

// GNMICfg configures the gNMI client.
//
// Two collection modes share one connection pool: Get for endpoints the
// scheduler polls, and Subscribe for endpoints whose poll profile says the
// device pushes instead (interval 0, push enabled - the gnmi-stream profile).
type GNMICfg struct {
	Enabled       bool          `yaml:"enabled"`
	MaxConcurrent int           `yaml:"max_concurrent"`
	PerHost       int           `yaml:"per_host"`
	Timeout       time.Duration `yaml:"timeout"`
	// Stream turns on the Subscribe path. Without it, push-profile endpoints
	// are simply not collected - which is visible as an endpoint that never
	// reports, rather than as silently missing data.
	Stream bool `yaml:"stream"`
	// A stream that is connected and silent looks exactly like one that has
	// died. Silence past this multiple of the requested sample interval fails
	// the session.
	StreamGraceFactor float64 `yaml:"stream_grace_factor"`
	// GraceWindow overrides the interval-derived window with a fixed one.
	GraceWindow time.Duration `yaml:"stream_grace_window"`
}

type ProtocolCfg struct {
	Enabled        bool          `yaml:"enabled"`
	MaxConcurrent  int           `yaml:"max_concurrent"`
	PerHost        int           `yaml:"per_host"`
	Timeout        time.Duration `yaml:"timeout"`
	Retries        int           `yaml:"retries"`
	MaxRepetitions int           `yaml:"max_repetitions"`
	// AcceptAnySourceReply tolerates an agent whose reply source address
	// differs from the address polled - normal for wildcard-bound agents.
	AcceptAnySourceReply bool `yaml:"accept_any_source_reply"`
}

// Token returns the DCIM API token, read from the environment. It is never
// stored in the config file.
func (c *Config) Token() string { return c.DCIM.token }

func Load(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config: %w", err)
	}
	cfg := Default()
	if err := yaml.Unmarshal(raw, cfg); err != nil {
		return nil, fmt.Errorf("parse config: %w", err)
	}
	if err := cfg.resolve(); err != nil {
		return nil, err
	}
	return cfg, cfg.validate()
}

func Default() *Config {
	c := &Config{}
	c.Collector.ID = "col-1"
	c.DCIM.BaseURL = "http://127.0.0.1:8000"
	c.DCIM.TokenEnv = "DCIM_COLLECTOR_TOKEN"
	c.DCIM.AssignmentPath = "/api/v1/collector/assignments"
	c.DCIM.AssignmentInterval = 30 * time.Second
	c.DCIM.RequestTimeout = 15 * time.Second
	c.Redis.URLEnv = "DCIM_REDIS_URL"
	c.Redis.URL = "redis://127.0.0.1:6379/0"
	// These caps are entry counts, but the thing that actually runs out is
	// memory, so they are derived from measured entry SIZE against a measured
	// memory budget - see contracts/schema/messages_v1.yaml, which is the
	// source of truth this mirrors.
	//
	// A telemetry entry is a batch of samples: 28 KB averaged over a day, 49 KB
	// for recent traffic. The old cap of 2,000,000 described a stream of tens of
	// GB on a 3.7 GB host, so Redis was OOM-killed at ~2 GB and the trim never
	// once fired. XACK does not shorten a stream - only the cap does - so the
	// cap is the whole memory budget.
	//
	// 8000 entries is ~400 MB inside Redis's 1 GB ceiling, and ~55 minutes of
	// buffer at the observed 2.4 entries a second.
	c.Redis.Streams.Telemetry = StreamCfg{Name: "telemetry.v1", MaxLen: 8000}
	// The other three are small (~300 bytes an entry), so their counts can
	// stay generous: 100k endpoint states is ~30 MB.
	c.Redis.Streams.Events = StreamCfg{Name: "events.v1", MaxLen: 200000}
	c.Redis.Streams.EndpointState = StreamCfg{Name: "endpointstate.v1", MaxLen: 100000}
	c.Redis.Streams.Heartbeat = StreamCfg{Name: "collectorhb.v1", MaxLen: 10000}
	c.Publisher.MaxBatch = 500
	c.Publisher.MaxDelay = 200 * time.Millisecond
	c.Publisher.RingCapacity = 50000
	c.Workers.PoolSize = 128
	c.Workers.QueueMultiplier = 8
	c.Protocols.SNMP = ProtocolCfg{
		Enabled: true, MaxConcurrent: 256, PerHost: 4,
		Timeout: 3 * time.Second, Retries: 2, MaxRepetitions: 25,
		AcceptAnySourceReply: true,
	}
	c.Protocols.Redfish = ProtocolCfg{
		// per_host 1: a BMC serialises requests anyway, and hammering one is
		// how a poll cycle turns into a queue of TLS handshakes.
		Enabled: true, MaxConcurrent: 32, PerHost: 1,
		Timeout: 8 * time.Second, Retries: 1,
	}
	c.Protocols.SNMPTrap = TrapCfg{
		Enabled: true, Listen: "0.0.0.0:162", Workers: 8,
		RateLimitPerMinute: 100,
	}
	c.Protocols.RedfishEvent = RedfishEventCfg{
		Enabled: false, Listen: "0.0.0.0:9143", Workers: 4,
		RateLimitPerMinute: 120, ReconcileEvery: 10 * time.Minute,
	}
	c.Protocols.BACnet = BACnetCfg{
		Enabled: false, LocalPort: 0, MaxConcurrent: 24, PerHost: 1,
		Timeout: 3 * time.Second, Retries: 2, BatchSize: 16,
	}
	c.Protocols.Modbus = ProtocolCfg{
		Enabled: false, MaxConcurrent: 24, PerHost: 1,
		Timeout: 3 * time.Second, Retries: 1,
	}
	c.Protocols.GNMI = GNMICfg{
		Enabled: false, MaxConcurrent: 16, PerHost: 2,
		Timeout: 10 * time.Second, Stream: true, StreamGraceFactor: 3,
	}
	c.Health.OfflineThreshold = 3
	// 60 s bounds how stale endpoint_state can get without making the write
	// rate interesting: 1386 endpoints refreshing at most once a minute is
	// ~15 rows a second, and the slower-polled ones report at their own
	// interval rather than this one.
	c.Health.RefreshInterval = 60 * time.Second
	c.Mappings.Dir = "../contracts/mappings"
	c.Observability.LogLevel = "info"
	c.Observability.LogFormat = "json"
	c.Observability.MetricsListen = "0.0.0.0:9100"
	c.Observability.HealthListen = "0.0.0.0:9101"
	c.Observability.HeartbeatEvery = 10 * time.Second
	c.Limits.MaxOpenFiles = 65536
	return c
}

func (c *Config) resolve() error {
	if env := strings.TrimSpace(c.DCIM.TokenEnv); env != "" {
		c.DCIM.token = os.Getenv(env)
	}
	if env := strings.TrimSpace(c.Redis.URLEnv); env != "" {
		if v := os.Getenv(env); v != "" {
			c.Redis.URL = v
		}
	}
	return nil
}

func (c *Config) validate() error {
	if c.Collector.ID == "" {
		return fmt.Errorf("collector.id is required")
	}
	if c.DCIM.token == "" {
		return fmt.Errorf("no collector token: set %s", c.DCIM.TokenEnv)
	}
	if c.Workers.PoolSize < 1 {
		return fmt.Errorf("workers.pool_size must be >= 1")
	}
	if c.Publisher.MaxBatch < 1 {
		return fmt.Errorf("publisher.max_batch must be >= 1")
	}
	return nil
}
