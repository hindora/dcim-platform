package snmp

import (
	"bufio"
	"context"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	g "github.com/gosnmp/gosnmp"

	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

const (
	agentUser = "dcim-poll"
	agentAuth = "auth-passphrase-1"
	agentPriv = "priv-passphrase-1"

	oidUnknownEngineIDs = ".1.3.6.1.6.3.15.1.1.4.0"
	oidNotInTimeWindows = ".1.3.6.1.6.3.15.1.1.2.0"
	oidWrongDigests     = ".1.3.6.1.6.3.15.1.1.5.0"

	oidUnavailableContexts = ".1.3.6.1.6.3.12.1.4.0"
)

// usmAgent is the authoritative side of RFC 3414, as much of it as a poller
// meets: discovery, the timeliness check, an unknown engine, and a GET.
type usmAgent struct {
	t    *testing.T
	conn *net.UDPConn

	mu       sync.Mutex
	engineID string
	boots    uint32
	since    time.Time // engine time is seconds since this
	// Answer a stale engine ID with a wrong-digest report, as some vendor
	// agents do, instead of usmStatsUnknownEngineIDs.
	digestOnUnknownEngine bool
	silent                bool

	discoveries, engineReports, timeReports, contextReports, gets int
}

func newUSMAgent(t *testing.T, engineID string) *usmAgent {
	t.Helper()
	conn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	a := &usmAgent{t: t, conn: conn, engineID: engineID, boots: 1, since: time.Now()}
	t.Cleanup(func() { _ = conn.Close() })
	go a.serve()
	return a
}

func (a *usmAgent) port() int { return a.conn.LocalAddr().(*net.UDPAddr).Port }

func (a *usmAgent) reboot() {
	a.mu.Lock()
	a.boots++
	a.since = time.Now()
	a.mu.Unlock()
}

func (a *usmAgent) swapCard(engineID string) {
	a.mu.Lock()
	a.engineID, a.boots, a.since = engineID, 1, time.Now()
	a.mu.Unlock()
}

func (a *usmAgent) counts() (discoveries, engineReports, timeReports, gets int) {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.discoveries, a.engineReports, a.timeReports, a.gets
}

func (a *usmAgent) usm(engineID string) *g.UsmSecurityParameters {
	return &g.UsmSecurityParameters{
		UserName:                 agentUser,
		AuthenticationProtocol:   g.SHA256,
		AuthenticationPassphrase: agentAuth,
		PrivacyProtocol:          g.AES,
		PrivacyPassphrase:        agentPriv,
		AuthoritativeEngineID:    engineID,
	}
}

func (a *usmAgent) serve() {
	buf := make([]byte, 65535)
	for {
		n, peer, err := a.conn.ReadFromUDP(buf)
		if err != nil {
			return
		}
		a.handle(append([]byte(nil), buf[:n]...), peer)
	}
}

func (a *usmAgent) handle(msg []byte, peer *net.UDPAddr) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.silent {
		return
	}
	decoder := &g.GoSNMP{Version: g.Version3, SecurityModel: g.UserSecurityModel,
		MsgFlags: g.AuthPriv, SecurityParameters: a.usm(a.engineID)}
	req, err := decoder.UnmarshalTrap(msg, true)
	if err != nil {
		a.t.Logf("agent: undecodable request: %v", err)
		return
	}
	in := req.SecurityParameters.(*g.UsmSecurityParameters)
	now := uint32(time.Since(a.since) / time.Second)

	switch {
	case req.MsgFlags&g.AuthNoPriv == 0 && in.AuthoritativeEngineID == "":
		a.discoveries++
		a.reply(req, peer, g.NoAuthNoPriv, g.Report, oidUnknownEngineIDs, "")
	case in.AuthoritativeEngineID != a.engineID:
		a.engineReports++
		if a.digestOnUnknownEngine {
			a.reply(req, peer, g.NoAuthNoPriv, g.Report, oidWrongDigests, "")
		} else {
			a.reply(req, peer, g.NoAuthNoPriv, g.Report, oidUnknownEngineIDs, "")
		}
	case in.AuthoritativeEngineBoots != a.boots || absDiff(in.AuthoritativeEngineTime, now) > 150:
		a.timeReports++
		a.reply(req, peer, g.AuthNoPriv, g.Report, oidNotInTimeWindows, agentUser)
	case req.ContextEngineID != a.engineID:
		// RFC 3413 3.2: a scoped PDU for a context engine this agent is not
		// is refused - net-snmp and pysnmp both answer with a report.
		a.contextReports++
		a.reply(req, peer, g.AuthNoPriv, g.Report, oidUnavailableContexts, agentUser)
	default:
		a.gets++
		a.reply(req, peer, g.AuthPriv, g.GetResponse, sysUpTimeOID, agentUser)
	}
}

