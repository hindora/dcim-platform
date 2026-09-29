// Package spool is the collector's durable fallback for when the platform
// cannot be reached: an append-only, segmented, on-disk log that a WAN
// partition or a collector restart costs replay time against, not data.
//
// Design in one sentence: memory is the hot path, disk is the durability
// backstop, and replay always drains oldest-first. A caller that can reach
// the platform never touches this package's disk at all - see
// publish.GatewayPublisher, which tries a live send before ever calling
// Append, and only appends when a backlog already exists (to preserve
// ordering) or the live attempt itself failed.
package spool

import (
	"encoding/binary"
	"fmt"
	"io"

	"github.com/vmihailenco/msgpack/v5"
)

// record is one queued batch, framed on disk as [4-byte big-endian length]
// [msgpack-encoded record]. Payload is exactly the bytes Append was given -
// this package never interprets them, so it cannot drift from whatever the
// wire format on top of it is; it stores and replays bytes.
type record struct {
	Seq     uint64 `msgpack:"seq"`
	Stream  string `msgpack:"stream"`
	At      int64  `msgpack:"at"` // unix nanos when Append was called
	Payload []byte `msgpack:"payload"`
}

const maxRecordBytes = 64 << 20 // 64 MiB: a sanity ceiling, not the segment size

func encodeRecord(r record) ([]byte, error) {
	body, err := msgpack.Marshal(r)
	if err != nil {
		return nil, fmt.Errorf("encode spool record: %w", err)
	}
	if len(body) > maxRecordBytes {
		return nil, fmt.Errorf("spool record is %d bytes, over the %d limit",
			len(body), maxRecordBytes)
	}
	framed := make([]byte, 4+len(body))
	binary.BigEndian.PutUint32(framed, uint32(len(body)))
	copy(framed[4:], body)
	return framed, nil
}

// readRecord reads one framed record from r. A short read on the length
// prefix or the body - the shape a crash mid-write leaves behind, since a
// write is never atomic across the 4-byte length and the body that follows
// it - is io.ErrUnexpectedEOF, which callers treat as "this segment's valid
// data ends here", not as a corrupt file.
func readRecord(r io.Reader) (record, error) {
	var lenBuf [4]byte
	if _, err := io.ReadFull(r, lenBuf[:]); err != nil {
		if err == io.EOF {
			return record{}, io.EOF // a clean end: no partial length prefix
		}
		return record{}, io.ErrUnexpectedEOF
	}
	n := binary.BigEndian.Uint32(lenBuf[:])
	if n > maxRecordBytes {
		// A length this large is not a real record - almost certainly a
		// segment file that was never valid, or corruption. Treated the same
		// as a truncated tail: stop reading, do not panic on the alloc.
		return record{}, io.ErrUnexpectedEOF
	}
	body := make([]byte, n)
	if _, err := io.ReadFull(r, body); err != nil {
		return record{}, io.ErrUnexpectedEOF
	}
	var rec record
	if err := msgpack.Unmarshal(body, &rec); err != nil {
		return record{}, io.ErrUnexpectedEOF
	}
	return rec, nil
}
