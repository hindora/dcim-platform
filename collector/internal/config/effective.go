package config

import (
	"encoding/json"
	"time"
)

// Effective reports what this process is actually configured to do.
//
// Sent on the heartbeat because nothing else can answer the question. The
// defaults live in collector.yaml on this host; the platform's database holds
// only what somebody overrode, which on a fresh install is nothing. A settings
// page built from the override document alone shows every field empty and
// tells an operator nothing about what the collector is doing - which is how
// the first version of that page shipped.
//
// The shape matches the platform's config schema exactly, key for key, so the
// form can put a real value in front of every field without a translation
// table that would drift from one side or the other.
func Effective(cfg *Config, trap TrapCfg) string {
	out := map[string]map[string]any{
		"snmp":    protocolMap(cfg.Protocols.SNMP),
		"redfish": protocolMap(cfg.Protocols.Redfish),
		"modbus":  protocolMap(cfg.Protocols.Modbus),
		"bacnet": merge(map[string]any{
			"enabled":        cfg.Protocols.BACnet.Enabled,
			"max_concurrent": cfg.Protocols.BACnet.MaxConcurrent,
			"per_host":       cfg.Protocols.BACnet.PerHost,
			"timeout_s":      seconds(cfg.Protocols.BACnet.Timeout),
			"retries":        cfg.Protocols.BACnet.Retries,
		}, map[string]any{"local_port": cfg.Protocols.BACnet.LocalPort}),
		"gnmi": merge(map[string]any{
			"enabled":        cfg.Protocols.GNMI.Enabled,
			"max_concurrent": cfg.Protocols.GNMI.MaxConcurrent,
			"per_host":       cfg.Protocols.GNMI.PerHost,
			"timeout_s":      seconds(cfg.Protocols.GNMI.Timeout),
			// No retries: a gNMI Get is one RPC over a connection the pool
			// manages, and a Subscribe reconnects rather than retrying. The
			// config has no such field, so offering one would store a setting
			// nothing reads.
		}, map[string]any{"stream": cfg.Protocols.GNMI.Stream}),
		// The RUNNING trap block, not the booted one. It is the only part of
		// the collector that can be moved without a restart, so the file's
		// value would be wrong the moment somebody moves it.
		"snmp_trap": {
			"enabled":               trap.Enabled,
			"listen":                trap.Listen,
			"workers":               trap.Workers,
			"rate_limit_per_minute": trap.RateLimitPerMinute,
		},
		"redfish_event": {
			"enabled":   cfg.Protocols.RedfishEvent.Enabled,
			"listen":    cfg.Protocols.RedfishEvent.Listen,
			"advertise": cfg.Protocols.RedfishEvent.Advertise,
			"tls":       cfg.Protocols.RedfishEvent.TLS,
		},
	}
	encoded, err := json.Marshal(out)
	if err != nil {
		return ""
	}
	return string(encoded)
}

func protocolMap(p ProtocolCfg) map[string]any {
	return map[string]any{
		"enabled":        p.Enabled,
		"max_concurrent": p.MaxConcurrent,
		"per_host":       p.PerHost,
		"timeout_s":      seconds(p.Timeout),
		"retries":        p.Retries,
	}
}

func merge(base, extra map[string]any) map[string]any {
	for k, v := range extra {
		base[k] = v
	}
	return base
}

// seconds rounds up, because the schema speaks whole seconds and a 500 ms
// timeout reported as 0 would read as "no timeout at all".
func seconds(d time.Duration) int {
	if d <= 0 {
		return 0
	}
	if d < time.Second {
		return 1
	}
	return int(d.Round(time.Second) / time.Second)
}