func (a *usmAgent) reply(req *g.SnmpPacket, peer *net.UDPAddr, flags g.SnmpV3MsgFlags,
	pdu g.PDUType, oid, user string) {
	sp := a.usm(a.engineID)
	sp.UserName = user
	sp.AuthoritativeEngineBoots = a.boots
	sp.AuthoritativeEngineTime = uint32(time.Since(a.since) / time.Second)
	if err := sp.InitSecurityKeys(); err != nil {
		a.t.Errorf("agent keys: %v", err)
		return
	}
	vb := g.SnmpPDU{Name: oid, Type: g.Counter32, Value: uint32(1)}
	if pdu == g.GetResponse {
		vb = g.SnmpPDU{Name: oid, Type: g.TimeTicks, Value: uint32(12345)}
	}
	out := &g.SnmpPacket{Version: g.Version3, MsgFlags: flags, SecurityModel: g.UserSecurityModel,
		SecurityParameters: sp, MsgID: req.MsgID, RequestID: req.RequestID, MsgMaxSize: 65507,
		ContextEngineID: a.engineID, PDUType: pdu, Variables: []g.SnmpPDU{vb}}
	if err := sp.InitPacket(out); err != nil {
		a.t.Errorf("agent salt: %v", err)
		return
	}
	b, err := out.MarshalMsg()
	if err != nil {
		a.t.Errorf("agent marshal: %v", err)
		return
	}
	_, _ = a.conn.WriteToUDP(b, peer)
}

func agentEndpoint(port int) *models.Endpoint {
	return &models.Endpoint{
		ID: "ep-v3", Protocol: "snmp", Address: "127.0.0.1", Port: port,
		Poll: models.PollProfile{TimeoutMs: 400},
		Credential: v3Cred(map[string]any{
			"security_name": agentUser, "auth_protocol": "sha256", "auth_key": agentAuth,
			"priv_protocol": "aes", "priv_key": agentPriv,
		}),
	}
}

func pingN(t *testing.T, a *Adapter, ep *models.Endpoint, n int) {
	t.Helper()
	for i := 0; i < n; i++ {
		if err := a.Ping(context.Background(), ep); err != nil {
			t.Fatalf("ping %d: %v", i+1, err)
		}
	}
}

func engineCount(m *obs.Metrics, result string) int {
	families, err := m.Registry.Gather()
	if err != nil {
		panic(err)
	}
	for _, f := range families {
		if f.GetName() != "dcim_collector_snmp_v3_engine_total" {
			continue
		}
		for _, metric := range f.GetMetric() {
			for _, l := range metric.GetLabel() {
				if l.GetName() == "result" && l.GetValue() == result {
					return int(metric.GetCounter().GetValue())
				}
			}
		}
	}
	return 0
}

