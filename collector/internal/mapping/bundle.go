package mapping

import (
	"crypto/sha256"
	"encoding/hex"
	"io/fs"
	"sort"
)

// BundleSHA fingerprints every regular file under fsys, in path order, so an
// embedded copy and a live directory holding the same content produce the
// same digest regardless of file order or mtimes.
//
// The backend computes the identical hash over contracts/mappings (see
// contracts/mapping_bundle.py - same file set, same sort, same separator
// bytes) and compares it against what a collector reports in its heartbeat.
// A mismatch means this collector is running mapping data the platform does
// not recognise, which a version string alone would not catch on a
// hot-patched build.
func BundleSHA(fsys fs.FS) (string, error) {
	var files []string
	err := fs.WalkDir(fsys, ".", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() {
			files = append(files, p)
		}
		return nil
	})
	if err != nil {
		return "", err
	}
	sort.Strings(files)

	h := sha256.New()
	for _, p := range files {
		raw, err := fs.ReadFile(fsys, p)
		if err != nil {
			return "", err
		}
		// A NUL separator between and after each field: cheap insurance
		// against a path and a content boundary reading the same as a
		// different split, for files that will never themselves contain one.
		h.Write([]byte(p))
		h.Write([]byte{0})
		h.Write(raw)
		h.Write([]byte{0})
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
