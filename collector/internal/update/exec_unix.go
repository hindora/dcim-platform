//go:build linux || darwin

package update

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"syscall"
)

// ExecSelf replaces this process with the binary at bin, keeping its PID -
// so a supervisor that tracks the PID (systemd, scripts/dev.sh) sees one
// long-lived process, not a crash and a restart.
func ExecSelf(bin string, argv []string) error {
	return syscall.Exec(bin, argv, os.Environ())
}

// SpawnWatcher starts the given binary as a detached watcher in its own
// session, so it outlives this process being replaced or killed.
func SpawnWatcher(bin, stateDir, logPath string) error {
	cmd := exec.Command(bin, "upgrade-watch", "--state-dir", stateDir)
	if f, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600); err == nil {
		cmd.Stdout, cmd.Stderr = f, f
	}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	return cmd.Start()
}

// KillMatching sends SIGKILL to every process whose command line is argv,
// other than the caller - the build that never confirmed, however many
// times its supervisor restarted it. Linux /proc only.
func KillMatching(argv []string) int {
	want := []byte(joinNul(argv))
	self := os.Getpid()
	n := 0
	entries, _ := os.ReadDir("/proc")
	for _, e := range entries {
		pid, err := strconv.Atoi(e.Name())
		if err != nil || pid == self {
			continue
		}
		raw, err := os.ReadFile(filepath.Join("/proc", e.Name(), "cmdline"))
		if err != nil || !bytes.Equal(bytes.TrimRight(raw, "\x00"), want) {
			continue
		}
		if syscall.Kill(pid, syscall.SIGKILL) == nil {
			n++
		}
	}
	return n
}

// Alive reports whether any process other than the caller runs argv.
func Alive(argv []string) bool {
	want := []byte(joinNul(argv))
	self := os.Getpid()
	entries, _ := os.ReadDir("/proc")
	for _, e := range entries {
		pid, err := strconv.Atoi(e.Name())
		if err != nil || pid == self {
			continue
		}
		raw, err := os.ReadFile(filepath.Join("/proc", e.Name(), "cmdline"))
		if err == nil && bytes.Equal(bytes.TrimRight(raw, "\x00"), want) {
			return true
		}
	}
	return false
}

// StartDetached runs bin with argv[1:] in a new session - the watcher's last
// resort when no supervisor restarted the collector after a rollback.
func StartDetached(bin string, argv []string) error {
	cmd := exec.Command(bin, argv[1:]...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	return cmd.Start()
}

func joinNul(argv []string) string {
	var b bytes.Buffer
	for i, a := range argv {
		if i > 0 {
			b.WriteByte(0)
		}
		b.WriteString(a)
	}
	return b.String()
}
