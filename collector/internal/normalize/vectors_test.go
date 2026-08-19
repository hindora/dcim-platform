package normalize

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// The rule is implemented twice - here for the collector, and in Python for
// the ingest worker. Two implementations of one rule drift, and the failure is
// silent: one port quietly becomes two series again. Both sides read this
// file, so a change to either has to be a change to both.
func TestSharedVectors(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "contracts",
		"testdata", "interface_names.json"))
	if err != nil {
		t.Fatalf("read vectors: %v", err)
	}
	var doc struct {
		Cases []struct {
			In   string `json:"in"`
			Name string `json:"name"`
			Key  string `json:"key"`
		} `json:"cases"`
		Distinct [][2]string `json:"distinct"`
		Same     [][2]string `json:"same"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("parse vectors: %v", err)
	}
	if len(doc.Cases) == 0 {
		t.Fatal("no vectors")
	}

	for _, c := range doc.Cases {
		if got := InterfaceName(c.In); got != c.Name {
			t.Errorf("InterfaceName(%q) = %q, want %q", c.In, got, c.Name)
		}
		if got := InterfaceKey(c.In); got != c.Key {
			t.Errorf("InterfaceKey(%q) = %q, want %q", c.In, got, c.Key)
		}
	}
	for _, pair := range doc.Same {
		if InterfaceKey(pair[0]) != InterfaceKey(pair[1]) {
			t.Errorf("%q and %q should share a key", pair[0], pair[1])
		}
	}
	for _, pair := range doc.Distinct {
		if InterfaceKey(pair[0]) == InterfaceKey(pair[1]) {
			t.Errorf("%q and %q must not share a key", pair[0], pair[1])
		}
	}
	t.Logf("%d shared vectors, %d same-pairs, %d distinct-pairs",
		len(doc.Cases), len(doc.Same), len(doc.Distinct))
}