// The point of the cache: one discovery, then every session goes straight
// to the request - one exchange per poll instead of two.
func TestV3DiscoversOnceThenPollsFromTheCachedEngine(t *testing.T) {
	agent := newUSMAgent(t, "\x80\x00\x01\x3e\x01\x0a\x34\x0b\x19")
	mets := obs.NewMetrics()
	a := New(nil, nil, mets, 25, false)
	pingN(t, a, agentEndpoint(agent.port()), 5)

	d, e, w, gets := agent.counts()
	if agent.contextReports != 0 {
		t.Fatalf("%d sessions sent the wrong contextEngineID", agent.contextReports)
	}
	if d != 1 || e != 0 || w != 0 || gets != 5 {
		t.Fatalf("discoveries=%d engineReports=%d timeReports=%d gets=%d, want 1/0/0/5", d, e, w, gets)
	}
	if engineCount(mets, "discovered") != 1 || engineCount(mets, "cached") != 4 {
		t.Fatalf("discovered=%d cached=%d, want 1/4",
			engineCount(mets, "discovered"), engineCount(mets, "cached"))
	}
}

// A reboot moves snmpEngineBoots: the agent reports the cached values out of
// the time window, gosnmp takes the new ones and retransmits, and the poll
// succeeds without another discovery.
func TestV3ARebootedAgentIsRelearnedFromItsTimeWindowReport(t *testing.T) {
	agent := newUSMAgent(t, "\x80\x00\x01\x3e\x01\x0a\x34\x0b\x1a")
	mets := obs.NewMetrics()
	a := New(nil, nil, mets, 25, false)
	ep := agentEndpoint(agent.port())
	pingN(t, a, ep, 1)
	agent.reboot()
	pingN(t, a, ep, 3)

	d, _, w, gets := agent.counts()
	if d != 1 || w != 1 || gets != 4 {
		t.Fatalf("discoveries=%d timeReports=%d gets=%d, want 1/1/4", d, w, gets)
	}
	if engineCount(mets, "refreshed") != 1 || engineCount(mets, "cached") != 2 {
		t.Fatalf("refreshed=%d cached=%d, want 1/2",
			engineCount(mets, "refreshed"), engineCount(mets, "cached"))
	}
}

// A swapped card has a new engine ID, whether the agent reports it as an
// unknown engine or (as some vendors do) as a wrong digest. Either way the
// poll succeeds from a fresh discovery and the next one is cached again.
func TestV3ASwappedCardIsRelearned(t *testing.T) {
	for _, digest := range []bool{false, true} {
		agent := newUSMAgent(t, "\x80\x00\x01\x3e\x01\x0a\x34\x0b\x1b")
		agent.digestOnUnknownEngine = digest
		mets := obs.NewMetrics()
		a := New(nil, nil, mets, 25, false)
		ep := agentEndpoint(agent.port())
		pingN(t, a, ep, 1)
		agent.swapCard("\x80\x00\x01\xdc\x01\x0a\x34\x0b\x1b")
		pingN(t, a, ep, 3)

		d, e, _, gets := agent.counts()
		// Both end in a fresh discovery: even when gosnmp retransmits with
		// the new engine ID, the session's contextEngineID still names the
		// old card, so the agent refuses that too and the fallback runs.
		wantD := 2
		if d != wantD || e < 1 || gets != 4 {
			t.Fatalf("digest=%v: discoveries=%d engineReports=%d gets=%d, want %d/1/4",
				digest, d, e, gets, wantD)
		}
		if engineCount(mets, "refreshed") != 1 || engineCount(mets, "cached") != 2 {
			t.Fatalf("digest=%v: refreshed=%d cached=%d, want 1/2", digest,
				engineCount(mets, "refreshed"), engineCount(mets, "cached"))
		}
	}
}

// A silent agent is not retried from a fresh discovery - nobody answered -
// but its entry is dropped, so whatever comes back is discovered afresh.
func TestV3ATimeoutDropsTheCachedEngine(t *testing.T) {
	agent := newUSMAgent(t, "\x80\x00\x01\x3e\x01\x0a\x34\x0b\x1c")
	a := New(nil, nil, obs.NewMetrics(), 25, false)
	ep := agentEndpoint(agent.port())
	pingN(t, a, ep, 1)

	agent.mu.Lock()
	agent.silent = true
	agent.mu.Unlock()
	if err := a.Ping(context.Background(), ep); err == nil {
		t.Fatal("ping to a silent agent succeeded")
	}
	if a.engines.apply(engineKey(ep), &g.UsmSecurityParameters{}, time.Now()) {
		t.Fatal("cached engine survived a timeout")
	}

	agent.mu.Lock()
	agent.silent = false
	agent.mu.Unlock()
	pingN(t, a, ep, 1)
	if d, _, _, _ := agent.counts(); d != 2 {
		t.Fatalf("discoveries=%d after the agent came back, want 2", d)
	}
}

