package discovery

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"testing"
)

// fakeBMC serves the shape a real one does: an unauthenticated service root and a
// 401 on everything else until credentials arrive.
func fakeBMC(t *testing.T, user, pass string) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/redfish/v1/", func(w http.ResponseWriter, r *http.Request) {
		// Only the root is anonymous. Anything deeper needs auth, which is what the
		// specification says and what makes credential-free detection possible.
		if r.URL.Path != "/redfish/v1/" {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"@odata.id":      "/redfish/v1/",
			"@odata.type":    "#ServiceRoot.v1_5_0.ServiceRoot",
			"Id":             "RootService",
			"Name":           "Root Service",
			"RedfishVersion": "1.6.0",
		})
	})
	guard := func(h http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			u, p, ok := r.BasicAuth()
			if !ok || u != user || p != pass {
				w.WriteHeader(http.StatusUnauthorized)
				return
			}
			h(w, r)
		}
	}
	mux.HandleFunc("/redfish/v1/Systems", guard(
		func(w http.ResponseWriter, _ *http.Request) {
			_ = json.NewEncoder(w).Encode(map[string]any{
				// Deliberately not "1": the member id is vendor-specific, and a
				// probe that assumed one would 404 on most of an estate.
				"Members": []map[string]string{
					{"@odata.id": "/redfish/v1/Systems/System.Embedded.1"},
				},
			})
		}))
	mux.HandleFunc("/redfish/v1/Systems/System.Embedded.1", guard(
		func(w http.ResponseWriter, _ *http.Request) {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"Manufacturer": "Dell Inc.",
				"Model":        "PowerEdge R7525",
				"SerialMumber": "ignored typo field",
				"SerialNumber": "  abc123  ",
				"UUID":         "4c4c4544-0042-1010",
				"HostName":     "srv-77",
				"PowerState":   "On",
			})
		}))
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

func hostPort(t *testing.T, raw string) (string, uint16) {
	t.Helper()
	u, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	p, err := strconv.Atoi(u.Port())
	if err != nil {
		t.Fatal(err)
	}
	return u.Hostname(), uint16(p)
}

func TestARootAnswersWithoutCredentials(t *testing.T) {
	// The whole reason sweeping for BMCs is possible: a controller identifies
	// itself to an anonymous caller. Without this, discovery would need a
	// credential for every address it had never spoken to.
	srv := fakeBMC(t, "root", "calvin")
	host, port := hostPort(t, srv.URL)
	s := &RedfishSweeper{Ports: []uint16{port}, AllowPlaintext: true}

	got := s.Sweep(context.Background(), []string{host})
	if len(got) != 1 {
		t.Fatalf("responders = %d, want 1", len(got))
	}
	if got[0].Protocol != "redfish" {
		t.Errorf("protocol = %q, want redfish", got[0].Protocol)
	}
	if got[0].Identity["redfishVersion"] != "1.6.0" {
		t.Errorf("version = %q", got[0].Identity["redfishVersion"])
	}
	// No credentials were configured, so no serial - and that must not stop the
	// BMC being reported. A found controller with a thin identity still tells an
	// operator something is there.
	if _, ok := got[0].Identity["serial"]; ok {
		t.Error("a serial appeared with no credentials configured")
	}
}

func TestCredentialsFetchTheChassisIdentity(t *testing.T) {
	srv := fakeBMC(t, "root", "calvin")
	host, port := hostPort(t, srv.URL)
	s := &RedfishSweeper{
		Ports: []uint16{port}, AllowPlaintext: true,
		// The wrong one first, so the list is genuinely tried in order.
		Credentials: []RedfishCredential{
			{Username: "root", Password: "wrong"},
			{Username: "root", Password: "calvin"},
		},
	}

	got := s.Sweep(context.Background(), []string{host})
	if len(got) != 1 {
		t.Fatalf("responders = %d, want 1", len(got))
	}
	id := got[0].Identity
	// Trimmed: gear reports its own serial inconsistently, and a match that fails
	// on whitespace looks like a new device. (Upper-casing is the API's job, so it
	// is deliberately NOT done here.)
	if id["serial"] != "abc123" {
		t.Errorf("serial = %q, want the trimmed value", id["serial"])
	}
	if id["vendor"] != "Dell Inc." || id["model"] != "PowerEdge R7525" {
		t.Errorf("identity = %v", id)
	}
	if id["hostName"] != "srv-77" {
		t.Errorf("hostName = %q", id["hostName"])
	}
}

func TestTheSystemsMemberIdIsNotAssumed(t *testing.T) {
	// /redfish/v1/Systems/1 is an HPE-ism. Dell uses System.Embedded.1 and this
	// estate's simulator uses the device's own id, so a hard-coded path would find
	// the serial on some vendors and 404 on the rest. The fake serves the Dell
	// shape; finding the serial at all proves the collection was followed.
	srv := fakeBMC(t, "root", "calvin")
	host, port := hostPort(t, srv.URL)
	s := &RedfishSweeper{
		Ports: []uint16{port}, AllowPlaintext: true,
		Credentials: []RedfishCredential{{Username: "root", Password: "calvin"}},
	}

	got := s.Sweep(context.Background(), []string{host})
	if len(got) != 1 || got[0].Identity["serial"] == "" {
		t.Fatalf("the Systems collection was not followed: %v", got)
	}
}

func TestAWebServerOnThePortIsNotReportedAsABMC(t *testing.T) {
	// A 200 with JSON that is not a service root is a web UI, a proxy or a load
	// balancer. RedfishVersion is the field the specification requires, so it is
	// the honest test - and reporting a load balancer as a management controller
	// would put a fictional device in front of an operator.
	srv := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, _ *http.Request) {
			_, _ = w.Write([]byte(`{"hello":"world"}`))
		}))
	t.Cleanup(srv.Close)
	host, port := hostPort(t, srv.URL)
	s := &RedfishSweeper{Ports: []uint16{port}, AllowPlaintext: true}

	if got := s.Sweep(context.Background(), []string{host}); len(got) != 0 {
		t.Errorf("responders = %d, want 0 - that is not a BMC", len(got))
	}
}

func TestPlaintextIsNotTriedUnlessAllowed(t *testing.T) {
	// A BMC serving Redfish in the clear is a finding rather than something to
	// accommodate, so a real site leaves this off - and with it off the sweep must
	// not quietly fall back to http://.
	srv := fakeBMC(t, "root", "calvin")
	host, port := hostPort(t, srv.URL)
	s := &RedfishSweeper{Ports: []uint16{port}, AllowPlaintext: false}

	if got := s.Sweep(context.Background(), []string{host}); len(got) != 0 {
		t.Errorf("responders = %d, want 0 - plaintext was not allowed", len(got))
	}
}

func TestAnAbsentFieldIsNotAnEmptyString(t *testing.T) {
	// An empty value would read downstream as "the device said nothing here"
	// rather than "it did not say", and an empty serial matches nothing while
	// looking like a key.
	m := map[string]string{}
	put(m, "serial", "   ")
	put(m, "vendor", "Dell")
	if _, ok := m["serial"]; ok {
		t.Error("a blank value was stored")
	}
	if m["vendor"] != "Dell" {
		t.Errorf("vendor = %q", m["vendor"])
	}
}

func TestTheRootPathIsTheSpecifiedOne(t *testing.T) {
	// Trailing slash included: some implementations 404 /redfish/v1 and answer
	// /redfish/v1/, which is what the specification names.
	src := redfishRootPath
	if !strings.HasSuffix(src, "/redfish/v1/") {
		t.Errorf("root path = %q", src)
	}
}
