// Package preflight is `dcim-collector preflight` (docs/26 Phase 8): a
// one-shot self-check that runs standalone, or automatically once after a
// successful `enroll`, and posts what it found to the platform so the
// onboarding wizard the frontend half of this phase will build can show
// "did this install actually work" without anyone reading a log file.
package preflight

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"time"

	"github.com/hari/dcim-platform/collector/internal/config"
)

// Status values, matching backend/app/schemas.PreflightCheck exactly - the
// three-language sync this project's own conventions require for any wire
// field (see feedback_adding_a_metric_key).
const (
	StatusOK      = "ok"
	StatusWarn    = "warn"
	StatusFail    = "fail"
	StatusSkipped = "skipped"
)

// Thresholds. Conservative rather than exact: preflight exists to catch a
// host that is badly wrong, not to be the platform's ongoing clock-drift
// or disk-usage monitor - those are separate, ongoing concerns, this is a
// one-time "is this install sane" gate.
const (
	// SNMPv3's own USM time window (RFC 3414) is +/-150s by default on
	// most agents; warning at a tenth of that and failing at half of it
	// catches a real clock problem well before it silently breaks v3 auth.
	ntpWarnOffset = 15 * time.Second
	ntpFailOffset = 75 * time.Second

	// Below this, the spool (72h/40GB default budget - internal/spool)
	// cannot hold even a fraction of a real WAN partition before shedding
	// starts immediately instead of after hours of buffering.
	diskWarnBytes = 20 << 30 // 20 GiB
	diskFailBytes = 4 << 30  // 4 GiB
)

// Check is one finding, matching backend/app/schemas.PreflightCheck.
type Check struct {
	Check  string   `json:"check"`
	Status string   `json:"status"`
	Detail string   `json:"detail,omitempty"`
	Value  *float64 `json:"value,omitempty"`
}

func val(f float64) *float64 { return &f }

// Run executes every check this package knows and returns the full set,
// in a fixed order - never partial, even when an individual check errors:
// an error becomes a "fail" Check, not a return that drops every check
// after it.
func Run(ctx context.Context, cfg *config.Config, httpClient *http.Client) []Check {
	ctx, cancel := context.WithTimeout(ctx, cfg.Preflight.Timeout)
	defer cancel()

	return []Check{
		checkNTP(ctx, cfg),
		checkSpoolDisk(cfg),
		checkTrapPort(cfg),
		checkCoreReachable(ctx, cfg, httpClient),
	}
}

func checkNTP(ctx context.Context, cfg *config.Config) Check {
	offset, err := ntpOffset(ctx, cfg.Preflight.NTPServer, cfg.Preflight.Timeout)
	if err != nil {
		return Check{Check: "ntp_offset", Status: StatusWarn,
			Detail: fmt.Sprintf("could not query %s: %v - clock accuracy unknown",
				cfg.Preflight.NTPServer, err)}
	}
	abs := offset
	if abs < 0 {
		abs = -abs
	}
	ms := float64(offset) / float64(time.Millisecond)
	switch {
	case abs > ntpFailOffset:
		return Check{Check: "ntp_offset", Status: StatusFail, Value: val(ms),
			Detail: fmt.Sprintf("clock is %v off from %s - SNMPv3 authentication "+
				"and TLS certificate validation will likely fail", offset,
				cfg.Preflight.NTPServer)}
	case abs > ntpWarnOffset:
		return Check{Check: "ntp_offset", Status: StatusWarn, Value: val(ms),
			Detail: fmt.Sprintf("clock is %v off from %s - correct this before "+
				"relying on SNMPv3", offset, cfg.Preflight.NTPServer)}
	default:
		return Check{Check: "ntp_offset", Status: StatusOK, Value: val(ms),
			Detail: fmt.Sprintf("%v off %s", offset, cfg.Preflight.NTPServer)}
	}
}

func checkSpoolDisk(cfg *config.Config) Check {
	if cfg.Transport.Mode != "gateway" {
		return Check{Check: "spool_disk", Status: StatusSkipped,
			Detail: "transport.mode is not \"gateway\" - this collector has no spool"}
	}
	if err := os.MkdirAll(cfg.Transport.Gateway.SpoolDir, 0o700); err != nil {
		return Check{Check: "spool_disk", Status: StatusFail,
			Detail: fmt.Sprintf("could not create %s: %v", cfg.Transport.Gateway.SpoolDir, err)}
	}
	free, err := freeBytes(cfg.Transport.Gateway.SpoolDir)
	if err != nil {
		return Check{Check: "spool_disk", Status: StatusWarn,
			Detail: fmt.Sprintf("could not read free space on %s: %v",
				cfg.Transport.Gateway.SpoolDir, err)}
	}
	status, reason := classifyDiskSpace(free)
	gb := float64(free) / float64(1<<30)
	return Check{Check: "spool_disk", Status: status, Value: val(gb),
		Detail: fmt.Sprintf("%.1f GiB free on %s%s", gb, cfg.Transport.Gateway.SpoolDir, reason)}
}

// classifyDiskSpace is separated from checkSpoolDisk purely so it can be
// tested against fixed byte counts - free disk space on whatever machine
// actually runs the test suite is not something a test should depend on.
func classifyDiskSpace(free uint64) (status, reason string) {
	switch {
	case free < diskFailBytes:
		return StatusFail, " - a WAN partition will shed almost immediately"
	case free < diskWarnBytes:
		return StatusWarn, " - less than the spool's own 40 GB default budget"
	default:
		return StatusOK, ""
	}
}

func checkTrapPort(cfg *config.Config) Check {
	if !cfg.Protocols.SNMPTrap.Enabled {
		return Check{Check: "trap_port", Status: StatusSkipped,
			Detail: "protocols.snmp_trap.enabled is false"}
	}
	if err := trapPortBindable(cfg.Protocols.SNMPTrap.Listen); err != nil {
		return Check{Check: "trap_port", Status: StatusFail,
			Detail: err.Error()}
	}
	return Check{Check: "trap_port", Status: StatusOK,
		Detail: fmt.Sprintf("%s is bindable", cfg.Protocols.SNMPTrap.Listen)}
}

func checkCoreReachable(ctx context.Context, cfg *config.Config, client *http.Client) Check {
	if err := coreReachable(ctx, client, cfg.DCIM.BaseURL); err != nil {
		return Check{Check: "core_tls", Status: StatusFail, Detail: err.Error()}
	}
	return Check{Check: "core_tls", Status: StatusOK,
		Detail: fmt.Sprintf("reached %s", cfg.DCIM.BaseURL)}
}

// Post sends results to POST /api/v1/collector/preflight - collector-
// scoped, the same auth (bearer token, or the client's own presented mTLS
// certificate) as every other route under /collector.
func Post(ctx context.Context, client *http.Client, baseURL, token string, checks []Check) error {
	body, err := json.Marshal(map[string]any{"checks": checks})
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		baseURL+"/api/v1/collector/preflight", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("POST %s/api/v1/collector/preflight: %w", baseURL, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		return fmt.Errorf("POST %s/api/v1/collector/preflight: %s", baseURL, resp.Status)
	}
	return nil
}
