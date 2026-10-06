package snmp

import (
	"testing"

	g "github.com/gosnmp/gosnmp"
)

// ber builds one tag-length-value; long forces a long-form length, as some
// embedded agents send.
func ber(tag byte, long bool, parts ...[]byte) []byte {
	var v []byte
	for _, p := range parts {
		v = append(v, p...)
	}
	if long || len(v) > 127 {
		return append([]byte{tag, 0x82, byte(len(v) >> 8), byte(len(v))}, v...)
	}
	return append([]byte{tag, byte(len(v))}, v...)
}

func v3Msg(flags g.SnmpV3MsgFlags, digest []byte, pduTag byte, long bool) []byte {
	usmParams := ber(0x30, long, ber(0x04, false, []byte("engine-01")), ber(0x02, false, []byte{1}),
		ber(0x02, false, []byte{1}), ber(0x04, false, []byte("dcim-poll")),
		ber(0x04, long, digest), ber(0x04, false))
	scoped := ber(0x30, long, ber(0x04, false), ber(0x04, false), ber(pduTag, false, []byte{0x02, 0x01, 0x01}))
	return ber(0x30, long, ber(0x02, false, []byte{3}),
		ber(0x30, false, ber(0x02, false, []byte{7}), ber(0x02, false, []byte{0x7f}),
			ber(0x04, false, []byte{byte(flags)}), ber(0x02, false, []byte{3})),
		ber(0x04, long, usmParams), scoped)
}

func TestTheUSMGuardRefusesWhatGosnmpWouldAccept(t *testing.T) {
	c := &usmGuardConn{need: g.AuthPriv, digestLen: 24}
	full, short := make([]byte, 24), []byte{0x5a}
	response, report := byte(g.GetResponse), byte(g.Report)
	for _, tc := range []struct {
		name string
		msg  []byte
		want string
	}{
		{"noAuthNoPriv response", v3Msg(g.NoAuthNoPriv, nil, response, false), "unauthenticated"},
		{"noAuthNoPriv response, long-form lengths", v3Msg(g.NoAuthNoPriv, nil, response, true), "unauthenticated"},
		{"authNoPriv response below authPriv", v3Msg(g.AuthNoPriv, full, response, false), "unauthenticated"},
		{"one-byte digest", v3Msg(g.AuthNoPriv, short, report, false), "unauthenticated"},
		{"discovery report, unauthenticated", v3Msg(g.NoAuthNoPriv, nil, report, false), ""},
		{"time-window report, authNoPriv", v3Msg(g.AuthNoPriv, full, report, false), ""},
		{"truncated v3", v3Msg(g.NoAuthNoPriv, nil, response, false)[:20], "malformed"},
		{"v2c datagram", ber(0x30, false, ber(0x02, false, []byte{1}), ber(0x04, false, []byte("x"))), ""},
	} {
		if got := c.refuse(tc.msg); got != tc.want {
			t.Errorf("%s: refuse = %q, want %q", tc.name, got, tc.want)
		}
	}
}
