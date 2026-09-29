// Package assign pulls this collector's work list from the DCIM API.
//
// The flow is inverted from the usual poller: the database is the source of
// truth for what exists, and the collector asks. A static device list cannot
// track a fleet that commissions and decommissions equipment at runtime, and it
// hides exactly the behaviour the platform exists to observe.
package assign

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"sync/atomic"
	"time"

	"github.com/hari/dcim-platform/collector/internal/config"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/internal/sealedbox"
	"github.com/hari/dcim-platform/collector/internal/vault"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

type Assignment struct {
	Version     int64     `json:"version"`
	GeneratedAt time.Time `json:"generated_at"`
	CollectorID string    `json:"collector_id"`
	// The site an admin placed this collector in; empty means "any".
	Site      string             `json:"site"`
	Endpoints []*models.Endpoint `json:"endpoints"`
	// Everything else a trap may arrive from - see ResolveEntry.
	Resolve []ResolveEntry `json:"resolve"`
}

// Diff is what changed between two assignments.
type Diff struct {
	Added   []*models.Endpoint
	Removed []*models.Endpoint
	Changed []*models.Endpoint
}

func (d Diff) Empty() bool {
	return len(d.Added) == 0 && len(d.Removed) == 0 && len(d.Changed) == 0
}

type Client struct {
	cfg  *config.Config
	http *http.Client
	log  *slog.Logger
	mets *obs.Metrics
	// seal is nil for a collector with no sealedbox keypair yet, which
	// cannot happen once app.New always calls sealedbox.LoadOrGenerate -
	// nil is only ever a test double's choice. A nil seal leaves a Sealed
	// credential exactly as the platform sent it: unusable by any adapter,
	// which is the honest outcome of a collector that cannot unseal
	// anything rather than a panic.
	seal *sealedbox.KeyPair
	// vault is nil unless a resolver was configured (internal/vault). An
	// endpoint whose credential is a "credential_ref" then keeps that
	// reference unresolved - every adapter treats it as an empty
	// credential, the same visible failure a real vault outage would
	// produce, not a crash.
	vault *vault.Registry

	etag    string
	current map[string]*models.Endpoint
	resolve []ResolveEntry
	site    string
	// Read by the heartbeat loop while the refresh loop writes them, so they
	// are atomics: the age and version in every heartbeat come from here.
	version atomic.Int64
	lastOK  atomic.Int64 // unix nanoseconds; zero before the first fetch
	started time.Time

	// OnChange is invoked with the diff whenever the assignment changes.
	OnChange func(Diff)
	// OnRefreshed is invoked after every fetch that returned a body, whether
	// or not the owned set moved: the resolve list changes when OTHER
	// collectors' endpoints do, which is no diff of this one's.
	OnRefreshed func()
}

// tlsConfig is nil for a collector with no enrolled certificate - see
// config.NewRemoteClient's docstring for why that is the ordinary case, not
// an error.
func New(cfg *config.Config, log *slog.Logger, mets *obs.Metrics,
	tlsConfig *tls.Config, seal *sealedbox.KeyPair) *Client {
	return &Client{
		cfg: cfg,
		http: &http.Client{Timeout: cfg.DCIM.RequestTimeout,
			Transport: &http.Transport{TLSClientConfig: tlsConfig}},
		log:     log,
		mets:    mets,
		seal:    seal,
		current: make(map[string]*models.Endpoint),
		started: time.Now(),
	}
}

// SetVaultRegistry attaches a vault resolver registry after construction -
// a setter rather than another New() parameter because it is the rare case
// (most deployments have no credential_ref endpoints at all) and every
// existing caller of New should not have to know it exists.
func (c *Client) SetVaultRegistry(r *vault.Registry) { c.vault = r }

// Run refreshes on an interval. It returns when ctx is cancelled.
func (c *Client) Run(ctx context.Context) {
	ticker := time.NewTicker(c.cfg.DCIM.AssignmentInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := c.Refresh(ctx); err != nil {
				c.mets.AssignmentErrors.Inc()
				// Keep polling the last known set. Falling back to "no
				// endpoints" would silently stop all collection, which is a far
				// worse failure than a stale list.
				c.log.Error("assignment refresh failed; keeping last known set",
					"error", err, "endpoints", len(c.current),
					"age_seconds", int(c.AgeSeconds()))
			}
			if c.lastOK.Load() != 0 {
				c.mets.AssignmentAge.Set(c.AgeSeconds())
			}
		}
	}
}

