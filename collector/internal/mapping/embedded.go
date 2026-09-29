package mapping

import (
	"embed"
	"io/fs"
	"os"
	"strings"
)

// embedded is a checked-in copy of contracts/mappings, synced by
// scripts/sync_mapping_bundle.py and verified in CI the same way generated
// contract code is: `git diff --exit-code` after regenerating.
//
// go:embed cannot reach outside this module - contracts/mappings lives at the
// monorepo root, one level above collector/ - so a live copy is committed
// here instead of a symlink, which Windows checkouts (this project's WSL/
// Windows dev split) cannot be relied on to preserve.
//
//go:embed all:embedded
var embeddedFS embed.FS

// Embedded is the mapping bundle built into this binary.
func Embedded() fs.FS {
	sub, err := fs.Sub(embeddedFS, "embedded")
	if err != nil {
		// Only reachable if the embed directive above and this Sub call
		// disagree, which a passing build already rules out.
		panic("mapping: embedded bundle: " + err.Error())
	}
	return sub
}

// Resolve picks where mapping data comes from, and says which it picked.
//
// Mappings are DATA - adding an OID must not require a collector release -
// so an operator override always wins when it is actually there: a directory
// on disk can be edited without a restart-and-redeploy cycle, which is how
// this platform's own dev loop works. A packaged install ships no such
// directory beside the binary, and `dir` not existing is not an error - it
// is the normal case for a container or a .deb - so it falls back to what
// was embedded at build time.
func Resolve(dir string) (fsys fs.FS, source string) {
	if d := strings.TrimSpace(dir); d != "" {
		if info, err := os.Stat(d); err == nil && info.IsDir() {
			return os.DirFS(d), "dir:" + d
		}
	}
	return Embedded(), "embedded"
}
