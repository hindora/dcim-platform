//go:build linux || darwin

package preflight

import "golang.org/x/sys/unix"

// freeBytes is the number of bytes available to an unprivileged process on
// the filesystem holding dir - what actually matters for "will the spool
// fit", not the filesystem's total size, which a quota or reserved-for-
// root margin can make meaningfully larger than what this process can
// really write.
func freeBytes(dir string) (uint64, error) {
	var st unix.Statfs_t
	if err := unix.Statfs(dir, &st); err != nil {
		return 0, err
	}
	return uint64(st.Bavail) * uint64(st.Bsize), nil //nolint:unconvert // Bsize's width differs by platform
}
