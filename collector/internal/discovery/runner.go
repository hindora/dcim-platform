package discovery

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"time"
)

// Runner claims queued discovery runs from the API and executes them.
//
// Pull rather than push: the API cannot reach into the management network and
// should not try. The collector asks whether there is work, which also means a
// collector that is down simply does not claim anything rather than having
// sweeps queue up against it.
type Runner struct {
	BaseURL  string
	Token    func() string
	Interval time.Duration
	Sweeper  *Sweeper
	HTTP     *http.Client
	Log      *slog.Logger
}

type claimResponse struct {
	Run *struct {
		ID     string          `json:"id"`
		Method string          `json:"method"`
		Scope  json.RawMessage `json:"scope"`
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

	started := time.Now()
	found := r.Sweeper.Sweep(ctx, addrs)
	r.Log.Info("discovery sweep finished", "run_id", run.ID,
		"probed", len(addrs), "answered", len(found),
		"seconds", int(time.Since(started).Seconds()))

	return r.report(ctx, run.ID, resultsBody{Responders: found})
}

func (r *Runner) claim(ctx context.Context) (*claimResponse, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		r.BaseURL+"/api/v1/collector/discovery/claim", nil)
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
