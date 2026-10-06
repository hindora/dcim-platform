package discovery

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"time"
)

// Runner claims queued discovery runs from the API and executes them.
//
// Pull rather than push: the API cannot reach into the management network and
// should not try. The collector asks whether there is work, which also means a
// collector that is down simply does not claim anything rather than having
// sweeps queue up against it.
type Runner struct {
	BaseURL string
	// CollectorID is declared on every claim, so a run assigned to this
	// collector's range reaches this collector and no other. A scoped token
	// already carries it; a fleet-wide token cannot, and this is how it says.
	CollectorID string
	Token       func() string
	Interval    time.Duration
	// StatusEvery is how often a sweep in progress asks whether its run was
	// cancelled. Zero means 15 s.
	StatusEvery time.Duration
	Sweeper     *Sweeper
	// Redfish is optional. Nil means a run sweeps SNMP only, which is what a
	// deployment that has not configured Redfish discovery should get.
	Redfish *RedfishSweeper
	// Liveness, when set, skips the full probes at addresses where nothing
	// answers - unless the API names the address as expected. Nil probes
	// every address, as sweeps always did.
	Liveness *Liveness
	HTTP     *http.Client
	Log      *slog.Logger
}

type claimResponse struct {
	Run *struct {
		ID     string          `json:"id"`
		Method string          `json:"method"`
		Scope  json.RawMessage `json:"scope"`
		// Expected is what must be probed in full whatever the liveness
		// check hears. Absent from an API that predates it - and then the
		// check is not used, since skipping inventory nobody named would
		// report it missing.
		Expected *[]string `json:"expected"`
	} `json:"run"`
}

type resultsBody struct {
	Responders []Responder `json:"responders"`
	Error      string      `json:"error,omitempty"`
}

func (r *Runner) Run(ctx context.Context) {
	interval := r.Interval
	if interval <= 0 {
		interval = 30 * time.Second
	}
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if err := r.once(ctx); err != nil {
				r.Log.Warn("discovery poll failed", "error", err)
			}
		}
	}
}

func (r *Runner) once(ctx context.Context) error {
	claim, err := r.claim(ctx)
	if err != nil || claim == nil || claim.Run == nil {
		return err
	}
	run := claim.Run
	r.Log.Info("discovery run claimed", "run_id", run.ID, "method", run.Method)

	scope, err := ParseScope(run.Scope)
	if err != nil {
		return r.report(ctx, run.ID, resultsBody{Error: "unreadable scope: " + err.Error()})
	}
	addrs, err := Hosts(scope.Subnets)
	if err != nil {
		// Reported rather than logged and dropped: the operator who queued the
		// run is the one who needs to know their scope was too wide.
		return r.report(ctx, run.ID, resultsBody{Error: err.Error()})
	}
	before := len(addrs)
	if addrs, err = Excluding(addrs, scope.Exclude); err != nil {
		return r.report(ctx, run.ID, resultsBody{Error: err.Error()})
	}
	if skipped := before - len(addrs); skipped > 0 {
		r.Log.Info("discovery exclusions applied", "run_id", run.ID,
			"skipped", skipped)
	}

	// The sweep runs under its own context, which a cancel on the API ends.
	// Without it a cancelled /20 swept on for hours, and this collector claimed
	// no other run until it was done. Reporting stays on ctx: the run's
	// context is dead exactly when there is nothing to report.
	runCtx, stop := context.WithCancelCause(ctx)
	defer stop(nil)
	go r.watchRun(runCtx, run.ID, stop)

	started := time.Now()
	targets := addrs
	if r.Liveness != nil && run.Expected != nil {
		expected := make(map[string]bool, len(*run.Expected))
		for _, a := range *run.Expected {
			expected[a] = true
		}
		targets = r.Liveness.Filter(runCtx, addrs, expected)
		if r.abandoned(runCtx, run.ID, started) {
			return nil
		}
		r.Log.Info("discovery liveness checked", "run_id", run.ID,
			"addresses", len(addrs), "expected", len(expected),
			"probing", len(targets), "seconds", int(time.Since(started).Seconds()))
	}

	found := r.Sweeper.Sweep(runCtx, targets)
	snmpCount := len(found)
	if r.abandoned(runCtx, run.ID, started) {
		return nil
	}

	// Both planes in one run, sequentially. One address can legitimately answer
	// both - a server's BMC runs an SNMP agent AND Redfish - and the two arrive as
	// separate responders, which is what the API's (address, protocol) candidate
	// key is for. Sequential because a sweep is a background audit: running them
	// together would double the traffic on the management network at once for no
	// answer that arrives sooner than the operator needs it.
	var redfishCount int
	if r.Redfish != nil {
		rf := r.Redfish.Sweep(runCtx, targets)
		redfishCount = len(rf)
		found = append(found, rf...)
		if r.abandoned(runCtx, run.ID, started) {
			return nil
		}
	}

	r.Log.Info("discovery sweep finished", "run_id", run.ID,
		"probed", len(addrs), "answered", len(found),
		"snmp", snmpCount, "redfish", redfishCount,
		"seconds", int(time.Since(started).Seconds()))

	return r.report(ctx, run.ID, resultsBody{Responders: found})
}

