// Redfish discovery: find a BMC, and read its chassis identity if we may.
//
// The property this exploits is in the specification: the service root at
// /redfish/v1/ is UNAUTHENTICATED. A BMC will tell an anonymous caller that it is
// a BMC and which Redfish version it speaks, and refuse everything else with 401.
// So detection needs no credentials at all, which is what makes sweeping an
// unknown subnet for management controllers possible in the first place.
//
// Why it matters here rather than being one more protocol: a server that has been
// racked and not yet built has its BMC up and NO operating system, so it answers
// Redfish and nothing on its production NIC. That is the default state of a device
// added to this simulator, and SNMP-only discovery could not see one unless the
// BMC happened to run an SNMP agent too. A Redfish-only BMC - plenty of real ones
// restrict SNMP - was invisible.
//
// Identity beyond "something is here" does need credentials, and that is the
// spec's design rather than an inconvenience: Manufacturer, Model and above all
// SerialNumber live under /redfish/v1/Systems, which is 401 without auth. The
// serial is what lets a re-addressed machine be recognised instead of promoted a
// second time, so it is worth the credential list.
package discovery

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

// redfishRootPath is the service root the specification names.
//
// Trailing slash included on purpose: some implementations 404 /redfish/v1 and
// answer /redfish/v1/, so the sweep asks for exactly what the spec defines rather
// than relying on a redirect that a BMC may not send.
const redfishRootPath = "/redfish/v1/"

// redfishSystemsPath is the collection to follow for the chassis identity. The
// MEMBER id is never assumed; see chassis().
const redfishSystemsPath = "/redfish/v1/Systems"

// RedfishCredential is one username/password pair to try.
type RedfishCredential struct {
	Username string
	Password string
}

// RedfishSweeper probes addresses for a Redfish service root.
type RedfishSweeper struct {
	// Ports to try, in order. 443 in the real world; this plane serves 8443.
	Ports []uint16
	// AllowPlaintext also tries http:// when https:// does not answer.
	//
	// False for a real site: a BMC that speaks Redfish over plaintext is a finding
	// rather than a thing to accommodate. True here because the simulator's
	// Redfish plane is plain HTTP, and pretending otherwise would mean the sweep
	// could not be exercised at all.
	AllowPlaintext bool
	// Credentials to try for the chassis identity. Detection needs none.
	Credentials []RedfishCredential
	Timeout     time.Duration
	Concurrency int
	Log         *slog.Logger

	client     *http.Client
	clientOnce sync.Once
}

func (s *RedfishSweeper) httpClient() *http.Client {
	s.clientOnce.Do(func() {
		timeout := s.Timeout
		if timeout <= 0 {
			timeout = defaultTimeout
		}
		s.client = &http.Client{
			Timeout: timeout,
			Transport: &http.Transport{
				// A BMC ships with a self-signed certificate for a hostname that
				// is not its address, and replacing that across an estate is a
				// project of its own. Verifying here would mean discovering
				// nothing on any real floor.
				//
				// The exposure is bounded and worth stating: this probe sends NO
				// credentials until a root has answered, reads only the service
				// root anonymously, and the credentialed request that follows goes
				// to an address that has already identified itself as Redfish. It
				// is a sweep of a management network the collector is already on,
				// not a fetch of arbitrary URLs.
				TLSClientConfig: &tls.Config{InsecureSkipVerify: true}, //nolint:gosec
				// One connection per probe, closed after. A sweep touches
				// thousands of addresses once each; a pool would hold file
				// descriptors open for hosts it will never speak to again.
				DisableKeepAlives:   true,
				MaxIdleConnsPerHost: -1,
			},
		}
	})
	return s.client
}

// Sweep probes every address and returns those that answered a Redfish root.
func (s *RedfishSweeper) Sweep(ctx context.Context, addrs []string) []Responder {
	conc := s.Concurrency
	if conc <= 0 {
		conc = defaultConcurrency
	}
	out := make([]Responder, 0, 16)
	var mu sync.Mutex
	sem := make(chan struct{}, conc)
	var wg sync.WaitGroup
	for _, addr := range addrs {
		if ctx.Err() != nil {
			break
		}
		wg.Add(1)
		sem <- struct{}{}
		go func(a string) {
			defer wg.Done()
			defer func() { <-sem }()
			if r, ok := s.probe(ctx, a); ok {
				mu.Lock()
				out = append(out, r)
				mu.Unlock()
			}
		}(addr)
	}
	wg.Wait()
	return out
}

