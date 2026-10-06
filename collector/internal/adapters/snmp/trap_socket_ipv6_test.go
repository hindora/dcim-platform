package snmp

import (
	"net"
	"strconv"
	"sync/atomic"
	"testing"
	"time"

	g "github.com/gosnmp/gosnmp"
)

func TestTrapBindsFollowTheConfiguredAddress(t *testing.T) {
	// A wildcard is both families - what gosnmp's "udp" listener gave before
	// trapSocket bound udp4 only and IPv6 traps stopped arriving.
	cases := map[string][]string{
		"0.0.0.0:162":       {"udp4 0.0.0.0:162", "udp6 [::]:162"},
		":162":              {"udp4 0.0.0.0:162", "udp6 [::]:162"},
		"[::]:162":          {"udp4 0.0.0.0:162", "udp6 [::]:162"},
		"127.0.0.1:162":     {"udp4 127.0.0.1:162"},
		"[::1]:162":         {"udp6 [::1]:162"},
		"10.52.15.250:1162": {"udp4 10.52.15.250:1162"},
	}
	for addr, want := range cases {
		binds, err := trapBinds(addr)
		if err != nil {
			t.Fatalf("%s: %v", addr, err)
		}
		var got []string
		for _, b := range binds {
			got = append(got, b.network+" "+b.addr.String())
		}
		if len(got) != len(want) {
			t.Fatalf("%s: binds %v, want %v", addr, got, want)
		}
		for i := range want {
			if got[i] != want[i] {
				t.Errorf("%s: binds %v, want %v", addr, got, want)
			}
		}
	}
}

func hasIPv6Loopback(t *testing.T) {
	t.Helper()
	c, err := net.ListenUDP("udp6", &net.UDPAddr{IP: net.IPv6loopback})
	if err != nil {
		t.Skipf("no IPv6 loopback here: %v", err)
	}
	_ = c.Close()
}

// v3Socket is a receiver's socket on its own, so a test can bind port 0 and
// know when it is listening without probing for the bind.
func v3Socket(t *testing.T, addr string) (*trapSocket, int, *atomic.Int32) {
	t.Helper()
	r, _, _ := newHoldReceiver(t)
	r.SetEngineID(receiverID)
	if err := r.SetUSM(trapUser("auth-pass-1", "priv-pass-1")); err != nil {
		t.Fatal(err)
	}
	var got atomic.Int32
	s := newTrapSocket()
	s.Params = r.params()
	s.OnNewTrap = func(*g.SnmpPacket, *net.UDPAddr) { got.Add(1) }
	errCh := make(chan error, 1)
	go func() { errCh <- s.Listen(addr) }()
	select {
	case <-s.Listening():
	case err := <-errCh:
		t.Fatal(err)
	case <-time.After(5 * time.Second):
		t.Fatal("trap socket never bound")
	}
	t.Cleanup(s.Close)
	return s, s.conns[0].LocalAddr().(*net.UDPAddr).Port, &got
}

func informTo(t *testing.T, target string, port int) error {
	t.Helper()
	c := &g.GoSNMP{
		Target: target, Port: uint16(port), Version: g.Version3,
		SecurityModel: g.UserSecurityModel, MsgFlags: g.AuthPriv,
		Timeout: 2 * time.Second, Retries: 1,
		SecurityParameters: &g.UsmSecurityParameters{
			UserName: "dcim-poll", AuthenticationProtocol: g.SHA256, AuthenticationPassphrase: "auth-pass-1",
			PrivacyProtocol: g.AES, PrivacyPassphrase: "priv-pass-1",
		},
	}
	if err := c.Connect(); err != nil {
		return err
	}
	defer c.Conn.Close()
	_, err := c.SendTrap(g.SnmpTrap{IsInform: true, Variables: []g.SnmpPDU{
		{Name: ".1.3.6.1.2.1.1.3.0", Type: g.TimeTicks, Value: uint32(42)},
		{Name: ".1.3.6.1.6.3.1.1.4.1.0", Type: g.ObjectIdentifier, Value: ".1.3.6.1.6.3.1.1.5.3"},
	}})
	return err
}

// The configured "0.0.0.0" listener hears an INFORM over IPv6 as well as over
// IPv4, and acknowledges both - engine discovery and the response go back
// through each family's own socket.
func TestAWildcardReceiverTakesIPv6AndIPv4Informs(t *testing.T) {
	hasIPv6Loopback(t)
	s, port, got := v3Socket(t, "0.0.0.0:0")
	if len(s.conns) != 2 {
		t.Fatalf("wildcard bound %d sockets, want an IPv4 and an IPv6 one", len(s.conns))
	}
	if err := informTo(t, "::1", port); err != nil {
		t.Fatalf("INFORM over IPv6 not acknowledged: %v", err)
	}
	if err := informTo(t, "127.0.0.1", port); err != nil {
		t.Fatalf("INFORM over IPv4 not acknowledged: %v", err)
	}
	if n := got.Load(); n != 2 {
		t.Errorf("handler saw %d INFORMs, want 2", n)
	}
}

func TestAnIPv6AddressIsServedOnItsOwn(t *testing.T) {
	hasIPv6Loopback(t)
	s, port, got := v3Socket(t, "[::1]:0")
	if len(s.conns) != 1 {
		t.Fatalf("bound %d sockets for one IPv6 address", len(s.conns))
	}
	if err := informTo(t, "::1", port); err != nil {
		t.Fatalf("INFORM over IPv6 not acknowledged: %v", err)
	}
	if got.Load() != 1 {
		t.Error("IPv6 INFORM never reached the handler")
	}
}

// IPv6 is best effort for a wildcard: when its socket cannot be had, the
// receiver still serves IPv4 rather than failing to start.
func TestAWildcardWithoutIPv6StillServesIPv4(t *testing.T) {
	hasIPv6Loopback(t)
	held, err := net.ListenUDP("udp6", &net.UDPAddr{IP: net.IPv6unspecified})
	if err != nil {
		t.Fatal(err)
	}
	defer held.Close()
	port := held.LocalAddr().(*net.UDPAddr).Port
	s, _, got := v3Socket(t, "0.0.0.0:"+strconv.Itoa(port))
	if len(s.conns) != 1 {
		t.Fatalf("bound %d sockets; the IPv6 port was taken", len(s.conns))
	}
	if err := informTo(t, "127.0.0.1", port); err != nil {
		t.Fatalf("INFORM over IPv4 not acknowledged: %v", err)
	}
	if got.Load() != 1 {
		t.Error("IPv4 INFORM never reached the handler")
	}
}
