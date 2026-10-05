package app

import (
	"context"
	"io"
	"log/slog"
	"net"
	"os"
	"testing"
	"time"

	g "github.com/gosnmp/gosnmp"

	"github.com/hari/dcim-platform/collector/internal/assign"
	"github.com/hari/dcim-platform/collector/internal/config"
	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// The receiver startTraps launches - the one that actually runs - answers a
// sender's engine discovery with this collector's own engine ID. Found live:
// the engine ID was set only on a receiver startTraps then replaced, so the
// running one had none, the discovery probe went to the handler as a trap
// with no OID, and no SNMPv3 INFORM was ever acknowledged.
func TestTheRunningTrapReceiverAnswersEngineDiscovery(t *testing.T) {
	table, err := mapping.LoadTraps(os.DirFS("../../../contracts/mappings"))
	if err != nil {
		t.Fatal(err)
	}
	cfg := config.Default()
	cfg.Collector.ID = "col-test"
	a := &App{cfg: cfg, log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		mets: obs.NewMetrics(), trapTable: table, resolver: assign.NewResolver()}

	probe, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer probe.Close()
	port := freePort(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.startTraps(ctx, config.TrapCfg{Enabled: true, Listen: net.JoinHostPort("127.0.0.1", port),
		Workers: 1, RateLimitPerMinute: 1000})
	defer a.stopTraps()

	want := receiverEngineID("col-test")
	if got := a.traps.EngineID(); got != want {
		t.Fatalf("running receiver engine ID %x, want %x", got, want)
	}
	// The pool's v3 user, as syncTrapUSM hands it over on an assignment.
	if err := a.traps.SetUSM(&models.Credential{Kind: "snmp_v3", Data: map[string]any{
		"security_name": "dcim-poll", "auth_protocol": "sha256", "auth_key": "auth-pass-1",
		"priv_protocol": "aes128", "priv_key": "priv-pass-1"}}); err != nil {
		t.Fatal(err)
	}

	// USM discovery as any sender does it: noAuthNoPriv, reportable, no
	// engine ID. Retried briefly: the listener binds asynchronously.
	msg, err := (&g.SnmpPacket{Version: g.Version3, MsgFlags: g.Reportable | g.NoAuthNoPriv,
		SecurityModel: g.UserSecurityModel, SecurityParameters: &g.UsmSecurityParameters{},
		PDUType: g.GetRequest, MsgID: 1, RequestID: 2, MsgMaxSize: 65507}).MarshalMsg()
	if err != nil {
		t.Fatal(err)
	}
	dst, _ := net.ResolveUDPAddr("udp4", net.JoinHostPort("127.0.0.1", port))
	buf := make([]byte, 4096)
	for attempt := 0; attempt < 20; attempt++ {
		_, _ = probe.WriteTo(msg, dst)
		_ = probe.SetReadDeadline(time.Now().Add(150 * time.Millisecond))
		n, _, err := probe.ReadFrom(buf)
		if err != nil {
			continue
		}
		dec := &g.GoSNMP{Version: g.Version3, SecurityModel: g.UserSecurityModel,
			SecurityParameters: &g.UsmSecurityParameters{}}
		rep, err := dec.UnmarshalTrap(buf[:n], true)
		if err != nil {
			t.Fatalf("undecodable reply: %v", err)
		}
		if rep.PDUType != g.Report {
			t.Fatalf("reply PDU %v, want Report", rep.PDUType)
		}
		if got := rep.SecurityParameters.(*g.UsmSecurityParameters).AuthoritativeEngineID; got != want {
			t.Fatalf("reported engine ID %x, want %x", got, want)
		}
		return
	}
	t.Fatal("no report for an engine discovery probe")
}

func freePort(t *testing.T) string {
	t.Helper()
	c, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	_, port, _ := net.SplitHostPort(c.LocalAddr().String())
	return port
}
