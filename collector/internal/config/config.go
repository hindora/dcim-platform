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
		SNMP ProtocolCfg `yaml:"snmp"`
	} `yaml:"protocols"`

	Health struct {
		OfflineThreshold int `yaml:"offline_threshold"`
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

type ProtocolCfg struct {
	Enabled        bool          `yaml:"enabled"`
	MaxConcurrent  int           `yaml:"max_concurrent"`
	PerHost        int           `yaml:"per_host"`
	Timeout        time.Duration `yaml:"timeout"`
	Retries        int           `yaml:"retries"`
	MaxRepetitions int           `yaml:"max_repetitions"`
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
	c.DCIM.BaseURL = "http://localhost:8000"
	c.DCIM.TokenEnv = "DCIM_COLLECTOR_TOKEN"
	c.DCIM.AssignmentPath = "/api/v1/collector/assignments"
	c.DCIM.AssignmentInterval = 30 * time.Second
	c.DCIM.RequestTimeout = 15 * time.Second
	c.Redis.URLEnv = "DCIM_REDIS_URL"
	c.Redis.URL = "redis://localhost:6379/0"
	c.Redis.Streams.Telemetry = StreamCfg{Name: "telemetry.v1", MaxLen: 2000000}
	c.Redis.Streams.Events = StreamCfg{Name: "events.v1", MaxLen: 500000}
	c.Redis.Streams.EndpointState = StreamCfg{Name: "endpointstate.v1", MaxLen: 200000}
	c.Redis.Streams.Heartbeat = StreamCfg{Name: "collectorhb.v1", MaxLen: 10000}
	c.Publisher.MaxBatch = 500
	c.Publisher.MaxDelay = 200 * time.Millisecond
	c.Publisher.RingCapacity = 50000
	c.Workers.PoolSize = 128
	c.Workers.QueueMultiplier = 8
	c.Protocols.SNMP = ProtocolCfg{
		Enabled: true, MaxConcurrent: 256, PerHost: 4,
		Timeout: 3 * time.Second, Retries: 2, MaxRepetitions: 25,
	}
	c.Health.OfflineThreshold = 3
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
