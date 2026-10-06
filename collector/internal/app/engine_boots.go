package app

import (
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

const engineBootsFile = "snmp_engine_boots"

// nextEngineBoots is this collector's snmpEngineBoots for this run: the
// stored value plus one, written back before it is used.
//
// For an INFORM the collector's trap receiver is the authoritative engine,
// and USM's replay protection rests on (boots, time) never repeating. Time
// restarts at zero with the process, so boots must go up every start - an
// agent keeps it in NVRAM for the same reason (RFC 3414 2.2.2). Were it
// not persisted, an INFORM captured early in one run would fall inside the
// window again early in the next.
//
// If the state dir cannot be written the count still advances for this run,
// and the warning says what that costs.
func nextEngineBoots(stateDir string, log *slog.Logger) uint32 {
	path := filepath.Join(stateDir, engineBootsFile)
	var prev uint64
	if b, err := os.ReadFile(path); err == nil {
		prev, _ = strconv.ParseUint(strings.TrimSpace(string(b)), 10, 32)
	}
	next := prev + 1
	if next > 2147483647 {
		next = 2147483647 // RFC 3414's latch: the engine is then never in time
	}
	if err := writeAtomic(path, []byte(strconv.FormatUint(next, 10)+"\n")); err != nil {
		log.Warn("SNMPv3 engine boots not persisted; INFORMs from before a restart "+
			"may be accepted again after it", "path", path, "error", err)
	}
	return uint32(next)
}

func writeAtomic(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".boots-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp.Name(), path); err != nil {
		return fmt.Errorf("replace %s: %w", path, err)
	}
	return nil
}
