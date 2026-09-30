package preflight

import (
	"path/filepath"
	"testing"
)

// Runs on whatever platform the test suite executes on - real filesystem
// calls against a real temp directory, not a mock, on every OS this
// package builds for (see diskspace_unix.go / diskspace_windows.go).
func TestFreeBytesReturnsSomethingPositiveForATempDir(t *testing.T) {
	free, err := freeBytes(t.TempDir())
	if err != nil {
		t.Fatalf("freeBytes: %v", err)
	}
	if free == 0 {
		t.Error("freeBytes returned 0 for a real, writable temp directory")
	}
}

func TestFreeBytesErrorsOnANonexistentPath(t *testing.T) {
	// filepath.Join, not a hardcoded separator, so this is the same test on
	// every OS this package builds for.
	bogus := filepath.Join(t.TempDir(), "this-path-does-not-exist", "nested")
	if _, err := freeBytes(bogus); err == nil {
		t.Error("expected an error for a path that does not exist")
	}
}
