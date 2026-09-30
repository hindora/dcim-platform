package assign

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/hari/dcim-platform/collector/internal/config"
	"github.com/hari/dcim-platform/collector/internal/obs"
)

// docs/26 Phase 5: pool settings ride the assignment. This pins the wire
// shape services/collector.build_assignment produces - the same JSON key
// names on both sides, so a pool's BBMD actually reaches reconcileFDR
// rather than decoding to a zero value nobody notices.
func TestRefreshCarriesPoolSettingsAndTheEndpointsPoolID(t *testing.T) {
	body := `{
		"version": 7, "generated_at": "2026-01-01T00:00:00Z", "collector_id": "col-1",
		"site": "DC1", "endpoints": [{
			"id": "ep-1", "device_id": "dev-1", "device_name": "dev-1",
			"device_type": "chiller", "protocol": "bacnet", "role": "native_card",
			"address": "10.52.1.20", "port": 47808, "addressing": {"instance": 2001},
			"via_endpoint_id": "", "pool_id": "pool-bms",
			"credential": null,
			"poll": {"interval_s": 60, "timeout_ms": 3000, "retries": 2,
				"metric_groups": [], "push_enabled": false}
		}], "resolve": [],
		"pools": {
			"pool-bms": {"id": "pool-bms", "name": "DC1/BMS", "site": "DC1", "plane": "bms",
				"trap_vip": "10.52.1.250",
				"bbmd": {"enabled": true, "bbmd": "10.52.1.1:47808", "ttl_s": 120},
				"rate_budget_points_per_s": 400},
			"pool-oob": {"id": "pool-oob", "name": "DC1/IT-OOB", "site": "DC1", "plane": "it_oob",
				"trap_vip": null, "bbmd": {"enabled": false, "bbmd": null, "ttl_s": 300},
				"rate_budget_points_per_s": null}
		}
	}`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(body))
	}))
	defer server.Close()

	cfg := config.Default()
	cfg.Collector.ID = "col-1"
	cfg.DCIM.BaseURL = server.URL
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	client := New(cfg, log, obs.NewMetrics(), nil, nil)
	if err := client.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh: %v", err)
	}

	eps := client.Endpoints()
	if len(eps) != 1 || eps[0].PoolID != "pool-bms" {
		t.Fatalf("endpoint pool_id did not decode: %+v", eps)
	}

	pools := client.Pools()
	if len(pools) != 2 {
		t.Fatalf("got %d pools, want 2", len(pools))
	}
	bms := pools["pool-bms"]
	if !bms.BBMD.Enabled || bms.BBMD.Addr != "10.52.1.1:47808" || bms.BBMD.TTLSeconds != 120 {
		t.Fatalf("bms pool BBMD decoded as %+v", bms.BBMD)
	}
	if bms.TrapVIP != "10.52.1.250" || bms.Plane != "bms" {
		t.Fatalf("bms pool decoded as %+v", bms)
	}
	if bms.RateBudgetPointsPerS == nil || *bms.RateBudgetPointsPerS != 400 {
		t.Fatalf("rate budget decoded as %v", bms.RateBudgetPointsPerS)
	}
	oob := pools["pool-oob"]
	if oob.BBMD.Enabled || oob.TrapVIP != "" || oob.RateBudgetPointsPerS != nil {
		t.Fatalf("null fields must decode to zero values, got %+v", oob)
	}

	// A copy: mutating what Pools() returned must not reach the client.
	pools["pool-bms"] = Pool{}
	if client.Pools()["pool-bms"].Name != "DC1/BMS" {
		t.Fatal("Pools() handed out the client's own map")
	}
}

func TestRefreshWithNoPoolsKeyLeavesPoolsEmpty(t *testing.T) {
	// A platform from before the pools API sends no "pools" key at all.
	body := `{"version": 1, "generated_at": "2026-01-01T00:00:00Z",
		"collector_id": "col-1", "site": "", "endpoints": [], "resolve": []}`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(body))
	}))
	defer server.Close()

	cfg := config.Default()
	cfg.Collector.ID = "col-1"
	cfg.DCIM.BaseURL = server.URL
	client := New(cfg, slog.New(slog.NewTextHandler(io.Discard, nil)), obs.NewMetrics(), nil, nil)
	if err := client.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh: %v", err)
	}
	if n := len(client.Pools()); n != 0 {
		t.Fatalf("got %d pools, want none", n)
	}
}
