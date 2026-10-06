package snmp

import (
	"net"

	g "github.com/gosnmp/gosnmp"

	"github.com/hari/dcim-platform/collector/internal/obs"
)

// usmGuardConn sits between a v3 poll session and its socket and drops a
// response that is not authenticated the way the session requires, before
// gosnmp sees it.
//
// gosnmp compares only as many digest bytes as a response carries, so a
// plaintext noAuthNoPriv response - one an on-path attacker can write with
// no key, given the request it answers - was accepted as the device's data,
// and a one-byte digest passes one time in 256. Forged UPS load or PDU
// current hides a real fault or raises a false one. net-snmp and the
// commercial pollers refuse such a response (RFC 3414 3.2 steps 5-6).
//
// Dropped, not failed: the request keeps waiting for the real response,
// and times out only if none comes. A Report below the session's level is
// let through - USM discovery and the time-window resync are unauthenticated
// or authNoPriv by design - but nothing else is.
type usmGuardConn struct {
	net.Conn
	need      g.SnmpV3MsgFlags // the session's security level
	digestLen int
	mets      *obs.Metrics
}

func guardUSM(conn net.Conn, usm usmParams, mets *obs.Metrics) net.Conn {
	return &usmGuardConn{Conn: conn, need: usm.msgFlags() & g.AuthPriv,
		digestLen: digestLen[usm.authProtocol], mets: mets}
}

func (c *usmGuardConn) Read(b []byte) (int, error) {
	for {
		n, err := c.Conn.Read(b)
		if err != nil {
			return n, err
		}
		if reason := c.refuse(b[:n]); reason != "" {
			if c.mets != nil {
				c.mets.V3RejectedTotal.WithLabelValues(reason).Inc()
			}
			continue
		}
		return n, nil
	}
}

// refuse is why a datagram must not reach gosnmp, or "" to let it through.
func (c *usmGuardConn) refuse(msg []byte) string {
	h, ok := parseV3Header(msg)
	if !ok {
		// Not something gosnmp would accept as a v3 response either - but
		// a v1/v2c datagram on a v3 session is not ours to judge.
		if isV3(msg) {
			return "malformed"
		}
		return ""
	}
	level := h.flags & g.AuthPriv
	if level < c.need && !(h.plaintext && h.pduTag == byte(g.Report)) {
		return "unauthenticated"
	}
	if level&g.AuthNoPriv != 0 && len(h.authParams) != c.digestLen {
		return "unauthenticated"
	}
	return ""
}

type v3Header struct {
	flags      g.SnmpV3MsgFlags
	authParams []byte
	plaintext  bool
	pduTag     byte
}

// parseV3Header reads the parts of an SNMPv3 message USM's checks need. A
// lenient BER reader, not encoding/asn1: some embedded agents use long-form
// lengths DER forbids, and a guard stricter than gosnmp would either break
// those agents or, failing open, let an oddly-encoded forgery past.
func parseV3Header(b []byte) (v3Header, bool) {
	var h v3Header
	msg, _, ok := berTLV(b, 0x30)
	if !ok {
		return h, false
	}
	ver, rest, ok := berTLV(msg, 0x02)
	if !ok || len(ver) != 1 || ver[0] != 3 {
		return h, false
	}
	hdr, rest, ok := berTLV(rest, 0x30)
	if !ok {
		return h, false
	}
	_, hr, ok := berTLV(hdr, 0x02) // msgID
	if !ok {
		return h, false
	}
	_, hr, ok = berTLV(hr, 0x02) // msgMaxSize
	if !ok {
		return h, false
	}
	flags, hr, ok := berTLV(hr, 0x04)
	if !ok || len(flags) != 1 {
		return h, false
	}
	model, _, ok := berTLV(hr, 0x02)
	if !ok || len(model) != 1 || model[0] != byte(g.UserSecurityModel) {
		return h, false
	}
	h.flags = g.SnmpV3MsgFlags(flags[0])

	secOctets, rest, ok := berTLV(rest, 0x04)
	if !ok {
		return h, false
	}
	sec, _, ok := berTLV(secOctets, 0x30)
	if !ok {
		return h, false
	}
	for _, tag := range []byte{0x04, 0x02, 0x02, 0x04} { // engine ID, boots, time, user
		if _, sec, ok = berTLV(sec, tag); !ok {
			return h, false
		}
	}
	if h.authParams, _, ok = berTLV(sec, 0x04); !ok {
		return h, false
	}

	if len(rest) > 0 && rest[0] == 0x30 { // plaintext scoped PDU
		scoped, _, ok := berTLV(rest, 0x30)
		if !ok {
			return h, false
		}
		for i := 0; i < 2; i++ { // contextEngineID, contextName
			if _, scoped, ok = berTLV(scoped, 0x04); !ok {
				return h, false
			}
		}
		if len(scoped) == 0 {
			return h, false
		}
		h.plaintext, h.pduTag = true, scoped[0]
	}
	return h, true
}

// isV3 reads only as far as the version, so a truncated v3 datagram is still
// recognised - and refused - rather than passed through as "not ours".
func isV3(b []byte) bool {
	if len(b) < 2 || b[0] != 0x30 {
		return false
	}
	i := 2
	if b[1]&0x80 != 0 {
		i += int(b[1] & 0x7f)
	}
	if i > len(b) {
		return false
	}
	ver, _, ok := berTLV(b[i:], 0x02)
	return ok && len(ver) == 1 && ver[0] == 3
}

// berTLV reads one tag-length-value with the given tag, accepting short and
// long-form definite lengths. It returns the value and what follows it.
func berTLV(b []byte, tag byte) (value, rest []byte, ok bool) {
	if len(b) < 2 || b[0] != tag {
		return nil, nil, false
	}
	n, i := int(b[1]), 2
	if n&0x80 != 0 {
		octets := n & 0x7f
		if octets == 0 || octets > 4 || len(b) < 2+octets {
			return nil, nil, false
		}
		n = 0
		for _, o := range b[2 : 2+octets] {
			n = n<<8 | int(o)
		}
		i = 2 + octets
	}
	if n < 0 || len(b)-i < n {
		return nil, nil, false
	}
	return b[i : i+n], b[i+n:], true
}
