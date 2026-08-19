package bacnet

import (
	"context"
	"os"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// Live tests against a running device plane. Skipped unless DCIM_LIVE_BACNET
// names targets, so the normal suite stays hermetic:
//
//	DCIM_LIVE_BACNET="chiller@10.52.14.12,energy_monitor@10.52.11.20,pump@10.52.14.15/2001/1" \
//	  go test ./internal/adapters/bacnet/ -run Live -v
//
// A fake proves the adapter is self-consistent. Only a real device proves the
// wire format is right - the failure a fake cannot show is the one where both
// sides of our own code agree on something the standard does not say.
//
// Target syntax: <device_type>@<ip>[/<network>/<mac>][:<port>]

type liveTarget struct {
	deviceType string
	ip         string
	port       int
	network    int
	mac        int
}

func parseLiveTargets(t *testing.T, spec string) []liveTarget {
	t.Helper()
	var out []liveTarget
	for _, raw := range strings.Split(spec, ",") {
		raw = strings.TrimSpace(raw)
		if raw == "" {
			continue
		}
		dtype, rest, ok := strings.Cut(raw, "@")
		if !ok {
			t.Fatalf("bad target %q, want <device_type>@<ip>[/net/mac][:port]", raw)
		}
		tgt := liveTarget{deviceType: dtype}
		if addr, portStr, ok := strings.Cut(rest, ":"); ok {
			rest = addr
			p, err := strconv.Atoi(portStr)
			if err != nil {
				t.Fatalf("bad port in %q", raw)
			}
			tgt.port = p
		}
		parts := strings.Split(rest, "/")
		tgt.ip = parts[0]
		if len(parts) == 3 {
			tgt.network, _ = strconv.Atoi(parts[1])
			tgt.mac, _ = strconv.Atoi(parts[2])
		}
		out = append(out, tgt)
	}
	return out
}

func TestLivePlantPoll(t *testing.T) {
	spec := os.Getenv("DCIM_LIVE_BACNET")
	if spec == "" {
		t.Skip("set DCIM_LIVE_BACNET to run against a live device plane")
	}
	targets := parseLiveTargets(t, spec)

	client := NewClient(0, 4*time.Second, 2, testLogger())
	a := New(loadMap(t), client, testLogger(), obs.NewMetrics(), 12)
	if err := a.Init(context.Background()); err != nil {
		t.Fatalf("open socket: %v", err)
	}
	defer func() { _ = a.Close(context.Background()) }()

	for _, tgt := range targets {
		name := tgt.deviceType + "@" + tgt.ip
		if tgt.network != 0 {
			name += "/mstp"
		}
		t.Run(name, func(t *testing.T) {
			ep := &models.Endpoint{
				ID: "live-" + name, DeviceID: "live-dev", DeviceType: tgt.deviceType,
				Protocol: "bacnet", Address: tgt.ip, Port: tgt.port,
				// Deliberately no device_instance: the directed Who-Is has to
				// find it, which is how a real commissioning run works.
				Addressing: map[string]any{},
				Poll:       models.PollProfile{IntervalS: 30, TimeoutMs: 4000},
			}
			if tgt.network != 0 {
				ep.Addressing["network"] = tgt.network
				ep.Addressing["mac"] = tgt.mac
			}

			ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
			defer cancel()

			out, err := a.Poll(ctx, ep)
			if err != nil {
				t.Fatalf("poll: %v", err)
			}
			if len(out.Samples) == 0 {
				t.Fatal("no samples from a live device")
			}

			a.mu.Lock()
			profile := a.discovery[ep.ID]
			a.mu.Unlock()
			t.Logf("device instance %d: %d mapped points, %d unmapped objects",
				profile.deviceObj.Instance, len(profile.points), profile.unmapped)

			lines := make([]string, 0, len(out.Samples))
			for _, s := range out.Samples {
				key := s.Metric
				if s.Instance != "" {
					key += "{" + s.Instance + "}"
				}
				lines = append(lines, key+" = "+
					strconv.FormatFloat(s.DoubleValue, 'f', 3, 64)+" "+s.Unit+
					"  <- "+s.Metadata["point"])
			}
			sort.Strings(lines)
			for _, l := range lines {
				t.Log(l)
			}
			for _, m := range out.Misses {
				t.Logf("MISS %s (%s)", m.Metric, m.Reason)
			}

			// Values have to be plausible, not merely present. A decoder that
			// misreads a REAL still returns a number, and a number nobody
			// sanity-checks is how a broken codec ships.
			for _, s := range out.Samples {
				def, ok := models.ValidateMetric(s.Metric)
				if !ok {
					t.Errorf("%s is not in the registry", s.Metric)
					continue
				}
				if def.HasMin && s.DoubleValue < def.MinValid {
					t.Errorf("%s = %v, below the registry minimum %v",
						s.Metric, s.DoubleValue, def.MinValid)
				}
				if def.HasMax && s.DoubleValue > def.MaxValid {
					t.Errorf("%s = %v, above the registry maximum %v",
						s.Metric, s.DoubleValue, def.MaxValid)
				}
			}
		})
	}
}