func (c *Client) Refresh(ctx context.Context) error {
	url := fmt.Sprintf("%s%s?collector_id=%s",
		c.cfg.DCIM.BaseURL, c.cfg.DCIM.AssignmentPath, c.cfg.Collector.ID)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.cfg.Token())
	req.Header.Set("Accept", "application/json")
	if c.etag != "" {
		req.Header.Set("If-None-Match", c.etag)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	switch resp.StatusCode {
	case http.StatusNotModified:
		c.lastOK.Store(time.Now().UnixNano())
		return nil
	case http.StatusOK:
	case http.StatusUnauthorized, http.StatusForbidden:
		// Distinct from a transport error: retrying a bad token forever is
		// noise, and the operator needs to see the real cause.
		return fmt.Errorf("assignment rejected (%d): check the collector token",
			resp.StatusCode)
	default:
		return fmt.Errorf("assignment failed: HTTP %d", resp.StatusCode)
	}

	var assignment Assignment
	if err := json.NewDecoder(resp.Body).Decode(&assignment); err != nil {
		return fmt.Errorf("decode assignment: %w", err)
	}
	c.unsealCredentials(assignment.Endpoints)
	c.resolveCredentialRefs(ctx, assignment.Endpoints)

	next := make(map[string]*models.Endpoint, len(assignment.Endpoints))
	for _, ep := range assignment.Endpoints {
		next[ep.ID] = ep
	}
	diff := c.diff(next)

	c.current = next
	c.resolve = assignment.Resolve
	c.site = assignment.Site
	c.version.Store(assignment.Version)
	c.etag = resp.Header.Get("ETag")
	c.lastOK.Store(time.Now().UnixNano())
	c.mets.AssignmentVersion.Set(float64(assignment.Version))
	c.mets.AssignmentAge.Set(0)

	if !diff.Empty() {
		c.log.Info("assignment changed", "version", assignment.Version,
			"total", len(next), "added", len(diff.Added),
			"removed", len(diff.Removed), "changed", len(diff.Changed))
		if c.OnChange != nil {
			c.OnChange(diff)
		}
	}
	if c.OnRefreshed != nil {
		c.OnRefreshed()
	}
	return nil
}

// unsealCredentials replaces every endpoint's Sealed credential with its
// plaintext Data, in place, before anything else in the collector ever sees
// the assignment - docs/26 Phase 4. A credential that fails to unseal
// (wrong/stale key, corrupt blob) is dropped rather than left half-sealed:
// Data stays nil, which every adapter already treats as "no credential",
// the same outcome build_assignment's own decrypt-failure path produces
// server-side. One endpoint's bad credential must not take the rest of the
// assignment down.
func (c *Client) unsealCredentials(endpoints []*models.Endpoint) {
	for _, ep := range endpoints {
		cred := ep.Credential
		if cred == nil || cred.Sealed == "" {
			continue
		}
		if c.seal == nil {
			c.log.Warn("assignment carries a sealed credential but this "+
				"collector has no sealing key; dropping it", "endpoint_id", ep.ID)
			cred.Data = nil
			cred.Sealed = ""
			continue
		}
		blob, err := base64.StdEncoding.DecodeString(cred.Sealed)
		if err != nil {
			c.log.Error("sealed credential is not valid base64; dropping it",
				"endpoint_id", ep.ID, "error", err)
			cred.Data = nil
			cred.Sealed = ""
			continue
		}
		data, err := c.seal.Unseal(blob)
		if err != nil {
			c.log.Error("could not unseal credential; dropping it",
				"endpoint_id", ep.ID, "error", err)
			cred.Data = nil
			cred.Sealed = ""
			continue
		}
		cred.Data = data
		cred.Sealed = ""
	}
}

