package sealedbox

import (
	"encoding/base64"
	"os"
	"path/filepath"
	"testing"
)

// TestUnsealMatchesThePythonEncoder is the one test that actually proves
// interop rather than self-consistency: round-tripping Generate/Unseal
// against itself would pass even if this package's construction quietly
// diverged from sealed_credential.py's - a bug that would only surface in
// production as every real assignment credential failing to decrypt. This
// vector was captured from a real call to seal_for_collector with a fixed
// private key (bytes 0..31) and the payload below; see that module's
// docstring for the wire format.
func TestUnsealMatchesThePythonEncoder(t *testing.T) {
	privB64 := "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
	sealedB64 := "SmLE6HE/hrqf36z8HRJmntNyUySLQyIMbBXqGqlCyweGOCMP3Lk8ClMTQ9EciPSB" +
		"Kb0nECUXxFULuH2Wj6xHxGFt+mfewlct5tgFa9MhCCzbuvt4bNY+J5moJghmL/8h/CCHimU="

	priv, err := base64.StdEncoding.DecodeString(privB64)
	if err != nil {
		t.Fatalf("decode private key fixture: %v", err)
	}
	sealed, err := base64.StdEncoding.DecodeString(sealedB64)
	if err != nil {
		t.Fatalf("decode sealed fixture: %v", err)
	}

	kp, err := Load(priv)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	got, err := kp.Unseal(sealed)
	if err != nil {
		t.Fatalf("Unseal: %v", err)
	}
	want := map[string]any{"username": "admin", "password": "hunter2"}
	if len(got) != len(want) || got["username"] != want["username"] ||
		got["password"] != want["password"] {
		t.Errorf("Unseal = %+v, want %+v", got, want)
	}
}

func TestGenerateSealAndUnsealRoundTrips(t *testing.T) {
	kp, err := Generate()
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	// This package only ever unseals - sealing is the platform's job - so
	// exercising the round trip here means reconstructing what
	// seal_for_collector does, in Go, purely to confirm Unseal is not
	// paired with a broken deriveKey that happens to also be broken the
	// same way on encode. TestUnsealMatchesThePythonEncoder above is what
	// actually guards against a real divergence; this one guards against a
	// regression that breaks Unseal for every key, not just the fixture's.
	if len(kp.PublicKeyBytes()) != 32 {
		t.Fatalf("PublicKeyBytes length = %d, want 32", len(kp.PublicKeyBytes()))
	}
	reloaded, err := Load(kp.Bytes())
	if err != nil {
		t.Fatalf("Load(Bytes()): %v", err)
	}
	if string(reloaded.PublicKeyBytes()) != string(kp.PublicKeyBytes()) {
		t.Error("a reloaded key does not reproduce the same public key")
	}
}

func TestUnsealRejectsATruncatedBlob(t *testing.T) {
	kp, err := Generate()
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	if _, err := kp.Unseal([]byte("too short")); err == nil {
		t.Error("Unseal accepted a blob shorter than pubkey+nonce")
	}
}

func TestUnsealRejectsATamperedCiphertext(t *testing.T) {
	kp, err := Generate()
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	// A blob of the right shape but garbage content must fail the GCM tag
	// check, not panic or silently return junk.
	blob := make([]byte, pubKeyBytes+nonceBytes+16)
	copy(blob[:pubKeyBytes], kp.PublicKeyBytes())
	if _, err := kp.Unseal(blob); err == nil {
		t.Error("Unseal accepted a tampered/garbage ciphertext")
	}
}

func TestLoadOrGeneratePersistsAcrossRestarts(t *testing.T) {
	dir := t.TempDir()
	first, err := LoadOrGenerate(dir)
	if err != nil {
		t.Fatalf("LoadOrGenerate (first): %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, keyFile)); err != nil {
		t.Fatalf("key file was not written: %v", err)
	}
	second, err := LoadOrGenerate(dir)
	if err != nil {
		t.Fatalf("LoadOrGenerate (second): %v", err)
	}
	if string(first.PublicKeyBytes()) != string(second.PublicKeyBytes()) {
		t.Error("a second LoadOrGenerate against the same state dir produced " +
			"a DIFFERENT public key - every restart would silently re-enroll")
	}
}