// The cached time is advanced by local elapsed time, or a poll interval
// longer than the 150 s window would trip a time-window report every time.
func TestEngineCacheAdvancesTheAgentsTime(t *testing.T) {
	c := newEngineCache()
	t0 := time.Now()
	c.learn("a:161", &g.UsmSecurityParameters{AuthoritativeEngineID: "e",
		AuthoritativeEngineBoots: 3, AuthoritativeEngineTime: 1000}, t0)
	sp := &g.UsmSecurityParameters{}
	if !c.apply("a:161", sp, t0.Add(10*time.Minute)) {
		t.Fatal("no entry")
	}
	if sp.AuthoritativeEngineTime != 1600 || sp.AuthoritativeEngineBoots != 3 {
		t.Fatalf("time=%d boots=%d, want 1600/3", sp.AuthoritativeEngineTime, sp.AuthoritativeEngineBoots)
	}
}

// pysnmpAgent is the simulator's SNMP stack as a v3 agent: the peer the
// fake agent above stands in for, so that what the fake does not check (it
// once ignored the contextEngineID; 0.5.5 shipped that) is checked by a real
// one. Opt-in via PYSNMP_PY, which CI sets.
const pysnmpAgent = `
import sys
from pysnmp.entity import engine, config
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.proto.rfc1902 import OctetString
snmp = engine.SnmpEngine(snmpEngineID=OctetString(hexValue="8000013e010a340b19"))
config.add_transport(snmp, udp.DOMAIN_NAME,
                     udp.UdpTransport().open_server_mode(("127.0.0.1", int(sys.argv[1]))))
config.add_v3_user(snmp, "dcim-poll", config.USM_AUTH_HMAC192_SHA256, "auth-passphrase-1",
                   config.USM_PRIV_CFB128_AES, "priv-passphrase-1")
config.add_vacm_user(snmp, 3, "dcim-poll", "authPriv", (1, 3, 6), (1, 3, 6))
ctx = context.SnmpContext(snmp)
cmdrsp.GetCommandResponder(snmp, ctx)
cmdrsp.NextCommandResponder(snmp, ctx)
print("READY", flush=True)
snmp.open_dispatcher()
`

func TestPysnmpAgentIsPolledFromTheCachedEngine(t *testing.T) {
	py := os.Getenv("PYSNMP_PY")
	if py == "" {
		t.Skip("PYSNMP_PY not set")
	}
	port := freeUDPPort(t)
	script := filepath.Join(t.TempDir(), "agent.py")
	if err := os.WriteFile(script, []byte(pysnmpAgent), 0o600); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(py, script, strconv.Itoa(port))
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = cmd.Process.Kill(); _ = cmd.Wait() }()
	ready := make(chan bool, 1)
	go func() {
		line, _ := bufio.NewReader(stdout).ReadString('\n')
		ready <- strings.HasPrefix(line, "READY")
	}()
	select {
	case ok := <-ready:
		if !ok {
			t.Fatal("pysnmp agent did not start")
		}
	case <-time.After(20 * time.Second):
		t.Fatal("pysnmp agent did not start within 20 s")
	}

	mets := obs.NewMetrics()
	a := New(nil, nil, mets, 25, false)
	pingN(t, a, agentEndpoint(port), 4)
	if d, c, r := engineCount(mets, "discovered"), engineCount(mets, "cached"),
		engineCount(mets, "refreshed"); d != 1 || c != 3 || r != 0 {
		t.Fatalf("discovered=%d cached=%d refreshed=%d, want 1/3/0 - a cached session "+
			"the agent refused falls back to discovery and counts as refreshed", d, c, r)
	}
}
