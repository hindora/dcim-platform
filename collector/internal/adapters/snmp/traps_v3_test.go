package snmp

import (
	"context"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"

	g "github.com/gosnmp/gosnmp"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

func trapUser(auth, priv string) *models.Credential {
	return &models.Credential{Kind: "snmp_v3", Data: map[string]any{
		"security_name": "dcim-poll", "auth_protocol": "sha256", "auth_key": auth,
		"priv_protocol": "aes128", "priv_key": priv}}
}

func freeUDPPort(t *testing.T) int {
	t.Helper()
	c, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	return c.LocalAddr().(*net.UDPAddr).Port
}

// sendV3Trap sends one authPriv TRAP the way a device does: the device is the
// authoritative engine, so its own engine ID is in the message.
func sendV3Trap(t *testing.T, port int, engineID, auth, priv string) {
	t.Helper()
	c := &g.GoSNMP{
		Target: "127.0.0.1", Port: uint16(port), Version: g.Version3,
		SecurityModel: g.UserSecurityModel, MsgFlags: g.AuthPriv, Timeout: 2 * time.Second,
		SecurityParameters: &g.UsmSecurityParameters{
			UserName: "dcim-poll", AuthenticationProtocol: g.SHA256, AuthenticationPassphrase: auth,
			PrivacyProtocol: g.AES, PrivacyPassphrase: priv,
			AuthoritativeEngineID: engineID, AuthoritativeEngineBoots: 1, AuthoritativeEngineTime: 1,
		},
	}
	if err := c.Connect(); err != nil {
		t.Fatal(err)
	}
	defer c.Conn.Close()
	if _, err := c.SendTrap(g.SnmpTrap{Variables: []g.SnmpPDU{
		{Name: ".1.3.6.1.2.1.1.3.0", Type: g.TimeTicks, Value: uint32(42)},
		{Name: ".1.3.6.1.6.3.1.1.4.1.0", Type: g.ObjectIdentifier, Value: ".1.3.6.1.6.3.1.1.5.3"},
	}}); err != nil {
		t.Fatal(err)
	}
}

// waitBound waits until something holds the port - the receiver's listener -
// rather than guessing how long a bind takes under a busy test run.
func waitBound(t *testing.T, port int) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		c, err := net.ListenPacket("udp", "127.0.0.1:"+strconv.Itoa(port))
		if err != nil {
			return
		}
		c.Close()
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("nothing bound 127.0.0.1:%d", port)
}

func waitReceived(r *TrapReceiver, want uint64) bool {
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if r.Received() >= want {
			return true
		}
		time.Sleep(20 * time.Millisecond)
	}
	return r.Received() >= want
}

// One user decodes v3 traps from every device that shares it - gosnmp
// re-localises the keys to each packet's engine ID - and a credential
// rotation re-keys the running receiver: the new key is accepted, the old
// one no longer is.
func TestV3TrapsFromManyEnginesAndARekeyWhileListening(t *testing.T) {
	r, _, _ := newHoldReceiver(t)
	port := freeUDPPort(t)
	r.listen = "127.0.0.1:" + strconv.Itoa(port)
	r.SetEngineID(receiverID)
	if err := r.SetUSM(trapUser("authpass123", "privpass123")); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = r.Listen(ctx) }()
	waitBound(t, port)

	apc := string([]byte{0x80, 0x00, 0x01, 0x3e, 0x01, 10, 52, 11, 25})
	vertiv := string([]byte{0x80, 0x00, 0x01, 0xdc, 0x01, 10, 52, 11, 26})
	sendV3Trap(t, port, apc, "authpass123", "privpass123")
	sendV3Trap(t, port, vertiv, "authpass123", "privpass123")
	if !waitReceived(r, 2) {
		t.Fatalf("received %d of 2 v3 traps from two engines", r.Received())
	}
	sendV3Trap(t, port, apc, "WRONGauth1", "privpass123")
	time.Sleep(300 * time.Millisecond)
	if r.Received() != 2 {
		t.Fatal("a trap with the wrong auth key was accepted")
	}

	// Rotation: the receiver is re-keyed while it runs.
	if err := r.SetUSM(trapUser("rotated-auth-9", "rotated-priv-9")); err != nil {
		t.Fatal(err)
	}
	time.Sleep(300 * time.Millisecond) // the old listener closes...
	waitBound(t, port)                 // ...and the re-keyed one binds
	sendV3Trap(t, port, apc, "rotated-auth-9", "rotated-priv-9")
	if !waitReceived(r, 3) {
		t.Fatal("the rotated key was not accepted after a re-key")
	}
	sendV3Trap(t, port, apc, "authpass123", "privpass123")
	time.Sleep(300 * time.Millisecond)
	if r.Received() != 3 {
		t.Fatal("the old key was still accepted after a re-key")
	}
}

func TestSetUSMIsANoOpWhenNothingChanged(t *testing.T) {
	r, _, _ := newHoldReceiver(t)
	_ = r.SetUSM(trapUser("authpass123", "privpass123"))
	<-r.reconfig // the first set is a change
	_ = r.SetUSM(trapUser("authpass123", "privpass123"))
	select {
	case <-r.reconfig:
		t.Fatal("an unchanged credential rebuilt the listener")
	default:
	}
	_ = r.SetUSM(nil)
	select {
	case <-r.reconfig:
	default:
		t.Fatal("dropping to v2c did not rebuild the listener")
	}
}

const receiverID = "\x80\x00\x1f\x88\x04col-test"