// resolveCredentialRefs replaces a "credential_ref" credential's Data
// (after unsealCredentials has already turned any sealed reference into a
// plain one) with the actual secret an external vault names, in place -
// docs/26 Phase 4. Runs after unsealing so a ref delivered sealed is
// resolved the same as one delivered plaintext.
//
// A ref this collector cannot currently resolve - no registry configured,
// the wrong backend registered, the vault itself unreachable - is left
// alone rather than cleared: Data still holds the reference string
// (Data["ref"]), and every protocol adapter that reads Credential.
// Community()/Data["username"] etc. finds nothing usable there and fails
// the poll as a config/auth error, which is the honest outcome of a
// credential this process genuinely cannot use right now.
func (c *Client) resolveCredentialRefs(ctx context.Context, endpoints []*models.Endpoint) {
	if c.vault == nil {
		return
	}
	for _, ep := range endpoints {
		cred := ep.Credential
		if cred == nil || cred.Kind != "credential_ref" {
			continue
		}
		ref, _ := cred.Data["ref"].(string)
		if ref == "" {
			continue
		}
		resolved, err := c.vault.Resolve(ctx, ref)
		if err != nil {
			c.log.Error("could not resolve credential_ref; endpoint will fail to "+
				"authenticate until it can be", "endpoint_id", ep.ID, "ref", ref, "error", err)
			continue
		}
		cred.Data = resolved
	}
}

func (c *Client) diff(next map[string]*models.Endpoint) Diff {
	var d Diff
	for id, ep := range next {
		previous, ok := c.current[id]
		if !ok {
			d.Added = append(d.Added, ep)
			continue
		}
		if changed(previous, ep) {
			d.Changed = append(d.Changed, ep)
		}
	}
	for id, ep := range c.current {
		if _, ok := next[id]; !ok {
			d.Removed = append(d.Removed, ep)
		}
	}
	return d
}

// changed reports whether anything that affects HOW we poll has moved.
// Deliberately excludes cosmetic fields: restarting a job resets counter
// baselines, and doing that because a device was renamed would put a gap in
// every throughput chart.
func changed(a, b *models.Endpoint) bool {
	if a.Address != b.Address || a.Port != b.Port || a.Protocol != b.Protocol {
		return true
	}
	if a.Poll.IntervalS != b.Poll.IntervalS ||
		a.Poll.TimeoutMs != b.Poll.TimeoutMs ||
		a.Poll.Retries != b.Poll.Retries {
		return true
	}
	if len(a.Poll.MetricGroups) != len(b.Poll.MetricGroups) {
		return true
	}
	for i := range a.Poll.MetricGroups {
		if a.Poll.MetricGroups[i] != b.Poll.MetricGroups[i] {
			return true
		}
	}
	// The whole credential, not only the SNMP community. A rotated Redfish or
	// gNMI password compared equal here, so even once the platform served it
	// the job kept presenting the old one - failing every poll, and on a BMC
	// that counts failures, locking the account.
	return credentialDigest(a.Credential) != credentialDigest(b.Credential)
}

func credentialDigest(c *models.Credential) string {
	if c == nil {
		return ""
	}
	// encoding/json sorts map keys, so equal credentials digest equally.
	raw, err := json.Marshal(c)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(raw)
	return string(sum[:])
}

func (c *Client) Endpoints() []*models.Endpoint {
	out := make([]*models.Endpoint, 0, len(c.current))
	for _, ep := range c.current {
		out = append(out, ep)
	}
	return out
}

func (c *Client) Count() int { return len(c.current) }

// Resolve is the rest of the estate, for the trap resolver.
func (c *Client) Resolve() []ResolveEntry { return c.resolve }

// Site is where this collector is placed; empty means "any".
func (c *Client) Site() string { return c.site }

// Version is the assignment version last applied.
func (c *Client) Version() int64 { return c.version.Load() }

// AgeSeconds is how long since an assignment was last fetched successfully.
// Before the first one it is the age of the process: zero would read as
// "fresh" to the platform, which is the one thing it certainly is not.
func (c *Client) AgeSeconds() float64 {
	last := c.lastOK.Load()
	if last == 0 {
		return time.Since(c.started).Seconds()
	}
	return time.Since(time.Unix(0, last)).Seconds()
}

// Stale reports whether the assignment is too old to trust. The caller uses it
// to mark the collector self-degraded so endpoint health stops condemning.
func (c *Client) Stale() bool {
	last := c.lastOK.Load()
	if last == 0 {
		return true
	}
	return time.Since(time.Unix(0, last)) > 5*c.cfg.DCIM.AssignmentInterval
}
