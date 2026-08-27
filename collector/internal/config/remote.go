package config

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"sync"
	"time"
)

// Overrides is the operational half of the configuration, held in the DCIM
// database and fetched from it.
//
// The other half - this collector's id, the API address, the token, Redis -
// stays in the file on purpose. Break the path to the control plane from the
// control plane and nobody can repair it from the control plane either.
//
// Every field is a pointer: absent means "the file's value stands", which is
// what lets a default change in a release and reach every collector that never
// overrode it. A zero value is a real setting - `local_port: 0` asks the
// kernel for an ephemeral port and is the sensible BACnet default - so absent
// and zero cannot be the same thing here.
type Overrides struct {
	SNMP         *ProtocolOverride `json:"snmp,omitempty"`
	Redfish      *ProtocolOverride `json:"redfish,omitempty"`
	Modbus       *ProtocolOverride `json:"modbus,omitempty"`
	BACnet       *BACnetOverride   `json:"bacnet,omitempty"`
	GNMI         *GNMIOverride     `json:"gnmi,omitempty"`
	SNMPTrap     *TrapOverride     `json:"snmp_trap,omitempty"`
	RedfishEvent *RFEventOverride  `json:"redfish_event,omitempty"`
}

type ProtocolOverride struct {
	Enabled       *bool `json:"enabled,omitempty"`
	MaxConcurrent *int  `json:"max_concurrent,omitempty"`
	PerHost       *int  `json:"per_host,omitempty"`
	TimeoutS      *int  `json:"timeout_s,omitempty"`
	Retries       *int  `json:"retries,omitempty"`
}

type BACnetOverride struct {
	ProtocolOverride
	LocalPort *int `json:"local_port,omitempty"`
}

type GNMIOverride struct {
	ProtocolOverride
	Stream *bool `json:"stream,omitempty"`
}

// TrapOverride is the one block that can be applied without a restart: the
// receiver owns its socket and its workers, so it can be closed and reopened
// in place.
type TrapOverride struct {
	Enabled            *bool   `json:"enabled,omitempty"`
	Listen             *string `json:"listen,omitempty"`
	Workers            *int    `json:"workers,omitempty"`
	RateLimitPerMinute *int    `json:"rate_limit_per_minute,omitempty"`
}

type RFEventOverride struct {
	Enabled   *bool   `json:"enabled,omitempty"`
	Listen    *string `json:"listen,omitempty"`
	Advertise *string `json:"advertise,omitempty"`
	TLS       *bool   `json:"tls,omitempty"`
}

type remoteDoc struct {
	CollectorID string    `json:"collector_id"`
	Version     uint32    `json:"version"`
	Config      Overrides `json:"config"`
}

// RemoteClient polls the platform for this collector's configuration.
//
// Modelled on the assignment client deliberately: same token, same ETag
// conditional fetch, same rule that a failed refresh keeps the last known good
// rather than falling back to nothing. A collector that cannot reach the API
// must go on collecting exactly as it was.
type RemoteClient struct {
	cfg  *Config
	http *http.Client
	log  *slog.Logger

	mu      sync.RWMutex
	version uint32
	current Overrides
	etag    string

	// OnChange is called with the new document whenever the version moves.
	OnChange func(version uint32, o Overrides)
}

func NewRemoteClient(cfg *Config, log *slog.Logger) *RemoteClient {
	return &RemoteClient{
		cfg:  cfg,
		http: &http.Client{Timeout: cfg.DCIM.RequestTimeout},
		log:  log,
	}
}

// Version is the configuration this collector last successfully fetched.
func (c *RemoteClient) Version() uint32 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.version
}

func (c *RemoteClient) Current() Overrides {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.current
}

// Run refreshes on the assignment interval until ctx is cancelled.
func (c *RemoteClient) Run(ctx context.Context) {
	if err := c.Refresh(ctx); err != nil {
		c.log.Warn("initial config fetch failed; running the file as-is",
			"error", err)
	}
	ticker := time.NewTicker(c.cfg.DCIM.AssignmentInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := c.Refresh(ctx); err != nil {
				// Same reasoning as a failed assignment refresh: keep running
				// what we have. Reverting to the file on a network blip would
				// move listeners and concurrency under a live estate.
				c.log.Warn("config refresh failed; keeping the last known set",
					"error", err, "version", c.Version())
			}
		}
	}
}

func (c *RemoteClient) Refresh(ctx context.Context) error {
	url := fmt.Sprintf("%s/api/v1/collector/config?collector_id=%s",
		c.cfg.DCIM.BaseURL, c.cfg.Collector.ID)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.cfg.Token())
	req.Header.Set("Accept", "application/json")
	c.mu.RLock()
	etag := c.etag
	c.mu.RUnlock()
	if etag != "" {
		req.Header.Set("If-None-Match", etag)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	switch resp.StatusCode {
	case http.StatusNotModified:
		return nil
	case http.StatusOK:
	case http.StatusNotFound:
		// An older platform that does not serve config at all. Not an error:
		// the file is a complete configuration on its own.
		return nil
	case http.StatusUnauthorized, http.StatusForbidden:
		return fmt.Errorf("config rejected (%d): check the collector token",
			resp.StatusCode)
	default:
		return fmt.Errorf("config fetch failed: HTTP %d", resp.StatusCode)
	}

	var doc remoteDoc
	if err := json.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return fmt.Errorf("decode config: %w", err)
	}

	c.mu.Lock()
	changed := doc.Version != c.version
	c.version, c.current, c.etag = doc.Version, doc.Config, resp.Header.Get("ETag")
	c.mu.Unlock()

	if changed {
		c.log.Info("collector config changed", "version", doc.Version)
		if c.OnChange != nil {
			c.OnChange(doc.Version, doc.Config)
		}
	}
	return nil
}

