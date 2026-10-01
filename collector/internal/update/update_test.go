package update

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func signed(t *testing.T, data []byte) (Release, map[string]string) {
	t.Helper()
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	sum := sha256.Sum256(data)
	return Release{Version: "2.0.0", SHA256: hex.EncodeToString(sum[:]),
			Signature: base64.StdEncoding.EncodeToString(ed25519.Sign(priv, sum[:])),
			KeyID:     "k1", SizeBytes: int64(len(data))},
		map[string]string{"k1": base64.StdEncoding.EncodeToString(pub)}
}

func TestVerifyAcceptsOnlyTheSignedBuildFromATrustedKey(t *testing.T) {
	data := []byte("\x7fELF a collector build")
	rel, trusted := signed(t, data)
	if err := Verify(data, rel, trusted); err != nil {
		t.Fatalf("good build refused: %v", err)
	}
	if Verify(append([]byte{}, append(data, 'x')...), rel, trusted) == nil {
		t.Fatal("a tampered (longer) build was accepted")
	}
	flipped := append([]byte{}, data...)
	flipped[0] ^= 1
	rel2 := rel
	rel2.SizeBytes = 0
	if Verify(flipped, rel2, trusted) == nil {
		t.Fatal("a build with a different digest was accepted")
	}
	if Verify(data, rel, map[string]string{}) == nil {
		t.Fatal("a build signed by an untrusted key was accepted")
	}
	_, other := signed(t, data)
	if Verify(data, rel, map[string]string{"k1": other["k1"]}) == nil {
		t.Fatal("a signature from a different key under the same id was accepted")
	}
}

func TestStageKeepsThePreviousBinaryAndRestorePutsItBack(t *testing.T) {
	dir := t.TempDir()
	bin := filepath.Join(dir, "collector")
	if err := os.WriteFile(bin, []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := Stage(bin, []byte("new")); err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(bin); string(got) != "new" {
		t.Fatalf("bin = %q after stage", got)
	}
	if got, _ := os.ReadFile(bin + ".prev"); string(got) != "old" {
		t.Fatalf(".prev = %q, want the previous build", got)
	}
	if err := Restore(bin); err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(bin); string(got) != "old" {
		t.Fatalf("bin = %q after restore", got)
	}
	if got, _ := os.ReadFile(bin + ".failed"); string(got) != "new" {
		t.Fatalf(".failed = %q, want the failed build kept", got)
	}
}

func TestTheWatcherDecides(t *testing.T) {
	now := time.Now()
	pending := &Marker{Deadline: now.Add(time.Minute)}
	if Decide(pending, now) != WatchKeepWaiting {
		t.Fatal("a pending upgrade inside its window should be waited on")
	}
	if Decide(&Marker{Deadline: now.Add(-time.Second)}, now) != WatchRollBack {
		t.Fatal("past the deadline unconfirmed must roll back")
	}
	if Decide(&Marker{Deadline: now.Add(-time.Second), Confirmed: true}, now) != WatchDone {
		t.Fatal("a confirmed upgrade is done even past the deadline")
	}
	if Decide(nil, now) != WatchDone {
		t.Fatal("no marker is nothing to watch")
	}
}

func TestTheWatcherRollsBackAnUnconfirmedBuildAndLeavesTheReason(t *testing.T) {
	dir := t.TempDir()
	bin := filepath.Join(dir, "collector")
	_ = os.WriteFile(bin, []byte("old"), 0o755)
	_ = Stage(bin, []byte("broken"))
	staged := time.Now().Add(-10 * time.Minute)
	m := &Marker{CommandID: "c1", From: "1.0", To: "2.0", Bin: bin,
		Argv: []string{bin, "--config", "x.yaml"}, StagedAt: staged,
		Deadline: staged.Add(5 * time.Minute)}
	if err := SaveMarker(dir, m); err != nil {
		t.Fatal(err)
	}
	killed := 0
	err := Watch(dir, time.Millisecond,
		func([]string) int { killed++; return 1 },
		func([]string) bool { return true }, // the supervisor restarted it
		func(string, []string) error { t.Fatal("must not start it when a supervisor did"); return nil },
		func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(bin); string(got) != "old" {
		t.Fatalf("bin = %q, want the previous build restored", got)
	}
	if killed != 1 {
		t.Fatalf("killed %d times, want 1", killed)
	}
	after, _ := LoadMarker(dir)
	if after == nil || !after.RolledBack || after.Reason == "" {
		t.Fatalf("marker after rollback = %+v, want rolled_back with a reason", after)
	}
}

func TestTheWatcherExitsQuietlyOnceConfirmed(t *testing.T) {
	dir := t.TempDir()
	_ = SaveMarker(dir, &Marker{Deadline: time.Now().Add(time.Hour), Confirmed: true})
	err := Watch(dir, time.Millisecond,
		func([]string) int { t.Fatal("must not kill a confirmed build"); return 0 },
		func([]string) bool { return true },
		func(string, []string) error { return nil }, func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
}
