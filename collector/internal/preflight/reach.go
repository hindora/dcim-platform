package preflight

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/hari/dcim-platform/collector/internal/adapters/bacnet"
	"github.com/hari/dcim-platform/collector/internal/config"
)

// docs/26 Phase 8's last preflight check: "reachability probes to sample
// addresses of each pool CIDR per enabled protocol". The addresses are real
// devices the platform says this collector owns (or will own, via its pool),
// not hosts picked out of a CIDR - an arbitrary 10.52.1.x not answering proves
// nothing, while a known chiller controller not answering is exactly the
// facilities pinhole that is not in yet, which is what this check exists to
// say in plain words before the first poll does.

// Target is one address to knock on.
type Target struct {
	Address string `json:"address"`
	Port    int    `json:"port"`
}

// ProtocolTargets is the platform's sample for one protocol.
type ProtocolTargets struct {
	Protocol string   `json:"protocol"`
	Targets  []Target `json:"targets"`
	Total    int      `json:"total"`
}

type targetsResponse struct {
	Source    string            `json:"source"`
	Protocols []ProtocolTargets `json:"protocols"`
}

// How each protocol can be probed WITHOUT a credential - preflight runs
// before the collector has been handed any.
//
//   - TCP protocols: a connect. A SYN/ACK is the firewall path proven; it
//     says nothing about the service, but the path is the question here.
//   - BACnet: a directed Who-Is, which every BACnet device must answer
//     with an I-Am and needs no credential. A UDP "connect" would prove
//     nothing at all.
//   - SNMP: skipped. There is no credential-free SNMP exchange a v2c agent
//     is obliged to answer (a wrong community is a silent drop - by design,
//     and indistinguishable from a blocked port), so any result here would
//     be a guess. Its first poll is the honest probe.
var tcpProtocols = map[string]bool{
	"redfish": true, "modbus": true, "gnmi": true, "provider": true,
}

// classifyReach turns probe counts into a status.
func classifyReach(ok, total int) string {
	switch {
	case total == 0:
		return StatusSkipped
	case ok == total:
		return StatusOK
	case ok == 0:
		// Every sample silent: a firewall rule or pinhole, not a device.
		return StatusFail
	default:
		// Some answer: the path is open, a few devices are down.
		return StatusWarn
	}
}

func fetchTargets(ctx context.Context, client *http.Client, baseURL, collectorID,
	token string) (*targetsResponse, error) {

	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		baseURL+"/api/v1/collector/preflight-targets?collector_id="+url.QueryEscape(collectorID), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return nil, fmt.Errorf("%s: %s", resp.Status, strings.TrimSpace(string(body)))
	}
	var out targetsResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("decode targets: %w", err)
	}
	return &out, nil
}

func probeTCP(ctx context.Context, t Target, timeout time.Duration) error {
	d := net.Dialer{Timeout: timeout}
	conn, err := d.DialContext(ctx, "tcp", net.JoinHostPort(t.Address, strconv.Itoa(t.Port)))
	if err != nil {
		return err
	}
	return conn.Close()
}

// checkReachability fetches the platform's sample and probes it. A platform
// that cannot be reached, or has nothing to offer, yields one skipped check
// saying why - never a fabricated pass.
func checkReachability(ctx context.Context, cfg *config.Config, client *http.Client) []Check {
	resp, err := fetchTargets(ctx, client, cfg.DCIM.BaseURL, cfg.Collector.ID, cfg.Token())
	if err != nil {
		return []Check{{Check: "reachability", Status: StatusSkipped,
			Detail: fmt.Sprintf("could not fetch targets from the platform: %v", err)}}
	}
	if resp.Source == "none" || len(resp.Protocols) == 0 {
		return []Check{{Check: "reachability", Status: StatusSkipped,
			Detail: "this collector owns no endpoints and is placed in no pool - nothing to probe yet"}}
	}

	probeTimeout := cfg.Preflight.Timeout
	if probeTimeout <= 0 || probeTimeout > 3*time.Second {
		probeTimeout = 3 * time.Second
	}

	var bc *bacnet.Client
	checks := make([]Check, 0, len(resp.Protocols))
	for _, p := range resp.Protocols {
		name := "reach_" + p.Protocol
		switch {
		case p.Protocol == "snmp":
			checks = append(checks, Check{Check: name, Status: StatusSkipped,
				Detail: "SNMP has no credential-free probe an agent must answer; its first poll is the test"})
			continue
		case p.Protocol == "bacnet":
			if bc == nil {
				bc = bacnet.NewClient(0, probeTimeout, 0,
					slog.New(slog.NewTextHandler(io.Discard, nil)))
				if err := bc.Open(); err != nil {
					checks = append(checks, Check{Check: name, Status: StatusFail,
						Detail: fmt.Sprintf("could not open a BACnet socket: %v", err)})
					bc = nil
					continue
				}
				defer bc.Close()
			}
		case !tcpProtocols[p.Protocol]:
			checks = append(checks, Check{Check: name, Status: StatusSkipped,
				Detail: "no credential-free probe for this protocol"})
			continue
		}
		checks = append(checks, probeProtocol(ctx, p, bc, probeTimeout, resp.Source))
	}
	return checks
}

// probeProtocol probes one protocol's sample concurrently - a fleet-sized
// sample in series would spend the whole preflight budget on timeouts.
func probeProtocol(ctx context.Context, p ProtocolTargets, bc *bacnet.Client,
	timeout time.Duration, source string) Check {

	errs := make([]error, len(p.Targets))
	var wg sync.WaitGroup
	for i, t := range p.Targets {
		wg.Add(1)
		go func(i int, t Target) {
			defer wg.Done()
			pctx, cancel := context.WithTimeout(ctx, timeout)
			defer cancel()
			if p.Protocol == "bacnet" {
				_, errs[i] = bc.Identify(pctx, bacnet.Address{IP: t.Address, Port: t.Port})
			} else {
				errs[i] = probeTCP(pctx, t, timeout)
			}
		}(i, t)
	}
	wg.Wait()

	ok := 0
	var failed []string
	for i, err := range errs {
		if err == nil {
			ok++
			continue
		}
		failed = append(failed, fmt.Sprintf("%s:%d", p.Targets[i].Address, p.Targets[i].Port))
	}
	status := classifyReach(ok, len(p.Targets))
	v := float64(ok)
	detail := fmt.Sprintf("%d of %d sampled %s address(es) answered (of %d %s)",
		ok, len(p.Targets), p.Protocol, p.Total, map[string]string{
			"owned": "owned", "pool": "in this collector's pool"}[source])
	if len(failed) > 0 {
		detail += "; silent: " + strings.Join(failed, ", ")
	}
	if status == StatusFail {
		detail += " - every sample silent points at a firewall rule or pinhole, not at the devices"
	}
	return Check{Check: "reach_" + p.Protocol, Status: status, Value: &v, Detail: detail}
}
