package preflight

import (
	"context"
	"encoding/binary"
	"fmt"
	"net"
	"time"
)

// ntpEpoch is the offset between NTP's epoch (1900-01-01) and Unix's
// (1970-01-01), in seconds - what every NTP timestamp in the wire format
// is measured from and what has to be subtracted to get a time.Time.
const ntpEpoch = 2208988800

// ntpOffset queries addr (host:port, usually "pool.ntp.org:123") with a
// minimal SNTP client (RFC 4330) and returns the standard NTP offset: how
// much the LOCAL clock would need to move forward to match the server's.
// Positive means the server is ahead of this host (this host is slow);
// negative means this host is ahead of the server (this host is fast).
//
// Hand-rolled rather than pulled in as a dependency: SNTP's client mode is
// one 48-byte request and one 48-byte reply, small enough that a purpose-
// built implementation is less risk than a new module for it, and NTP is
// exactly the kind of protocol worth being able to read end to end in this
// codebase rather than trust to a black box.
func ntpOffset(ctx context.Context, addr string, timeout time.Duration) (time.Duration, error) {
	conn, err := net.Dial("udp", addr)
	if err != nil {
		return 0, fmt.Errorf("dial %s: %w", addr, err)
	}
	defer conn.Close()
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	} else {
		_ = conn.SetDeadline(time.Now().Add(timeout))
	}

	var req [48]byte
	// LI = 0 (no warning), VN = 4, Mode = 3 (client) - the single byte
	// every SNTP request is identified by.
	req[0] = 0x23
	t1 := time.Now().UTC()
	putNTPTime(req[40:48], t1) // Transmit Timestamp - echoed back as Originate

	if _, err := conn.Write(req[:]); err != nil {
		return 0, fmt.Errorf("send to %s: %w", addr, err)
	}

	var resp [48]byte
	n, err := conn.Read(resp[:])
	t4 := time.Now().UTC()
	if err != nil {
		return 0, fmt.Errorf("read from %s: %w", addr, err)
	}
	if n < 48 {
		return 0, fmt.Errorf("%s sent a short reply (%d bytes)", addr, n)
	}

	mode := resp[0] & 0x7
	if mode != 4 { // server mode
		return 0, fmt.Errorf("%s replied in mode %d, not 4 (server)", addr, mode)
	}
	stratum := resp[1]
	if stratum == 0 {
		return 0, fmt.Errorf("%s sent a kiss-of-death reply (stratum 0) - "+
			"it is refusing this query, possibly rate limiting", addr)
	}

	t2 := ntpTime(resp[32:40]) // Receive Timestamp
	t3 := ntpTime(resp[40:48]) // Transmit Timestamp

	// The standard NTP offset formula: the client-observed round trip,
	// split evenly, minus the server's own processing delay between
	// receiving and replying.
	offset := ((t2.Sub(t1) + t3.Sub(t4)) / 2)
	return offset, nil
}

func putNTPTime(b []byte, t time.Time) {
	sec := uint32(t.Unix() + ntpEpoch)
	frac := uint32((uint64(t.Nanosecond()) << 32) / 1e9)
	binary.BigEndian.PutUint32(b[0:4], sec)
	binary.BigEndian.PutUint32(b[4:8], frac)
}

func ntpTime(b []byte) time.Time {
	sec := binary.BigEndian.Uint32(b[0:4])
	frac := binary.BigEndian.Uint32(b[4:8])
	nsec := (uint64(frac) * 1e9) >> 32
	return time.Unix(int64(sec)-ntpEpoch, int64(nsec)).UTC()
}
