// Package assign pulls this collector's work list from the DCIM API.
//
// The flow is inverted from the usual poller: the database is the source of
// truth for what exists, and the collector asks. A static device list cannot
// track a fleet that commissions and decommissions equipment at runtime, and it
// hides exactly the behaviour the platform exists to observe.
package assign

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"github.com/hari/dcim-platform/collector/internal/config"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

type Assignment struct {
	Version     int64              `json:"version"`
	GeneratedAt time.Time          `json:"generated_at"`
	CollectorID string             `json:"collector_id"`
	Endpoints   []*models.Endpoint `json:"endpoints"`
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

	etag    string
	current map[string]*models.Endpoint
	lastOK  time.Time

	// OnChange is invoked with the diff whenever the assignment changes.
	OnChange func(Diff)
}

func New(cfg *config.Config, log *slog.Logger, mets *obs.Metrics) *Client {
	return &Client{
		cfg:     cfg,
		http:    &http.Client{Timeout: cfg.DCIM.RequestTimeout},
		log:     log,
		mets:    mets,
		current: make(map[string]*models.Endpoint),
	}
}

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
					"age_seconds", int(time.Since(c.lastOK).Seconds()))
			}
			if !c.lastOK.IsZero() {
				c.mets.AssignmentAge.Set(time.Since(c.lastOK).Seconds())
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
		c.lastOK = time.Now()
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

	next := make(map[string]*models.Endpoint, len(assignment.Endpoints))
	for _, ep := range assignment.Endpoints {
		next[ep.ID] = ep
	}
	diff := c.diff(next)

	c.current = next
	c.etag = resp.Header.Get("ETag")
	c.lastOK = time.Now()
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
	return nil
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
	return a.Credential.Community() != b.Credential.Community()
}

func (c *Client) Endpoints() []*models.Endpoint {
	out := make([]*models.Endpoint, 0, len(c.current))
	for _, ep := range c.current {
		out = append(out, ep)
	}
	return out
}

func (c *Client) Count() int { return len(c.current) }

// Stale reports whether the assignment is too old to trust. The caller uses it
// to mark the collector self-degraded so endpoint health stops condemning.
func (c *Client) Stale() bool {
	if c.lastOK.IsZero() {
		return true
	}
	return time.Since(c.lastOK) > 5*c.cfg.DCIM.AssignmentInterval
}
