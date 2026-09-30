package provider

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

func newAdapter(t *testing.T) *Adapter {
	t.Helper()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	return New(log, obs.NewMetrics(), 0)
}

// fakeProvider serves both the OAuth2 token endpoint and the cabinet
// telemetry endpoint from one httptest.Server, the way a real provider's API
// gateway would front both under one host.
func fakeProvider(t *testing.T, cabinetID string, body map[string]any) *httptest.Server {
	t.Helper()
	var mux http.ServeMux
	mux.HandleFunc("/oauth2/token", func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"access_token": "tok", "expires_in": 3600,
		})
	})
	mux.HandleFunc("/v1/cabinets/"+cabinetID+"/telemetry", func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer tok" {
			t.Errorf("Authorization = %q, want Bearer tok", got)
		}
		_ = json.NewEncoder(w).Encode(body)
	})
	srv := httptest.NewServer(&mux)
	t.Cleanup(srv.Close)
	return srv
}

func endpoint(srv *httptest.Server, cabinetID string) *models.Endpoint {
	return &models.Endpoint{
		ID: "ep-1", DeviceID: "dev-1", Protocol: "provider",
		Address:    srv.URL,
		Addressing: map[string]any{"cabinet_id": cabinetID},
		Credential: &models.Credential{Data: map[string]any{
			"client_id": "cid", "client_secret": "secret",
			"token_url": srv.URL + "/oauth2/token",
		}},
	}
}

func TestPollMapsAllThreeFieldsToSamples(t *testing.T) {
	srv := fakeProvider(t, "cab-1", map[string]any{
		"power_w": 1450.2, "temperature_c": 22.4, "humidity_pct": 41.0,
	})
	a := newAdapter(t)
	out, err := a.Poll(context.Background(), endpoint(srv, "cab-1"))
	if err != nil {
		t.Fatalf("Poll: %v", err)
	}
	if len(out.Samples) != 3 {
		t.Fatalf("got %d samples, want 3", len(out.Samples))
	}
	byMetric := map[string]models.Telemetry{}
	for _, s := range out.Samples {
		byMetric[s.Metric] = s
		if s.SourceProtocol != models.ProtocolProvider {
			t.Errorf("sample %s: source_protocol = %v, want ProtocolProvider",
				s.Metric, s.SourceProtocol)
		}
	}
	if byMetric["power_draw"].DoubleValue != 1450.2 {
		t.Errorf("power_draw = %v, want 1450.2", byMetric["power_draw"].DoubleValue)
	}
	if byMetric["inlet_temperature"].DoubleValue != 22.4 {
		t.Errorf("inlet_temperature = %v, want 22.4", byMetric["inlet_temperature"].DoubleValue)
	}
	if byMetric["relative_humidity"].DoubleValue != 41.0 {
		t.Errorf("relative_humidity = %v, want 41.0", byMetric["relative_humidity"].DoubleValue)
	}
}

func TestPollReportsAMissForAnAbsentField(t *testing.T) {
	srv := fakeProvider(t, "cab-2", map[string]any{"power_w": 900.0})
	a := newAdapter(t)
	out, err := a.Poll(context.Background(), endpoint(srv, "cab-2"))
	if err != nil {
		t.Fatalf("Poll: %v", err)
	}
	if len(out.Samples) != 1 {
		t.Fatalf("got %d samples, want 1", len(out.Samples))
	}
	if len(out.Misses) != 2 {
		t.Fatalf("got %d misses, want 2 (temperature, humidity)", len(out.Misses))
	}
	if !out.Partial {
		t.Error("Partial should be true when any field is missing")
	}
}

func TestPollFailsCleanlyWithNoRecognisedFields(t *testing.T) {
	srv := fakeProvider(t, "cab-3", map[string]any{"unrelated_field": 1})
	a := newAdapter(t)
	_, err := a.Poll(context.Background(), endpoint(srv, "cab-3"))
	if err == nil {
		t.Fatal("expected an error when nothing in the response maps to a metric")
	}
}

func TestPollFailsWithoutACredential(t *testing.T) {
	a := newAdapter(t)
	ep := &models.Endpoint{ID: "ep-1", Address: "example.invalid",
		Addressing: map[string]any{"cabinet_id": "cab-1"}}
	_, err := a.Poll(context.Background(), ep)
	if err == nil {
		t.Fatal("expected an error for an endpoint with no credential")
	}
}

func TestPollFailsWithoutACabinetIDOrPath(t *testing.T) {
	srv := fakeProvider(t, "cab-1", map[string]any{"power_w": 1})
	a := newAdapter(t)
	ep := endpoint(srv, "cab-1")
	ep.Addressing = map[string]any{} // no cabinet_id, no path
	_, err := a.Poll(context.Background(), ep)
	if err == nil {
		t.Fatal("expected an error for an endpoint with no cabinet_id and no path")
	}
}

func TestPollUsesAFullPathOverrideWhenGiven(t *testing.T) {
	var mux http.ServeMux
	mux.HandleFunc("/oauth2/token", func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"access_token": "tok", "expires_in": 3600})
	})
	mux.HandleFunc("/custom/shape/here", func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"power_w": 5.0})
	})
	srv := httptest.NewServer(&mux)
	defer srv.Close()

	a := newAdapter(t)
	ep := &models.Endpoint{
		ID: "ep-1", Address: srv.URL,
		Addressing: map[string]any{"path": "/custom/shape/here"},
		Credential: &models.Credential{Data: map[string]any{
			"client_id": "cid", "client_secret": "secret",
			"token_url": srv.URL + "/oauth2/token",
		}},
	}
	out, err := a.Poll(context.Background(), ep)
	if err != nil {
		t.Fatalf("Poll: %v", err)
	}
	if len(out.Samples) != 1 {
		t.Fatalf("got %d samples, want 1", len(out.Samples))
	}
}

func TestPollInvalidatesTheTokenOnA401AndSurfacesAnAuthError(t *testing.T) {
	var mux http.ServeMux
	mux.HandleFunc("/oauth2/token", func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"access_token": "tok", "expires_in": 3600})
	})
	mux.HandleFunc("/v1/cabinets/cab-1/telemetry", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	})
	srv := httptest.NewServer(&mux)
	defer srv.Close()

	a := newAdapter(t)
	_, err := a.Poll(context.Background(), endpoint(srv, "cab-1"))
	if err == nil {
		t.Fatal("expected an error for a 401 on the telemetry request")
	}
	if models.ClassifyError(err) != models.ErrClassAuth {
		t.Errorf("error class = %q, want %q", models.ClassifyError(err), models.ErrClassAuth)
	}
}
