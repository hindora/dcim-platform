package modbus

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

// Live tests against a running device plane. Skipped unless DCIM_LIVE_MODBUS
// names targets, so the normal suite stays hermetic:
//
//	DCIM_LIVE_MODBUS="utility_feed@10.52.14.47,sensor@10.52.14.19/1/chw_supply" \
//	  go test ./internal/adapters/modbus/ -run Live -v
//
// Target syntax: <device_type>@<ip>[/<unit_id>[/<probe_role>]][:<port>]
//
// A fake proves the adapter is self-consistent. Only a real device proves the
// template is right - and on Modbus a wrong template does not fail, it returns
// numbers. Checking the values against the registry's bounds is therefore part
// of the test, not decoration.

type liveTarget struct {
	deviceType string
	ip         string
	port       int
	unit       int
	probeRole  string
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
			t.Fatalf("bad target %q", raw)
		}
		tgt := liveTarget{deviceType: dtype, unit: 1}
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
		if len(parts) > 1 {
			tgt.unit, _ = strconv.Atoi(parts[1])
		}
		if len(parts) > 2 {
			tgt.probeRole = parts[2]
		}
		out = append(out, tgt)
	}
	return out
}

func TestLiveElectricalPoll(t *testing.T) {
	spec := os.Getenv("DCIM_LIVE_MODBUS")
	if spec == "" {
		t.Skip("set DCIM_LIVE_MODBUS to run against a live device plane")
	}
	targets := parseLiveTargets(t, spec)

	client := NewClient(4*time.Second, 1, testLogger())
	a := New(loadMaps(t), client, testLogger(), obs.NewMetrics())
	defer func() { _ = a.Close(context.Background()) }()

	for _, tgt := range targets {
		name := tgt.deviceType + "@" + tgt.ip
		if tgt.probeRole != "" {
			name += "/" + tgt.probeRole
		}
		t.Run(name, func(t *testing.T) {
			ep := &models.Endpoint{
				ID: "live-" + name, DeviceID: "live-dev", DeviceType: tgt.deviceType,
				Protocol: "modbus", Address: tgt.ip, Port: tgt.port,
				Role:       "native_card",
				Addressing: map[string]any{"unit_id": tgt.unit},
				Poll:       models.PollProfile{IntervalS: 30, TimeoutMs: 4000},
			}
			if tgt.probeRole != "" {
				ep.Addressing["probe_role"] = tgt.probeRole
				ep.Role = "field_device"
			}

			ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
			defer cancel()

			out, err := a.Poll(ctx, ep)
			if err != nil {
				t.Fatalf("poll: %v", err)
			}
			if len(out.Samples) == 0 {
				t.Fatal("no samples from a live device")
			}

			a.mu.Lock()
			profile := a.verified[ep.ID]
			a.mu.Unlock()
			t.Logf("template %s (%s, word order %s), identity %q",
				profile.template.MapID, profile.template.Product,
				profile.template.WordOrder, profile.identity.Product)

			lines := make([]string, 0, len(out.Samples))
			for _, s := range out.Samples {
				key := s.Metric
				if s.Instance != "" {
					key += "{" + s.Instance + "}"
				}
				val := strconv.FormatFloat(s.DoubleValue, 'f', 3, 64) + " " + s.Unit
				if s.ValueType == models.ValueTypeText {
					val = s.TextValue
				}
				lines = append(lines, key+" = "+val+"  <- "+s.Metadata["point"]+
					" @ "+s.Metadata["register"])
			}
			sort.Strings(lines)
			for _, l := range lines {
				t.Log(l)
			}
			for _, m := range out.Misses {
				t.Logf("MISS %s (%s)", m.Metric, m.Reason)
			}

			// A wrong word order or a missed scale factor still produces a
			// number. The registry's bounds are what catch it.
			for _, s := range out.Samples {
				if s.ValueType == models.ValueTypeText {
					continue
				}
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