// probe tries each port and scheme until a service root answers.
func (s *RedfishSweeper) probe(ctx context.Context, addr string) (Responder, bool) {
	ports := s.Ports
	if len(ports) == 0 {
		ports = []uint16{443}
	}
	for _, port := range ports {
		schemes := []string{"https"}
		if s.AllowPlaintext {
			schemes = append(schemes, "http")
		}
		for _, scheme := range schemes {
			base := fmt.Sprintf("%s://%s", scheme, net.JoinHostPort(addr,
				fmt.Sprint(port)))
			identity, ok := s.serviceRoot(ctx, base)
			if !ok {
				if ctx.Err() != nil {
					return Responder{}, false
				}
				continue
			}
			// Answered. Anything more needs credentials, and a BMC that refuses
			// them is still a BMC worth reporting - so a failure here narrows the
			// identity rather than discarding the responder.
			s.chassis(ctx, base, identity)
			return Responder{Address: addr, Protocol: "redfish",
				Identity: identity}, true
		}
	}
	return Responder{}, false
}

// serviceRoot reads /redfish/v1/ anonymously. This is the detection.
func (s *RedfishSweeper) serviceRoot(ctx context.Context,
	base string) (map[string]string, bool) {
	var root struct {
		OdataType      string `json:"@odata.type"`
		RedfishVersion string `json:"RedfishVersion"`
		Name           string `json:"Name"`
		ID             string `json:"Id"`
		// Optional in the specification and volunteered by many BMCs. Free
		// identity when it is there.
		Vendor  string `json:"Vendor"`
		Product string `json:"Product"`
		UUID    string `json:"UUID"`
	}
	if !s.getJSON(ctx, base+redfishRootPath, "", "", &root) {
		return nil, false
	}
	// A 200 with JSON that is not a service root is something else answering on
	// the port - a web UI, a load balancer, a proxy. RedfishVersion is the field
	// the specification requires, so it is the honest test.
	if root.RedfishVersion == "" && !strings.Contains(root.OdataType, "ServiceRoot") {
		return nil, false
	}
	identity := map[string]string{}
	put(identity, "redfishVersion", root.RedfishVersion)
	put(identity, "sysName", root.Name)
	put(identity, "serviceRootId", root.ID)
	put(identity, "vendor", root.Vendor)
	put(identity, "product", root.Product)
	put(identity, "uuid", root.UUID)
	return identity, true
}

// chassis follows Systems to the first member and reads its identity.
//
// The member id is NOT assumed. /redfish/v1/Systems/1 is an HPE-ism; Dell uses
// System.Embedded.1 and this plane uses the device's own id, so a hard-coded path
// would have found the serial on some vendors and 404ed on the rest.
func (s *RedfishSweeper) chassis(ctx context.Context, base string,
	identity map[string]string) {
	if len(s.Credentials) == 0 {
		return
	}
	for _, cred := range s.Credentials {
		var coll struct {
			Members []struct {
				ID string `json:"@odata.id"`
			} `json:"Members"`
		}
		if !s.getJSON(ctx, base+redfishSystemsPath,
			cred.Username, cred.Password, &coll) {
			continue
		}
		if len(coll.Members) == 0 {
			return
		}
		var sys struct {
			Manufacturer string `json:"Manufacturer"`
			Model        string `json:"Model"`
			SerialNumber string `json:"SerialNumber"`
			UUID         string `json:"UUID"`
			HostName     string `json:"HostName"`
			PowerState   string `json:"PowerState"`
		}
		if !s.getJSON(ctx, base+coll.Members[0].ID,
			cred.Username, cred.Password, &sys) {
			continue
		}
		// `serial` is the key the API matches on, and the reason this request is
		// worth making: it is the only identity that survives a re-addressing.
		put(identity, "serial", sys.SerialNumber)
		put(identity, "vendor", sys.Manufacturer)
		put(identity, "model", sys.Model)
		put(identity, "uuid", sys.UUID)
		put(identity, "hostName", sys.HostName)
		put(identity, "powerState", sys.PowerState)
		return
	}
}

func (s *RedfishSweeper) getJSON(ctx context.Context, url, user, pass string,
	into any) bool {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return false
	}
	req.Header.Set("Accept", "application/json")
	if user != "" {
		req.SetBasicAuth(user, pass)
	}
	resp, err := s.httpClient().Do(req)
	if err != nil {
		return false
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return false
	}
	return json.NewDecoder(resp.Body).Decode(into) == nil
}

// put writes a value only when there is one, so an absent field does not become
// an empty string that reads downstream as "the device said nothing here".
func put(m map[string]string, key, value string) {
	if v := strings.TrimSpace(value); v != "" {
		m[key] = v
	}
}
