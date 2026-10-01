// Package update replaces this collector's own binary with a signed release
// and rolls it back if the new one does not come up healthy (docs/26 Phase 7).
//
// The shape is Elastic Agent's: the binary being replaced stays on disk as
// <bin>.prev and is started, detached, as a WATCHER before the new binary
// takes over the process. A broken release that crashes before main() can
// never roll itself back - only something that is not that release can - so
// the watcher is the old, known-good binary. It waits for the new binary to
// confirm (assignment fetched, healthy for a while) and, if the deadline
// passes first, restores .prev, kills whatever is running the bad build, and
// lets the supervisor (systemd Restart=always; scripts/dev.sh's loop) start
// the old binary again - or starts it itself if nothing does. The old binary
// then finds the marker saying it was rolled back and reports the command
// failed, with the reason.
//
// Trust: the platform serves releases but cannot sign them. Every artefact is
// checked here - size, sha256, and an Ed25519 signature over the digest -
// against keys in THIS collector's own configuration, before a byte of it is
// executed. A compromised platform can withhold an upgrade, never push one.
package update

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

// Release is the upgrade command's payload.
type Release struct {
	Version   string `json:"version"`
	SHA256    string `json:"sha256"`
	Signature string `json:"signature"`
	KeyID     string `json:"key_id"`
	SizeBytes int64  `json:"size_bytes"`
	URL       string `json:"url"`
}

// Marker is the upgrade's state on disk, shared by the old binary (which
// writes it), the new one (which confirms it) and the watcher (which rolls
// back on it).
type Marker struct {
	CommandID  string    `json:"command_id"`
	From       string    `json:"from"`
	To         string    `json:"to"`
	Bin        string    `json:"bin"`
	Argv       []string  `json:"argv"`
	StagedAt   time.Time `json:"staged_at"`
	Deadline   time.Time `json:"deadline"`
	Confirmed  bool      `json:"confirmed"`
	RolledBack bool      `json:"rolled_back"`
	Reason     string    `json:"reason,omitempty"`
}

const markerName = "upgrade.json"

func MarkerPath(stateDir string) string { return filepath.Join(stateDir, markerName) }

func LoadMarker(stateDir string) (*Marker, error) {
	raw, err := os.ReadFile(MarkerPath(stateDir))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var m Marker
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("corrupt upgrade marker: %w", err)
	}
	return &m, nil
}

// SaveMarker writes atomically: a crash mid-write must not leave the watcher
// reading half a marker.
func SaveMarker(stateDir string, m *Marker) error {
	if err := os.MkdirAll(stateDir, 0o700); err != nil {
		return err
	}
	raw, _ := json.MarshalIndent(m, "", "  ")
	tmp := MarkerPath(stateDir) + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, MarkerPath(stateDir))
}

func RemoveMarker(stateDir string) error {
	err := os.Remove(MarkerPath(stateDir))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

// Verify checks an artefact against the release record and this collector's
// trusted keys. Size and digest first (cheap, and catch truncation), then the
// signature over the digest's raw bytes.
func Verify(data []byte, rel Release, trusted map[string]string) error {
	if rel.SizeBytes > 0 && int64(len(data)) != rel.SizeBytes {
		return fmt.Errorf("size %d, release says %d", len(data), rel.SizeBytes)
	}
	sum := sha256.Sum256(data)
	if hex.EncodeToString(sum[:]) != rel.SHA256 {
		return fmt.Errorf("sha256 mismatch: got %s", hex.EncodeToString(sum[:]))
	}
	keyB64, ok := trusted[rel.KeyID]
	if !ok {
		return fmt.Errorf("signed with key %q, which this collector does not trust", rel.KeyID)
	}
	key, err := base64.StdEncoding.DecodeString(keyB64)
	if err != nil || len(key) != ed25519.PublicKeySize {
		return fmt.Errorf("trusted key %q is not a 32-byte base64 Ed25519 key", rel.KeyID)
	}
	sig, err := base64.StdEncoding.DecodeString(rel.Signature)
	if err != nil {
		return fmt.Errorf("signature is not base64")
	}
	if !ed25519.Verify(ed25519.PublicKey(key), sum[:], sig) {
		return fmt.Errorf("signature does not verify against key %q", rel.KeyID)
	}
	return nil
}

// Download fetches the artefact with the collector's authenticated client,
// refusing anything larger than the release declared.
func Download(ctx context.Context, client *http.Client, baseURL, token string,
	rel Release) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+rel.URL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("download: HTTP %d", resp.StatusCode)
	}
	limit := rel.SizeBytes + 1
	if limit <= 1 {
		limit = 256 << 20
	}
	return io.ReadAll(io.LimitReader(resp.Body, limit))
}