// Apply folds the overrides into a Config.
//
// Used at startup, before any adapter is built, so a stored setting is in
// force from the first poll rather than from the second config fetch thirty
// seconds later.
func (o Overrides) Apply(cfg *Config) {
	applyProtocol(o.SNMP, &cfg.Protocols.SNMP)
	applyProtocol(o.Redfish, &cfg.Protocols.Redfish)
	applyProtocol(o.Modbus, &cfg.Protocols.Modbus)

	if o.BACnet != nil {
		b := &cfg.Protocols.BACnet
		setBool(o.BACnet.Enabled, &b.Enabled)
		setInt(o.BACnet.MaxConcurrent, &b.MaxConcurrent)
		setInt(o.BACnet.PerHost, &b.PerHost)
		setSeconds(o.BACnet.TimeoutS, &b.Timeout)
		setInt(o.BACnet.Retries, &b.Retries)
		setInt(o.BACnet.LocalPort, &b.LocalPort)
	}
	if o.GNMI != nil {
		g := &cfg.Protocols.GNMI
		setBool(o.GNMI.Enabled, &g.Enabled)
		setInt(o.GNMI.MaxConcurrent, &g.MaxConcurrent)
		setInt(o.GNMI.PerHost, &g.PerHost)
		setSeconds(o.GNMI.TimeoutS, &g.Timeout)
		setBool(o.GNMI.Stream, &g.Stream)
	}
	if o.SNMPTrap != nil {
		t := &cfg.Protocols.SNMPTrap
		setBool(o.SNMPTrap.Enabled, &t.Enabled)
		setString(o.SNMPTrap.Listen, &t.Listen)
		setInt(o.SNMPTrap.Workers, &t.Workers)
		setInt(o.SNMPTrap.RateLimitPerMinute, &t.RateLimitPerMinute)
	}
	if o.RedfishEvent != nil {
		e := &cfg.Protocols.RedfishEvent
		setBool(o.RedfishEvent.Enabled, &e.Enabled)
		setString(o.RedfishEvent.Listen, &e.Listen)
		setString(o.RedfishEvent.Advertise, &e.Advertise)
		setBool(o.RedfishEvent.TLS, &e.TLS)
	}
}

// TrapChanged reports whether the trap block differs from what is running.
//
// The trap receiver is the one thing that can be moved without a restart, so
// this is the question the apply loop asks on every config change.
func (o Overrides) TrapChanged(running TrapCfg) bool {
	if o.SNMPTrap == nil {
		return false
	}
	want := running
	setBool(o.SNMPTrap.Enabled, &want.Enabled)
	setString(o.SNMPTrap.Listen, &want.Listen)
	setInt(o.SNMPTrap.Workers, &want.Workers)
	setInt(o.SNMPTrap.RateLimitPerMinute, &want.RateLimitPerMinute)
	return want != running
}

// TrapConfig is the trap block with the overrides folded in.
func (o Overrides) TrapConfig(running TrapCfg) TrapCfg {
	if o.SNMPTrap == nil {
		return running
	}
	out := running
	setBool(o.SNMPTrap.Enabled, &out.Enabled)
	setString(o.SNMPTrap.Listen, &out.Listen)
	setInt(o.SNMPTrap.Workers, &out.Workers)
	setInt(o.SNMPTrap.RateLimitPerMinute, &out.RateLimitPerMinute)
	return out
}

// RestartPending reports whether the overrides ask for anything this process
// cannot pick up while running.
//
// Everything except the trap block: adapters read their concurrency, timeouts
// and ports once, when they are built. Saying so is the whole point - a
// settings page that reports a stored value as a live one is lying about the
// estate.
func (o Overrides) RestartPending(booted *Config) bool {
	want := *booted
	o.Apply(&want)
	want.Protocols.SNMPTrap = booted.Protocols.SNMPTrap // applied live
	return want.Protocols != booted.Protocols
}

func applyProtocol(o *ProtocolOverride, p *ProtocolCfg) {
	if o == nil {
		return
	}
	setBool(o.Enabled, &p.Enabled)
	setInt(o.MaxConcurrent, &p.MaxConcurrent)
	setInt(o.PerHost, &p.PerHost)
	setSeconds(o.TimeoutS, &p.Timeout)
	setInt(o.Retries, &p.Retries)
}

func setBool(v *bool, dst *bool) {
	if v != nil {
		*dst = *v
	}
}

func setInt(v *int, dst *int) {
	if v != nil {
		*dst = *v
	}
}

func setString(v *string, dst *string) {
	if v != nil && *v != "" {
		*dst = *v
	}
}

func setSeconds(v *int, dst *time.Duration) {
	if v != nil {
		*dst = time.Duration(*v) * time.Second
	}
}
