package snmp

import (
	"context"
	"strconv"
	"testing"
	"time"

	g "github.com/gosnmp/gosnmp"
)

const deviceEngine = "\x80\x00\x01\x3e\x01\x0a\x34\x0b\x19"

func trapCount(r *TrapReceiver, result string) int {
	fams, _ := r.mets.Registry.Gather()
	for _, f := range fams {
		if f.GetName() != "dcim_collector_traps_received_total" {
			continue
		}
		for _, m := range f.GetMetric() {
			for _, l := range m.GetLabel() {
				if l.GetName() == "result" && l.GetValue() == result {
					return int(m.GetCounter().GetValue())
				}
			}
		}
	}
	return 0
}

func waitCount(r *TrapReceiver, result string, want int) bool {
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if trapCount(r, result) >= want {
			return true
		}
		time.Sleep(20 * time.Millisecond)
	}
	return false
}

// v3Receiver is a listening receiver with the test user and a known clock.
func v3Receiver(t *testing.T) (*TrapReceiver, int) {
	t.Helper()
	r, _, _ := newHoldReceiver(t)
	port := freeUDPPort(t)
	r.listen = "127.0.0.1:" + strconv.Itoa(port)
	r.SetEngineID(string([]byte{0x80, 0x00, 0x1f, 0x88, 0x04}) + "col-test")
	r.SetEngineClock(7, time.Now(), NewEngineTimes())
	if err := r.SetUSM(trapUser("auth-pass-1", "priv-pass-1")); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() { _ = r.Listen(ctx) }()
	waitBound(t, port)
	return r, port
}

func trapClient(t *testing.T, port int, flags g.SnmpV3MsgFlags, sp *g.UsmSecurityParameters) *g.GoSNMP {
	t.Helper()
	c := &g.GoSNMP{Target: "127.0.0.1", Port: uint16(port), Version: g.Version3,
		SecurityModel: g.UserSecurityModel, MsgFlags: flags, Timeout: 500 * time.Millisecond,
		Retries: 1, SecurityParameters: sp}
	if err := c.Connect(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Conn.Close() })
	return c
}

var linkDown = []g.SnmpPDU{
	{Name: ".1.3.6.1.2.1.1.3.0", Type: g.TimeTicks, Value: uint32(42)},
	{Name: ".1.3.6.1.6.3.1.1.4.1.0", Type: g.ObjectIdentifier, Value: ".1.3.6.1.6.3.1.1.5.3"},
}

func sendTrapAt(t *testing.T, port int, engineID string, boots, engineTime uint32) {
	t.Helper()
	c := trapClient(t, port, g.AuthPriv, &g.UsmSecurityParameters{
		UserName: "dcim-poll", AuthenticationProtocol: g.SHA256, AuthenticationPassphrase: "auth-pass-1",
		PrivacyProtocol: g.AES, PrivacyPassphrase: "priv-pass-1",
		AuthoritativeEngineID: engineID, AuthoritativeEngineBoots: boots, AuthoritativeEngineTime: engineTime,
	})
	if _, err := c.SendTrap(g.SnmpTrap{Variables: linkDown}); err != nil {
		t.Fatal(err)
	}
}

// gosnmp compares only the digest bytes a message carries. A v3 TRAP sent
// noAuthNoPriv under the user's name - no key needed, only the name - was
// accepted and raised its alarm.
func TestAnUnauthenticatedV3TrapIsRefused(t *testing.T) {
	r, port := v3Receiver(t)
	c := trapClient(t, port, g.NoAuthNoPriv, &g.UsmSecurityParameters{UserName: "dcim-poll",
		AuthoritativeEngineID: deviceEngine, AuthoritativeEngineBoots: 1, AuthoritativeEngineTime: 1})
	if _, err := c.SendTrap(g.SnmpTrap{Variables: linkDown}); err != nil {
		t.Fatal(err)
	}
	if !waitCount(r, rejectUnauthenticated, 1) {
		t.Fatal("unauthenticated v3 trap was not refused")
	}
	if r.Received() != 0 {
		t.Fatal("unauthenticated v3 trap reached the handler")
	}
}

