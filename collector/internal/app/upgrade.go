package app

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/hari/dcim-platform/collector/internal/update"
)

// commandLoop long-polls the platform for commands (docs/26 Phase 7). The
// same response carries a "moves" token: when it changes, some collector's
// ownership changed, and this one refetches its assignment now rather than
// on its next interval - a failover reaches the survivor in about a second.
func (a *App) commandLoop(ctx context.Context) {
	base := a.cfg.DCIM.BaseURL + strings.TrimSuffix(a.cfg.DCIM.AssignmentPath, "/assignments")
	client := &http.Client{Timeout: 40 * time.Second,
		Transport: &http.Transport{TLSClientConfig: a.tlsConfig}}
	moves := ""
	for ctx.Err() == nil {
		q := url.Values{"collector_id": {a.cfg.Collector.ID}, "wait": {"25"}, "moves": {moves}}
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, base+"/commands?"+q.Encode(), nil)
		req.Header.Set("Authorization", "Bearer "+a.cfg.Token())
		resp, err := client.Do(req)
		if err != nil {
			a.sleep(ctx, 10*time.Second)
			continue
		}
		var body struct {
			Commands []struct {
				ID      string          `json:"id"`
				Kind    string          `json:"kind"`
				Payload json.RawMessage `json:"payload"`
			} `json:"commands"`
			Moves string `json:"moves"`
		}
		status := resp.StatusCode
		if status == http.StatusOK {
			_ = json.NewDecoder(resp.Body).Decode(&body)
		}
		resp.Body.Close()
		switch {
		case status == http.StatusNotFound:
			// A platform older than Phase 7: nothing to poll. Check rarely.
			a.sleep(ctx, 5*time.Minute)
			continue
		case status != http.StatusOK:
			a.sleep(ctx, 10*time.Second)
			continue
		}
		if moves != "" && body.Moves != moves {
			a.assign.Kick()
		}
		moves = body.Moves
		for _, c := range body.Commands {
			switch c.Kind {
			case "upgrade":
				a.handleUpgrade(ctx, base, c.ID, c.Payload)
			default:
				a.reportCommand(ctx, base, c.ID, "failed",
					fmt.Sprintf("unknown command kind %q", c.Kind), "")
			}
		}
	}
}

func (a *App) sleep(ctx context.Context, d time.Duration) {
	select {
	case <-ctx.Done():
	case <-time.After(d):
	}
}

func (a *App) reportCommand(ctx context.Context, base, id, state, detail, version string) {
	payload, _ := json.Marshal(map[string]string{"state": state, "detail": detail,
		"version": version})
	q := url.Values{"collector_id": {a.cfg.Collector.ID}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		base+"/commands/"+url.PathEscape(id)+"/result?"+q.Encode(), bytes.NewReader(payload))
	if err != nil {
		return
	}
	req.Header.Set("Authorization", "Bearer "+a.cfg.Token())
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 15 * time.Second,
		Transport: &http.Transport{TLSClientConfig: a.tlsConfig}}
	if resp, err := client.Do(req); err == nil {
		resp.Body.Close()
	}
	a.log.Info("command finished", "command_id", id, "state", state, "detail", detail)
}

