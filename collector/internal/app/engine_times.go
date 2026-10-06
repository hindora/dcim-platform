package app

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"time"

	"github.com/hari/dcim-platform/collector/internal/adapters/snmp"
)

const engineTimesFile = "snmp_engine_times.json"

// engineTimesEvery is how often a changed table is written. A crash loses at
// most this much of the senders' clocks, and anything that recent is inside
// the 150 s window anyway - so per-trap writes would buy nothing.
const engineTimesEvery = 30 * time.Second

// engineTimesDoc is the file. Engine IDs are binary (RFC 3411: 5-32 octets),
// so they are hex: JSON would replace their non-UTF-8 bytes.
type engineTimesDoc struct {
	Version int                              `json:"version"`
	Engines map[string]snmp.EngineClockState `json:"engines"`
}

func engineTimesPath(stateDir string) string {
	return filepath.Join(stateDir, engineTimesFile)
}

func readEngineTimes(path string) (map[string]snmp.EngineClockState, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var doc engineTimesDoc
	if err := json.Unmarshal(b, &doc); err != nil {
		return nil, err
	}
	if doc.Version != 1 {
		return nil, fmt.Errorf("unknown version %d", doc.Version)
	}
	out := make(map[string]snmp.EngineClockState, len(doc.Engines))
	for k, v := range doc.Engines {
		id, err := hex.DecodeString(k)
		if err != nil {
			return nil, fmt.Errorf("engine %q: %w", k, err)
		}
		out[string(id)] = v
	}
	return out, nil
}

func writeEngineTimes(path string, engines map[string]snmp.EngineClockState) error {
	doc := engineTimesDoc{Version: 1, Engines: make(map[string]snmp.EngineClockState, len(engines))}
	for id, v := range engines {
		doc.Engines[hex.EncodeToString([]byte(id))] = v
	}
	b, err := json.Marshal(doc)
	if err != nil {
		return err
	}
	return writeAtomic(path, b)
}

// loadEngineTimes restores the senders' clocks saved by the last run. A file
// that is missing is a first start; one that cannot be read is said loudly,
// because the receiver then trusts each engine's next message afresh - the
// replay window persistence exists to close.
func loadEngineTimes(stateDir string, times *snmp.EngineTimes, log *slog.Logger) {
	path := engineTimesPath(stateDir)
	saved, err := readEngineTimes(path)
	switch {
	case errors.Is(err, os.ErrNotExist):
		return
	case err != nil:
		log.Warn("SNMPv3 sender clocks not restored; each engine's next trap sets "+
			"its baseline again, so one replay per engine is possible", "path", path, "error", err)
		return
	}
	times.Import(saved)
	log.Info("SNMPv3 sender clocks restored", "engines", len(saved))
}

// saveEngineTimes writes the table if it changed since the last write.
func saveEngineTimes(stateDir string, times *snmp.EngineTimes, log *slog.Logger) {
	if times == nil || !times.Dirty() {
		return
	}
	path := engineTimesPath(stateDir)
	if err := writeEngineTimes(path, times.Export()); err != nil {
		log.Warn("SNMPv3 sender clocks not saved; after a restart one replay per "+
			"engine is possible", "path", path, "error", err)
	}
}

// persistEngineTimes saves the table while it changes. The last save is the
// shutdown path's, made after the trap listener has closed.
func persistEngineTimes(ctx context.Context, stateDir string, times *snmp.EngineTimes,
	every time.Duration, log *slog.Logger) {
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			saveEngineTimes(stateDir, times, log)
		}
	}
}

// ForgetEngine drops one sending engine from a stopped collector's saved
// table, so its next authentic trap becomes the new baseline. For a device
// whose boots went backwards - a factory reset that kept its engine ID - which
// RFC 3414 otherwise refuses until its boots catch up. The collector must be
// stopped: a running one writes its own table over this file.
func ForgetEngine(stateDir, engineHex string) (bool, error) {
	id, err := hex.DecodeString(engineHex)
	if err != nil {
		return false, fmt.Errorf("engine ID must be hex: %w", err)
	}
	path := engineTimesPath(stateDir)
	saved, err := readEngineTimes(path)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if _, ok := saved[string(id)]; !ok {
		return false, nil
	}
	delete(saved, string(id))
	return true, writeEngineTimes(path, saved)
}
