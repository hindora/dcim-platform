package mapping

import (
	"io/fs"
	"os"
	"testing"
	"testing/fstest"
)

func TestBundleSHAIsStableAcrossFileOrderAndSource(t *testing.T) {
	// The same content, presented two different ways - a map with keys in one
	// order, and the embedded bundle itself - must fingerprint identically, or
	// the backend's comparison against contracts/mappings (a third source of
	// the same bytes) is meaningless.
	a := fstest.MapFS{
		"snmp/standard.yaml":  &fstest.MapFile{Data: []byte("a: 1\n")},
		"bacnet/objects.yaml": &fstest.MapFile{Data: []byte("b: 2\n")},
	}
	b := fstest.MapFS{
		"bacnet/objects.yaml": &fstest.MapFile{Data: []byte("b: 2\n")},
		"snmp/standard.yaml":  &fstest.MapFile{Data: []byte("a: 1\n")},
	}
	shaA, err := BundleSHA(a)
	if err != nil {
		t.Fatal(err)
	}
	shaB, err := BundleSHA(b)
	if err != nil {
		t.Fatal(err)
	}
	if shaA != shaB {
		t.Fatalf("map iteration order changed the digest: %s vs %s", shaA, shaB)
	}
}

func TestBundleSHAChangesWithContent(t *testing.T) {
	a := fstest.MapFS{"snmp/standard.yaml": &fstest.MapFile{Data: []byte("a: 1\n")}}
	b := fstest.MapFS{"snmp/standard.yaml": &fstest.MapFile{Data: []byte("a: 2\n")}}
	shaA, _ := BundleSHA(a)
	shaB, _ := BundleSHA(b)
	if shaA == shaB {
		t.Fatal("changing one byte did not change the digest")
	}
}

func TestBundleSHACannotConfusePathAndContentBoundaries(t *testing.T) {
	// "ab" split as path "a"+content "b" must not equal path "ab"+content "".
	a := fstest.MapFS{"a": &fstest.MapFile{Data: []byte("b")}}
	b := fstest.MapFS{"ab": &fstest.MapFile{Data: []byte("")}}
	shaA, _ := BundleSHA(a)
	shaB, _ := BundleSHA(b)
	if shaA == shaB {
		t.Fatal("a path/content boundary collision was not distinguished")
	}
}

func TestEmbeddedBundleMatchesTheCheckedOutMappings(t *testing.T) {
	// If this ever fails, scripts/sync_mapping_bundle.py --check would already
	// have caught it in CI - this test is what a contributor sees locally
	// without running that script by hand.
	embeddedSHA, err := BundleSHA(Embedded())
	if err != nil {
		t.Fatal(err)
	}
	onDiskSHA, err := BundleSHA(os.DirFS("../../../contracts/mappings"))
	if err != nil {
		t.Fatal(err)
	}
	if embeddedSHA != onDiskSHA {
		t.Fatalf("embedded mapping bundle (%s) is out of sync with "+
			"contracts/mappings (%s) - run scripts/sync_mapping_bundle.py",
			embeddedSHA[:12], onDiskSHA[:12])
	}
}

func TestResolvePrefersAnExistingDirectoryOverTheEmbeddedBundle(t *testing.T) {
	fsys, source := Resolve("../../../contracts/mappings")
	if source == "embedded" {
		t.Fatal("an existing override directory was ignored")
	}
	if _, err := fs.Stat(fsys, "snmp/standard.yaml"); err != nil {
		t.Fatalf("resolved filesystem cannot read a known mapping file: %v", err)
	}
}

func TestResolveFallsBackToEmbeddedWhenTheDirectoryIsMissing(t *testing.T) {
	fsys, source := Resolve("/no/such/directory/anywhere")
	if source != "embedded" {
		t.Fatalf("expected embedded fallback, got %q", source)
	}
	if _, err := fs.Stat(fsys, "snmp/standard.yaml"); err != nil {
		t.Fatalf("embedded fallback cannot read a known mapping file: %v", err)
	}
}

func TestResolveFallsBackWhenDirIsEmpty(t *testing.T) {
	_, source := Resolve("")
	if source != "embedded" {
		t.Fatalf("expected embedded fallback for an unset dir, got %q", source)
	}
}