// errRunStopped is the cause a run's context ends with when the API says the
// run is no longer running - cancelled, or failed by the scheduler.
var errRunStopped = errors.New("discovery run stopped by the API")

// watchRun asks after the run until the sweep ends, and stops the sweep when
// the run is no longer running. Only an answer stops it: an error, or a 404
// from an API that predates the status route, leaves the sweep going, which
// is what every sweep did before.
func (r *Runner) watchRun(ctx context.Context, runID string, stop context.CancelCauseFunc) {
	every := r.StatusEvery
	if every <= 0 {
		every = 15 * time.Second
	}
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			st, err := r.runStatus(ctx, runID)
			if err != nil {
				r.Log.Debug("discovery run status unknown", "run_id", runID, "error", err)
				continue
			}
			if st != "running" {
				r.Log.Info("discovery run no longer running; stopping the sweep",
					"run_id", runID, "status", st)
				stop(errRunStopped)
				return
			}
		}
	}
}

// abandoned says whether the API stopped the run, logging it once.
func (r *Runner) abandoned(runCtx context.Context, runID string, started time.Time) bool {
	if !errors.Is(context.Cause(runCtx), errRunStopped) {
		return false
	}
	r.Log.Info("discovery sweep abandoned", "run_id", runID,
		"seconds", int(time.Since(started).Seconds()))
	return true
}

func (r *Runner) runStatus(ctx context.Context, runID string) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		r.BaseURL+"/api/v1/collector/discovery/"+url.PathEscape(runID)+"/status", nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+r.Token())
	resp, err := r.HTTP.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("run status: HTTP %d", resp.StatusCode)
	}
	var out struct {
		Status string `json:"status"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", err
	}
	if out.Status == "" {
		return "", errors.New("run status: empty")
	}
	return out.Status, nil
}

func (r *Runner) claim(ctx context.Context) (*claimResponse, error) {
	q := url.Values{}
	if r.CollectorID != "" {
		q.Set("collector_id", r.CollectorID)
	}
	// Said, not assumed: a run with exclusions is only handed to a collector
	// that can honour them.
	q.Set("features", "exclude")
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		r.BaseURL+"/api/v1/collector/discovery/claim?"+q.Encode(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+r.Token())
	resp, err := r.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("claim: HTTP %d", resp.StatusCode)
	}
	var out claimResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, err
	}
	return &out, nil
}

func (r *Runner) report(ctx context.Context, runID string, body resultsBody) error {
	buf, err := json.Marshal(body)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		r.BaseURL+"/api/v1/collector/discovery/"+runID+"/results",
		bytes.NewReader(buf))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+r.Token())
	req.Header.Set("Content-Type", "application/json")
	resp, err := r.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("report: HTTP %d", resp.StatusCode)
	}
	return nil
}