// handleUpgrade verifies and stages a release, starts the previous binary as
// the watcher, and re-executes into the new one. It only returns on refusal
// or failure; on success the process image is replaced.
func (a *App) handleUpgrade(ctx context.Context, base, id string, raw json.RawMessage) {
	fail := func(msg string) { a.reportCommand(ctx, base, id, "failed", msg, a.cfg.Collector.Version) }
	if !a.cfg.Update.Enabled {
		fail("self-update is disabled on this collector (update.enabled is false)")
		return
	}
	var rel update.Release
	if err := json.Unmarshal(raw, &rel); err != nil || rel.Version == "" {
		fail("malformed upgrade payload")
		return
	}
	if rel.Version == a.cfg.Collector.Version {
		a.reportCommand(ctx, base, id, "succeeded", "already running "+rel.Version, rel.Version)
		return
	}
	stateDir := a.cfg.Collector.StateDir
	if m, _ := update.LoadMarker(stateDir); m != nil && !m.Confirmed && !m.RolledBack {
		fail("another upgrade is still being verified")
		return
	}
	a.log.Info("upgrade requested", "from", a.cfg.Collector.Version, "to", rel.Version)
	dl, cancel := context.WithTimeout(ctx, 5*time.Minute)
	defer cancel()
	client := &http.Client{Transport: &http.Transport{TLSClientConfig: a.tlsConfig}}
	data, err := update.Download(dl, client, a.cfg.DCIM.BaseURL, a.cfg.Token(), rel)
	if err != nil {
		fail("download failed: " + err.Error())
		return
	}
	if err := update.Verify(data, rel, a.cfg.Update.TrustedKeys); err != nil {
		fail("refused: " + err.Error())
		return
	}
	bin, err := os.Executable()
	if err == nil {
		bin, err = filepath.EvalSymlinks(bin)
	}
	if err != nil {
		fail("cannot locate the running binary: " + err.Error())
		return
	}
	if err := update.Stage(bin, data); err != nil {
		fail("staging failed: " + err.Error())
		return
	}
	now := time.Now()
	m := &update.Marker{CommandID: id, From: a.cfg.Collector.Version, To: rel.Version,
		Bin: bin, Argv: os.Args, StagedAt: now, Deadline: now.Add(a.cfg.Update.VerifyWindow)}
	if err := update.SaveMarker(stateDir, m); err != nil {
		_ = update.Restore(bin)
		fail("could not record the upgrade: " + err.Error())
		return
	}
	if err := update.SpawnWatcher(bin+".prev", stateDir,
		filepath.Join(stateDir, "upgrade-watch.log")); err != nil {
		_ = update.Restore(bin)
		_ = update.RemoveMarker(stateDir)
		fail("could not start the rollback watcher: " + err.Error())
		return
	}
	a.log.Warn("re-executing into the new release", "from", m.From, "to", m.To,
		"verify_window", a.cfg.Update.VerifyWindow.String())
	if err := update.ExecSelf(bin, os.Args); err != nil {
		// Still the old process: undo the swap. The watcher sees the marker
		// gone and exits.
		_ = update.Restore(bin)
		_ = update.RemoveMarker(stateDir)
		fail("re-exec failed: " + err.Error())
	}
}

// resumeUpgrade runs once at start: this process may be the new build that
// must confirm itself, or the old one put back by the watcher, which reports
// the failure.
func (a *App) resumeUpgrade(ctx context.Context) {
	base := a.cfg.DCIM.BaseURL + strings.TrimSuffix(a.cfg.DCIM.AssignmentPath, "/assignments")
	stateDir := a.cfg.Collector.StateDir
	m, err := update.LoadMarker(stateDir)
	if err != nil || m == nil {
		return
	}
	switch {
	case m.Confirmed:
		_ = update.RemoveMarker(stateDir)
	case m.RolledBack || m.To != a.cfg.Collector.Version:
		reason := m.Reason
		if reason == "" {
			reason = fmt.Sprintf("upgrade to %s did not take effect; still running %s",
				m.To, a.cfg.Collector.Version)
		}
		a.reportCommand(ctx, base, m.CommandID, "failed", reason, a.cfg.Collector.Version)
		_ = update.RemoveMarker(stateDir)
	default:
		go a.confirmUpgrade(ctx, base, m)
	}
}

// confirmUpgrade waits until this new build has run ConfirmAfter with a
// fresh assignment, then confirms - the watcher then exits - and reports
// success. Never confirming is the failure signal: the watcher rolls back at
// the deadline.
func (a *App) confirmUpgrade(ctx context.Context, base string, m *update.Marker) {
	started := time.Now()
	for ctx.Err() == nil {
		a.sleep(ctx, 5*time.Second)
		if time.Since(started) < a.cfg.Update.ConfirmAfter || a.assign.Stale() ||
			a.assign.AgeSeconds() > 2*a.cfg.DCIM.AssignmentInterval.Seconds() {
			continue
		}
		m.Confirmed = true
		if err := update.SaveMarker(a.cfg.Collector.StateDir, m); err != nil {
			a.log.Error("could not confirm the upgrade", "error", err)
			continue
		}
		a.reportCommand(ctx, base, m.CommandID, "succeeded",
			fmt.Sprintf("running %s, healthy for %s", m.To,
				time.Since(started).Round(time.Second)), m.To)
		return
	}
}
