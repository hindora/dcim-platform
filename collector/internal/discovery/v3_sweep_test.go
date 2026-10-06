package discovery

import (
	"bufio"
	"context"
	"encoding/hex"
	"io"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gosnmp/gosnmp"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

// agentEngine is the pysnmp agent's engine ID: APC's enterprise (318),
// format 1, 10.52.11.25 - what the simulator gives a DC1 UPS.
const agentEngine = "8000013e010a340b19"

// pysnmpAgent serves SNMPv3 (user dcim-poll, SHA256/AES) and, if a community
// is given, v2c - the simulator's stack, the peer this sweep meets for real.
const pysnmpAgent = `
import sys
from pysnmp.entity import engine, config
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.proto.rfc1902 import OctetString
port, community = int(sys.argv[1]), sys.argv[2]
snmp = engine.SnmpEngine(snmpEngineID=OctetString(hexValue="` + agentEngine + `"))
config.add_transport(snmp, udp.DOMAIN_NAME, udp.UdpTransport().open_server_mode(("127.0.0.1", port)))
config.add_v3_user(snmp, "dcim-poll", config.USM_AUTH_HMAC192_SHA256, "auth-passphrase-1",
                   config.USM_PRIV_CFB128_AES, "priv-passphrase-1")
config.add_vacm_user(snmp, 3, "dcim-poll", "authPriv", (1, 3, 6), (1, 3, 6))
if community != "-":
    config.add_v1_system(snmp, "area", community)
    config.add_vacm_user(snmp, 2, "area", "noAuthNoPriv", (1, 3, 6), (1, 3, 6))
ctx = context.SnmpContext(snmp)
cmdrsp.GetCommandResponder(snmp, ctx)
cmdrsp.NextCommandResponder(snmp, ctx)
print("READY", flush=True)
snmp.open_dispatcher()
`

func v3Cred(auth string) *models.Credential {
	return &models.Credential{Kind: "snmp_v3", Data: map[string]any{
		"security_name": "dcim-poll", "auth_protocol": "sha256", "auth_key": auth,
		"priv_protocol": "aes128", "priv_key": "priv-passphrase-1"}}
}

func freePort(t *testing.T) int {
	t.Helper()
	c, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	return c.LocalAddr().(*net.UDPAddr).Port
}

// startAgent runs the pysnmp agent and waits until it answers USM discovery.
func startAgent(t *testing.T, community string) int {
	t.Helper()
	py := os.Getenv("PYSNMP_PY")
	if py == "" {
		t.Skip("PYSNMP_PY not set")
	}
	port := freePort(t)
	script := filepath.Join(t.TempDir(), "agent.py")
	if err := os.WriteFile(script, []byte(pysnmpAgent), 0o600); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(py, script, strconv.Itoa(port), community)
	out, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })
	if line, _ := bufio.NewReader(out).ReadString('\n'); !strings.HasPrefix(line, "READY") {
		t.Fatal("pysnmp agent did not start")
	}
	deadline := time.Now().Add(15 * time.Second)
	for {
		c := &gosnmp.GoSNMP{Target: "127.0.0.1", Port: uint16(port), Version: gosnmp.Version3,
			SecurityModel: gosnmp.UserSecurityModel, MsgFlags: gosnmp.NoAuthNoPriv,
			Timeout: 500 * time.Millisecond, SecurityParameters: &gosnmp.UsmSecurityParameters{UserName: "x"}}
		if c.Connect() == nil {
			_, _ = c.Get([]string{oidSysDescr})
			eng := c.SecurityParameters.(*gosnmp.UsmSecurityParameters).AuthoritativeEngineID
			_ = c.Conn.Close()
			if eng != "" {
				return port
			}
		}
		if time.Now().After(deadline) {
			t.Fatal("pysnmp agent never answered")
		}
		time.Sleep(200 * time.Millisecond)
	}
}

