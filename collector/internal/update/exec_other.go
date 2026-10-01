//go:build !linux && !darwin

package update

import "errors"

// Self-update replaces a running binary and re-executes it in place, which
// this collector only does on Linux (its deployment target) and macOS. A
// Windows build - only ever a developer's - refuses the command instead.
var errUnsupported = errors.New("self-update is supported on Linux and macOS only")

func ExecSelf(string, []string) error           { return errUnsupported }
func SpawnWatcher(string, string, string) error { return errUnsupported }
func KillMatching([]string) int                 { return 0 }
func Alive([]string) bool                       { return false }
func StartDetached(string, []string) error      { return errUnsupported }
