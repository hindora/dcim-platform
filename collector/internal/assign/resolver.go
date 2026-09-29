package assign

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"sync"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

// ResolveEntry is an endpoint this collector does not poll but may hear from.
//
// A trap goes wherever the device was configured to send it, and nobody
// rewrites trap destinations on every rebalance - so with more than one
// collector most traps land on a collector that does not own the sender.
// Resolved against the collector's own shard only, they published with no
// device and raised no alarm. Credential-free: the only secret-derived field
// is a digest of the v1/v2c community, a string every trap carries in clear.
type ResolveEntry struct {
	ID              string `json:"id"`
	DeviceID        string `json:"device_id"`
	DeviceName      string `json:"device_name"`
	DeviceType      string `json:"device_type"`
	Protocol        string `json:"protocol"`
	Role            string `json:"role"`
	Address         string `json:"address"`
	Site            string `json:"site"`
	CommunitySHA256 string `json:"community_sha256"`
}

func (e ResolveEntry) endpoint() *models.Endpoint {
	return &models.Endpoint{
		ID: e.ID, DeviceID: e.DeviceID, DeviceName: e.DeviceName,
		DeviceType: e.DeviceType, Protocol: e.Protocol, Role: e.Role,
		Address: e.Address,
	}
}

// Resolver maps a trap's source address back to an endpoint and device.
//
// This runs on every inbound trap, so it is an in-memory map fed from the
// assignment rather than a database lookup. A trap whose source cannot be
// resolved is still emitted - with the source IP and no device - because
// dropping it is how an outage becomes "the DCIM never saw it".
//
// Three tiers, in order: the endpoints this collector owns, then the rest of
// its own site, then every other site. Within the two borrowed tiers an
// address or community that names two different devices is dropped rather
// than guessed at: overlapping RFC1918 between sites is ordinary, and a trap
// filed against the wrong device is worse than one filed against none.
type Resolver struct {
	mu        sync.RWMutex
	byAddr    map[string]*models.Endpoint
	byCommun  map[string]*models.Endpoint
	byDigest  map[string]*models.Endpoint
	byID      map[string]*models.Endpoint
	ambiguous int
}

func NewResolver() *Resolver {
	return &Resolver{
		byAddr:   make(map[string]*models.Endpoint),
		byCommun: make(map[string]*models.Endpoint),
		byDigest: make(map[string]*models.Endpoint),
		byID:     make(map[string]*models.Endpoint),
	}
}

// tier fills one map from one tier of candidates. Keys a higher tier already
// holds are left alone; keys that two DIFFERENT devices in this tier both
// claim are removed and remembered, so a lower tier cannot re-add them.
type tier struct {
	into    map[string]*models.Endpoint
	blocked map[string]bool
}

func (t tier) offer(key string, ep *models.Endpoint, seen map[string]*models.Endpoint) bool {
	if key == "" || t.blocked[key] {
		return false
	}
	if _, held := t.into[key]; held {
		return false
	}
	if prev, ok := seen[key]; ok {
		if prev.DeviceID != ep.DeviceID {
			t.blocked[key] = true
			delete(seen, key)
			return true
		}
		return false
	}
	seen[key] = ep
	return false
}

func (t tier) commit(seen map[string]*models.Endpoint) {
	for k, ep := range seen {
		if !t.blocked[k] {
			t.into[k] = ep
		}
	}
}

// Replace swaps in a fresh view. Called on every assignment refresh.
func (r *Resolver) Replace(owned []*models.Endpoint, others []ResolveEntry, site string) {
	byAddr := make(map[string]*models.Endpoint, len(owned)+len(others))
	byCommun := make(map[string]*models.Endpoint, len(owned))
	byDigest := make(map[string]*models.Endpoint, len(owned)+len(others))
	byID := make(map[string]*models.Endpoint, len(owned)+len(others))

	for _, ep := range owned {
		byID[ep.ID] = ep
		if ep.Address != "" {
			// First writer wins: a device with several endpoints on one
			// address (an OS agent and a BMC never share one) should resolve
			// to the first, and any of them names the same device anyway.
			if _, seen := byAddr[ep.Address]; !seen {
				byAddr[ep.Address] = ep
			}
		}
		// The community is a second, independent identity hint. In this device
		// plane it IS the agent's address, so a trap that arrives from a
		// different source address than it polls on can still be attributed.
		if c := ep.Credential.Community(); c != "" {
			if _, seen := byCommun[c]; !seen {
				byCommun[c] = ep
			}
			d := CommunityDigest(c)
			if _, seen := byDigest[d]; !seen {
				byDigest[d] = ep
			}
		}
	}

	addr := tier{into: byAddr, blocked: map[string]bool{}}
	digest := tier{into: byDigest, blocked: map[string]bool{}}
	ambiguous := 0
	for _, sameSite := range []bool{true, false} {
		seenAddr := map[string]*models.Endpoint{}
		seenDigest := map[string]*models.Endpoint{}
		for _, e := range others {
			if (site != "" && e.Site == site) != sameSite {
				continue
			}
			ep := e.endpoint()
			if _, owned := byID[ep.ID]; !owned {
				byID[ep.ID] = ep
			}
			if addr.offer(e.Address, ep, seenAddr) {
				ambiguous++
			}
			if digest.offer(e.CommunitySHA256, ep, seenDigest) {
				ambiguous++
			}
		}
		addr.commit(seenAddr)
		digest.commit(seenDigest)
	}

	r.mu.Lock()
	r.byAddr, r.byCommun, r.byDigest, r.byID = byAddr, byCommun, byDigest, byID
	r.ambiguous = ambiguous
	r.mu.Unlock()
}

// CommunityDigest is the form the platform sends a borrowed community in.
func CommunityDigest(community string) string {
	sum := sha256.Sum256([]byte(community))
	return hex.EncodeToString(sum[:])
}

// Resolve finds the endpoint a trap came from, preferring the source address
// and falling back to the community.
func (r *Resolver) Resolve(sourceIP, community string) (*models.Endpoint, bool) {
	ip := strings.TrimSpace(sourceIP)
	r.mu.RLock()
	defer r.mu.RUnlock()
	if ep, ok := r.byAddr[ip]; ok {
		return ep, true
	}
	if community != "" {
		if ep, ok := r.byCommun[community]; ok {
			return ep, true
		}
		if ep, ok := r.byDigest[CommunityDigest(community)]; ok {
			return ep, true
		}
	}
	return nil, false
}

// ResolveID looks an endpoint up by its id.
//
// A Redfish subscription carries the endpoint id in its Context, which is a
// far better identity than the source address: the BMC may deliver from a
// different interface than it is polled on, and behind NAT the source address
// is not the BMC's at all.
func (r *Resolver) ResolveID(id string) (*models.Endpoint, bool) {
	if id == "" {
		return nil, false
	}
	r.mu.RLock()
	defer r.mu.RUnlock()
	ep, ok := r.byID[id]
	return ep, ok
}

func (r *Resolver) Len() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.byAddr)
}

// Ambiguous is how many addresses and communities were refused because two
// devices outside this collector's shard both claim them.
func (r *Resolver) Ambiguous() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.ambiguous
}