// A Close that lands before the bind must not leave a socket held and read by
// nothing. gosnmp's own listener did exactly that (found live: a collector
// re-keyed seconds after start stopped taking every trap); trapSocket
// returns without binding.
func TestTrapSocketCloseBeforeBindReturns(t *testing.T) {
	for i := 0; i < 20; i++ {
		s := newTrapSocket()
		s.Params = g.Default
		errCh := make(chan error, 1)
		s.Close()
		go func() { errCh <- s.Listen("127.0.0.1:0") }()
		select {
		case err := <-errCh:
			if err != nil {
				t.Fatal(err)
			}
		case <-time.After(time.Second):
			t.Fatal("Listen did not return after an early Close")
		}
	}
}

// An INFORM sent to an address other than the one the kernel would pick for
// the reply - a pool's trap VIP, here 127.0.0.2 - must be answered FROM that
// address. The sender's socket is connected to it, as pysnmp effectively is:
// a report or response from any other source is dropped, the engine-ID
// discovery never completes, and the INFORM is never acknowledged. That was
// every INFORM through a trap VIP until the receiver replied from the
// datagram's own destination (IP_PKTINFO). Linux only: other platforms have
// no IP_PKTINFO here and the kernel still picks.
func TestV3InformThroughAnAliasIsAcknowledgedFromThatAlias(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("IP_PKTINFO source selection is Linux-only")
	}
	r, _, _ := newHoldReceiver(t)
	port := freeUDPPort(t)
	r.listen = "0.0.0.0:" + strconv.Itoa(port)
	r.SetEngineID(string([]byte{0x80, 0x00, 0x1f, 0x88, 0x04}) + "col-test")
	if err := r.SetUSM(trapUser("auth-pass-1", "priv-pass-1")); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = r.Listen(ctx) }()
	waitBound(t, port)

	c := &g.GoSNMP{
		Target: "127.0.0.2", Port: uint16(port), Version: g.Version3,
		SecurityModel: g.UserSecurityModel, MsgFlags: g.AuthPriv,
		Timeout: 2 * time.Second, Retries: 1,
		SecurityParameters: &g.UsmSecurityParameters{
			UserName: "dcim-poll", AuthenticationProtocol: g.SHA256, AuthenticationPassphrase: "auth-pass-1",
			PrivacyProtocol: g.AES, PrivacyPassphrase: "priv-pass-1",
		},
	}
	if err := c.Connect(); err != nil {
		t.Fatal(err)
	}
	defer c.Conn.Close()
	if _, err := c.SendTrap(g.SnmpTrap{IsInform: true, Variables: []g.SnmpPDU{
		{Name: ".1.3.6.1.2.1.1.3.0", Type: g.TimeTicks, Value: uint32(42)},
		{Name: ".1.3.6.1.6.3.1.1.4.1.0", Type: g.ObjectIdentifier, Value: ".1.3.6.1.6.3.1.1.5.3"},
	}}); err != nil {
		t.Fatalf("INFORM through 127.0.0.2 not acknowledged: %v", err)
	}
	if !waitReceived(r, 1) {
		t.Fatal("acknowledged INFORM never reached the handler")
	}
}

// Interop with the simulator's SNMP stack: a pysnmp INFORM (engine discovery,
// then authPriv) against the real receiver, which must acknowledge it and
// read the notification's OID. Opt-in - set PYSNMP_PY to a Python with pysnmp
// and cryptography - because CI has no pysnmp.
func TestPysnmpInformIsAcknowledged(t *testing.T) {
	py := os.Getenv("PYSNMP_PY")
	if py == "" {
		t.Skip("PYSNMP_PY not set")
	}
	r, _, _ := newHoldReceiver(t)
	port := freeUDPPort(t)
	r.listen = "127.0.0.1:" + strconv.Itoa(port)
	r.SetEngineID(string([]byte{0x80, 0x00, 0x1f, 0x88, 0x04}) + "col-test")
	if err := r.SetUSM(trapUser("auth-pass-1", "priv-pass-1")); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = r.Listen(ctx) }()
	waitBound(t, port)

	script := filepath.Join(t.TempDir(), "inform.py")
	if err := os.WriteFile(script, []byte(`
import asyncio, sys
from pysnmp.hlapi.v3arch.asyncio import *
from pysnmp.proto.rfc1902 import OctetString
async def main():
    eng = SnmpEngine(snmpEngineID=OctetString(hexValue="8000013e010a340b19"))
    u = UsmUserData("dcim-poll", "auth-pass-1", "priv-pass-1",
                    authProtocol=usmHMAC192SHA256AuthProtocol, privProtocol=usmAesCfb128Protocol)
    t = await UdpTransportTarget.create(("127.0.0.1", int(sys.argv[1])), timeout=2, retries=0)
    e, s, i, vb = await send_notification(eng, u, t, ContextData(), "inform",
                                          NotificationType(ObjectIdentity("1.3.6.1.6.3.1.1.5.3")))
    print("RESULT", e or s or "ACKED")
asyncio.run(main())
`), 0o600); err != nil {
		t.Fatal(err)
	}
	out, err := exec.Command(py, script, strconv.Itoa(port)).CombinedOutput()
	if err != nil || !strings.Contains(string(out), "RESULT ACKED") {
		t.Fatalf("pysnmp INFORM not acknowledged: %v; output: %s", err, out)
	}
	if !waitReceived(r, 1) {
		t.Fatal("acknowledged INFORM never reached the handler")
	}
	time.Sleep(100 * time.Millisecond)
	fams, _ := r.mets.Registry.Gather()
	for _, f := range fams {
		if f.GetName() != "dcim_collector_traps_received_total" {
			continue
		}
		for _, m := range f.GetMetric() {
			if m.GetLabel()[0].GetValue() == "no_trap_oid" {
				t.Fatal("the INFORM's snmpTrapOID was not read")
			}
		}
	}
}
