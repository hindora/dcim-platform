//go:build windows

package preflight

import "golang.org/x/sys/windows"

// freeBytes is the number of bytes available to THIS process's user on the
// volume holding dir - GetDiskFreeSpaceEx's "free bytes available to the
// calling user", not the volume's raw free space, for the same reason the
// Unix build uses Bavail (unprivileged-available) rather than Bfree
// (total-free, quota and reservations included).
//
// Present at all only because this collector's own development happens on
// Windows; every real deployment target is deploy/collector.service's
// systemd unit on Linux.
func freeBytes(dir string) (uint64, error) {
	pathPtr, err := windows.UTF16PtrFromString(dir)
	if err != nil {
		return 0, err
	}
	var freeAvail, total, totalFree uint64
	if err := windows.GetDiskFreeSpaceEx(pathPtr, &freeAvail, &total, &totalFree); err != nil {
		return 0, err
	}
	return freeAvail, nil
}