func sweeper(port int, communities []string, v3 ...V3Cred) *Sweeper {
	s := New(slog.New(slog.NewTextHandler(io.Discard, nil)), Communities(communities...), uint16(port))
	s.Timeout, s.Retries = 2*time.Second, 0
	s.V3 = func() []V3Cred { return v3 }
	return s
}

func sweepOne(t *testing.T, s *Sweeper) (Responder, bool) {
	t.Helper()
	found := s.Sweep(context.Background(), []string{"127.0.0.1"})
	if len(found) == 0 {
		return Responder{}, false
	}
	return found[0], true
}

func TestTheSweepAuthenticatesWithThePoolsV3Credential(t *testing.T) {
	port := startAgent(t, "-")
	r, ok := sweepOne(t, sweeper(port, []string{"wrong"}, V3Cred{PoolID: "pool-1", Cred: v3Cred("auth-passphrase-1")}))
	if !ok {
		t.Fatal("a v3 agent the pool credential opens was not found")
	}
	want := map[string]string{"version": "3", "credential": "pool", "pool_id": "pool-1",
		"engine_id": agentEngine, "port": strconv.Itoa(port)}
	for k, v := range want {
		if r.Access[k] != v {
			t.Errorf("access[%s] = %q, want %q", k, r.Access[k], v)
		}
	}
	if r.Identity["sysDescr"] == "" || r.Identity["engineID"] != agentEngine {
		t.Errorf("identity %v: want sysDescr and engineID", r.Identity)
	}
}

// v2c switched off and no credential accepted: still reported, as an agent
// that speaks v3, rather than an address that answers nothing.
func TestAV3OnlyAgentNoCredentialOpensIsStillFound(t *testing.T) {
	port := startAgent(t, "-")
	r, ok := sweepOne(t, sweeper(port, []string{"wrong"}, V3Cred{PoolID: "pool-1", Cred: v3Cred("not-the-key-1")}))
	if !ok {
		t.Fatal("a v3-only agent was reported as nothing")
	}
	if r.Access["version"] != "3" || r.Access["credential"] != "none" || r.Access["engine_id"] != agentEngine {
		t.Errorf("access %v: want version 3, credential none, the engine ID", r.Access)
	}
	if _, has := r.Access["pool_id"]; has {
		t.Error("a credential that did not authenticate is named as the pool's")
	}
}

// A network mid-migration: the agent answers v2c and speaks v3 too.
func TestAV2cAnswerCarriesTheV3EngineItAlsoSpeaks(t *testing.T) {
	port := startAgent(t, "secret-community")
	r, ok := sweepOne(t, sweeper(port, []string{"secret-community"},
		V3Cred{PoolID: "pool-1", Cred: v3Cred("not-the-key-1")}))
	if !ok {
		t.Fatal("agent not found")
	}
	if r.Access["version"] != "2c" || r.Access["v3_engine_id"] != agentEngine {
		t.Errorf("access %v: want v2c with the v3 engine ID", r.Access)
	}
}

func TestNoAgentNoResponder(t *testing.T) {
	if os.Getenv("PYSNMP_PY") == "" {
		t.Skip("PYSNMP_PY not set")
	}
	s := sweeper(freePort(t), []string{"public"}, V3Cred{PoolID: "pool-1", Cred: v3Cred("auth-passphrase-1")})
	s.Timeout = 300 * time.Millisecond
	if _, ok := sweepOne(t, s); ok {
		t.Fatal("an empty address was reported")
	}
}

func TestV3AccessNamesThePoolNotTheSecret(t *testing.T) {
	a := v3Access("pool-1", "\x80\x00\x01\x3e\x01", 0)
	if a["credential"] != "pool" || a["pool_id"] != "pool-1" || a["port"] != "161" ||
		a["engine_id"] != hex.EncodeToString([]byte("\x80\x00\x01\x3e\x01")) {
		t.Fatalf("access %v", a)
	}
	for _, v := range a {
		if strings.Contains(v, "passphrase") {
			t.Fatal("a secret travelled")
		}
	}
}
