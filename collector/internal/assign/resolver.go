package assign

import (
	"strings"
	"sync"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

// Resolver maps a trap's source address back to an endpoint and device.
//
// This runs on every inbound trap, so it is an in-memory map fed from the
// assignment rather than a database lookup. A trap whose source cannot be
// resolved is still emitted - with the source IP and no device - because
// dropping it is how an outage becomes "the DCIM never saw it".
type Resolver struct {
	mu       sync.RWMutex
	byAddr   map[string]*models.Endpoint
	byCommun map[string]*models.Endpoint
	byID     map[string]*models.Endpoint
}

func NewResolver() *Resolver {
	return &Resolver{
		byAddr:   make(map[string]*models.Endpoint),
		byCommun: make(map[string]*models.Endpoint),
		byID:     make(map[string]*models.Endpoint),
	}
}

// Replace swaps in a fresh view. Called on every assignment change.
func (r *Resolver) Replace(endpoints []*models.Endpoint) {
	byAddr := make(map[string]*models.Endpoint, len(endpoints))
	byCommun := make(map[string]*models.Endpoint, len(endpoints))
	byID := make(map[string]*models.Endpoint, len(endpoints))
	for _, ep := range endpoints {
		byID[ep.ID] = ep
		if ep.Address != "" {
			// First writer wins: a device with several endpoints on one address
			// (an OS agent and a BMC never share one) should resolve to the
			// first, and any of them names the same device anyway.
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
		}
	}
	r.mu.Lock()
	r.byAddr, r.byCommun, r.byID = byAddr, byCommun, byID
	r.mu.Unlock()
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