// Stage puts a verified build in place: written beside the running binary,
// the current one kept as .prev, then swapped in with renames on the same
// filesystem so the path never points at half a file.
func Stage(bin string, data []byte) error {
	next := bin + ".next"
	if err := os.WriteFile(next, data, 0o755); err != nil {
		return err
	}
	if f, err := os.Open(next); err == nil {
		_ = f.Sync()
		_ = f.Close()
	}
	if err := os.Rename(bin, bin+".prev"); err != nil {
		return fmt.Errorf("keeping the running binary as .prev: %w", err)
	}
	if err := os.Rename(next, bin); err != nil {
		_ = os.Rename(bin+".prev", bin)
		return fmt.Errorf("swapping in the new binary: %w", err)
	}
	return nil
}

// Restore puts .prev back. The failed build is kept as .failed for whoever
// investigates it.
func Restore(bin string) error {
	if _, err := os.Stat(bin + ".prev"); err != nil {
		return fmt.Errorf("no previous binary to restore: %w", err)
	}
	_ = os.Remove(bin + ".failed")
	_ = os.Rename(bin, bin+".failed")
	return os.Rename(bin+".prev", bin)
}

// WatchDecision is what the watcher should do on one look at the marker.
type WatchDecision int

const (
	WatchKeepWaiting WatchDecision = iota
	WatchDone                      // confirmed (or the marker is gone)
	WatchRollBack                  // deadline passed unconfirmed
)

// Decide is the watcher's whole policy, pure.
func Decide(m *Marker, now time.Time) WatchDecision {
	switch {
	case m == nil, m.Confirmed, m.RolledBack:
		return WatchDone
	case now.After(m.Deadline):
		return WatchRollBack
	default:
		return WatchKeepWaiting
	}
}

// Watch is the watcher process's loop: the OLD binary, detached, deciding
// every few seconds until the upgrade is confirmed or its deadline passes.
// kill stops whatever runs the given argv; start runs it if nothing did.
func Watch(stateDir string, poll time.Duration, kill func(argv []string) int,
	alive func(argv []string) bool, start func(bin string, argv []string) error,
	logf func(string, ...any)) error {
	for {
		m, err := LoadMarker(stateDir)
		if err != nil {
			return err
		}
		switch Decide(m, time.Now()) {
		case WatchDone:
			logf("upgrade confirmed or settled; watcher exiting")
			return nil
		case WatchRollBack:
			logf("no healthy start of %s before %s - rolling back to %s",
				m.To, m.Deadline.Format(time.RFC3339), m.From)
			if err := Restore(m.Bin); err != nil {
				return err
			}
			m.RolledBack = true
			m.Reason = fmt.Sprintf("rolled back to %s: %s did not report healthy within %s",
				m.From, m.To, m.Deadline.Sub(m.StagedAt).Round(time.Second))
			if err := SaveMarker(stateDir, m); err != nil {
				return err
			}
			logf("stopped %d process(es) running the failed build", kill(m.Argv))
			// A supervisor (systemd, dev.sh's loop) restarts it with the
			// restored binary. If nothing has after a while, start it here.
			deadline := time.Now().Add(15 * time.Second)
			for time.Now().Before(deadline) {
				if alive(m.Argv) {
					logf("supervisor restarted the collector on %s", m.From)
					return nil
				}
				time.Sleep(time.Second)
			}
			logf("no supervisor restarted it; starting %s directly", m.From)
			return start(m.Bin, m.Argv)
		}
		time.Sleep(poll)
	}
}