// The same hole with the flags claiming authentication: gosnmp compares only
// as many digest bytes as the message carries, so a one-byte digest passes one
// time in 256 - a forgery in a few hundred packets. (An empty digest gosnmp
// itself fails to decode.) A digest must be the HMAC's full truncated length.
func TestAShortDigestIsRefused(t *testing.T) {
	s := newTrapSocket()
	var rejected []string
	s.OnReject = func(reason string) { rejected = append(rejected, reason) }
	s.Params = &g.GoSNMP{Version: g.Version3, SecurityModel: g.UserSecurityModel, MsgFlags: g.AuthPriv,
		SecurityParameters: &g.UsmSecurityParameters{AuthenticationProtocol: g.SHA256,
			AuthoritativeEngineID: "own-engine"}}
	own := s.Params.SecurityParameters.(*g.UsmSecurityParameters)
	for _, digest := range []string{"", "Z", string(make([]byte, 12)), string(make([]byte, 24))} {
		rejected = nil
		got := &g.UsmSecurityParameters{AuthoritativeEngineID: deviceEngine,
			AuthenticationParameters: digest, AuthoritativeEngineBoots: 1, AuthoritativeEngineTime: 1}
		ok := s.secure(&g.SnmpPacket{MsgFlags: g.AuthPriv, PDUType: g.SNMPv2Trap}, own, got, nil, nil, nil)
		if want := len(digest) == 24; ok != want {
			t.Fatalf("%d-byte digest: accepted=%v, want %v", len(digest), ok, want)
		}
		if !ok && (len(rejected) != 1 || rejected[0] != rejectUnauthenticated) {
			t.Fatalf("%d-byte digest: rejected as %v", len(digest), rejected)
		}
	}
}

// RFC 3414 3.2.7b: a TRAP more than 150 s behind the receiver's notion of
// the sender's clock, or from an earlier boot, is a replay. A reboot (more
// boots) is not.
func TestAReplayedV3TrapIsRefused(t *testing.T) {
	r, port := v3Receiver(t)
	sendTrapAt(t, port, deviceEngine, 5, 1000) // learned
	sendTrapAt(t, port, deviceEngine, 5, 900)  // 100 s behind: inside the window
	if !waitReceived(r, 2) {
		t.Fatalf("in-window traps: received %d, want 2", r.Received())
	}
	sendTrapAt(t, port, deviceEngine, 5, 700) // 300 s behind
	sendTrapAt(t, port, deviceEngine, 4, 5000)
	if !waitCount(r, rejectNotInTimeWindow, 2) {
		t.Fatalf("replays refused: %d, want 2", trapCount(r, rejectNotInTimeWindow))
	}
	sendTrapAt(t, port, deviceEngine, 6, 3) // the device rebooted
	if !waitReceived(r, 3) {
		t.Fatal("a trap after a reboot was refused")
	}
	if r.Received() != 3 {
		t.Fatalf("received %d, want 3", r.Received())
	}
}

// An INFORM from a sender holding a stale notion of this receiver's clock
// gets a notInTimeWindow report with the real boots and time; the sender
// resynchronises and resends, and the resend is accepted.
func TestAnInformWithAStaleClockIsResynchronised(t *testing.T) {
	r, port := v3Receiver(t)
	c := trapClient(t, port, g.AuthPriv, &g.UsmSecurityParameters{
		UserName: "dcim-poll", AuthenticationProtocol: g.SHA256, AuthenticationPassphrase: "auth-pass-1",
		PrivacyProtocol: g.AES, PrivacyPassphrase: "priv-pass-1",
		AuthoritativeEngineID:    r.EngineID(),
		AuthoritativeEngineBoots: 3, AuthoritativeEngineTime: 99999, // receiver is on boot 7
	})
	if _, err := c.SendTrap(g.SnmpTrap{IsInform: true, Variables: linkDown}); err != nil {
		t.Fatalf("INFORM after resync: %v", err)
	}
	if trapCount(r, rejectNotInTimeWindow) != 1 {
		t.Fatalf("stale INFORMs refused: %d, want 1", trapCount(r, rejectNotInTimeWindow))
	}
	if got := c.SecurityParameters.(*g.UsmSecurityParameters).AuthoritativeEngineBoots; got != 7 {
		t.Fatalf("sender resynchronised to boots %d, want 7", got)
	}
	if !waitReceived(r, 1) {
		t.Fatal("the resent INFORM never reached the handler")
	}
}

func TestEngineTimesFollowsTheSendersClock(t *testing.T) {
	e := NewEngineTimes()
	t0 := time.Now()
	if !e.Accept("e", 2, 600, t0) {
		t.Fatal("first sighting refused")
	}
	// A minute on, the sender's clock is ~660. Messages no newer than the
	// latest seen (600) are judged against it: 550 is 110 s behind (in), 500
	// is 160 s behind (out). A newer one simply advances the clock.
	t1 := t0.Add(time.Minute)
	if !e.Accept("e", 2, 550, t1) {
		t.Fatal("110 s behind refused")
	}
	if e.Accept("e", 2, 500, t1) {
		t.Fatal("160 s behind accepted")
	}
	if !e.Accept("e", 2, 650, t1) {
		t.Fatal("a newer message refused")
	}
	if e.Accept("e", 1, 999999, t1) {
		t.Fatal("an earlier boot accepted")
	}
	if !e.Accept("e", 3, 1, t1) {
		t.Fatal("a reboot refused")
	}
	if e.Accept("f", maxBoots, 1, t0) {
		t.Fatal("the boots latch value accepted")
	}
}
