package spool

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

const segmentExt = ".seg"

// segmentPath names segments so lexicographic sort is chronological order:
// zero-padded decimal, sorting the same way whether read by a shell glob, a
// file browser, or this package.
func segmentPath(dir string, index uint64) string {
	return filepath.Join(dir, fmt.Sprintf("%020d%s", index, segmentExt))
}

// listSegments returns every segment's index and file size, oldest first.
// A directory that does not exist yet is not an error - a partition that has
// never spilled to disk has no segments, which is the ordinary case.
func listSegments(dir string) ([]segmentMeta, error) {
	entries, err := os.ReadDir(dir)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("list segments in %s: %w", dir, err)
	}
	var out []segmentMeta
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), segmentExt) {
			continue
		}
		idxStr := strings.TrimSuffix(e.Name(), segmentExt)
		idx, err := strconv.ParseUint(idxStr, 10, 64)
		if err != nil {
			continue // not one of ours - ignore rather than fail the whole spool
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		out = append(out, segmentMeta{index: idx, bytes: info.Size(),
			modTime: info.ModTime()})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].index < out[j].index })
	return out, nil
}

type segmentMeta struct {
	index   uint64
	bytes   int64
	modTime time.Time
}
