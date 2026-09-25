// Package discovery sweeps management subnets for devices that answer SNMP.
//
// It runs on the collector because the collector is what sits on the
// management network. The API decides what should be swept and what the answer
// means; this only finds out who is there.
//
// Two things a sweep must get right or it becomes the problem it is looking
// for. It has to be bounded - an unbounded /16 is 65,536 probes and looks
// exactly like a port scan to anything watching - and it has to be slow enough
// not to matter, because discovery is a background audit, not an emergency.
package discovery

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"strings"
	"sync"
	"time"

	"github.com/gosnmp/gosnmp"

	snmpadapter "github.com/hari/dcim-platform/collector/internal/adapters/snmp"
)

const (
	// sysDescr, sysObjectID, sysName: enough to say what answered and to guess
	// what it is, and cheap enough that a wide sweep stays polite.
	oidSysDescr    = "1.3.6.1.2.1.1.1.0"
	oidSysObjectID = "1.3.6.1.2.1.1.2.0"
	oidSysName     = "1.3.6.1.2.1.1.5.0"

	// entPhysicalSerialNum for the first physical entity: the chassis serial.
	//
	// Worth one more OID in the probe because it is the only key that survives a
	// device being RE-ADDRESSED. Matching a responder to inventory by address
	// alone reported a moved machine as brand new, and promoting it created a
	// second record for one physical box.
	//
	// ENTITY-MIB indexes entPhysicalEntry by an arbitrary entity number, and .1 is
	// the chassis on the overwhelming majority of gear. A GET of the wrong index
	// returns noSuchInstance, which is handled like any other absent OID - a
	// device that does not answer it simply has no serial on its candidate, which
	// is why the column is nullable.
	oidEntSerial = "1.3.6.1.2.1.47.1.1.1.1.11.1"

	// Vendor serial OIDs, read only when the standard one answered nothing.
	//
	// ENTITY-MIB is a network-gear convention. Facility gear does not implement it
	// and the standard MIB for each class has no serial object to fall back on:
	// UPS-MIB (RFC 1628) stops at manufacturer, model and two software versions,
	// and neither Modbus nor BACnet defines a serial register at all. So a serial
	// for a PDU or a UPS lives in the vendor's own tree or nowhere, which is why a
	// real discovery tool reads sysObjectID first and then asks the vendor.
	//
	// Without this, 80 rack PDUs and every UPS on the plane came back with no
	// serial, and a serial is the only key that survives a device being
	// re-addressed - so a re-addressed PDU read as a brand new one.
	oidAPCrPDU2Serial = "1.3.6.1.4.1.318.1.1.26.2.1.7.1"  // rPDU2IdentSerialNumber
	oidAPCrPDUSerial  = "1.3.6.1.4.1.318.1.1.12.1.6.0"    // rPDUIdentSerialNumber
	oidAPCUPSSerial   = "1.3.6.1.4.1.318.1.1.1.1.2.3.0"   // upsBasicIdentSerialNumber
	oidRaritanSerial  = "1.3.6.1.4.1.13742.6.3.2.1.1.4.1" // pduSerialNumber
	oidLiebertSerial  = "1.3.6.1.4.1.476.1.42.2.1.4.0"    // lgpAgentIdentSerialNumber

	// Hard ceiling on addresses in one run. A /16 is refused rather than
	// truncated: silently sweeping the first 4096 of 65,536 and reporting
	// "found 12" would be a lie about what was audited.
	MaxAddresses = 4096

	// Matched to what the responder can actually serve, not to what the
	// network could carry.
	//
	// The first version used 2 s with no retries and 16 in flight, and found
	// 18 devices in a /24 that holds 105 - it lost 83% of them to timeouts.
	// A sweep that reports present devices as absent is worse than no sweep:
	// someone eventually acts on "this address answers nothing".
	//
	// Every agent on this plane is served by ONE process, so raising
	// concurrency does not raise throughput, it just deepens the queue behind
	// the same socket and times more probes out. Fewer in flight, waiting as
	// long as the pollers do, with a retry.
	defaultConcurrency = 8
	defaultTimeout     = 6 * time.Second
	defaultRetries     = 1
)

// Responder is one address that answered.
type Responder struct {
	Address  string            `json:"address"`
	Protocol string            `json:"protocol"`
	Identity map[string]string `json:"identity"`
}

// CommunityFor returns the communities to TRY for an address, in order.
//
// A list, not one string. Real discovery has no way to know which community a
// device it has never spoken to will accept, so it tries the site's candidates
// until one answers - and on SNMPv2c a wrong community is silence rather than an
// auth failure, so trying is the only way to find out.
//
// This was a single string returning the address itself, which is the SIMULATOR's
// convention: one snmpsim process serves every agent from one socket and routes by
// community. That is a fine thing to be able to configure and a wrong thing to
// hard-wire, which is what it was - against real gear the sweep would have found
// nothing at all.
type CommunityFor func(addr string) []string

// PerAddressCommunity answers with the address itself.
//
// Correct for a snmpsim-backed plane and for nothing else. Selected explicitly by
// configuration (`discovery.community_is_address`), never by default, so a
// deployment that needs it says so and every other deployment does not silently
// get it.
func PerAddressCommunity(addr string) []string { return []string{addr} }

// Communities tries a fixed list, in order. What a real site uses.
//
// Empty falls back to "public": a sweep with no configured community would
// otherwise probe every address with an empty one and report a silent network,
// which reads as "nothing is there" rather than "nobody told me how to ask".
func Communities(list ...string) CommunityFor {
	if len(list) == 0 {
		list = []string{"public"}
	}
	out := make([]string, len(list))
	copy(out, list)
	return func(string) []string { return out }
}

// Hosts expands CIDRs into probeable addresses, minus network and broadcast.
//
// Refuses rather than truncates past MaxAddresses, for the reason above.
func Hosts(cidrs []string) ([]string, error) {
	var out []string
	seen := map[string]bool{}
	for _, c := range cidrs {
		_, ipnet, err := net.ParseCIDR(c)
		if err != nil {
			return nil, fmt.Errorf("bad cidr %q: %w", c, err)
		}
		ones, bits := ipnet.Mask.Size()
		if bits != 32 {
			return nil, fmt.Errorf("only IPv4 is supported, got %q", c)
		}
		count := 1 << (bits - ones)
		if count > 2 {
			count -= 2 // network and broadcast are not hosts
		}
		if len(out)+count > MaxAddresses {
			return nil, fmt.Errorf(
				"%q would take the sweep past %d addresses; narrow the scope",
				c, MaxAddresses)
		}
		ip := ipnet.IP.Mask(ipnet.Mask).To4()
		for i := 0; i < count; i++ {
			cur := make(net.IP, 4)
			copy(cur, ip)
			// Skip the network address on anything wider than a /31.
			add(cur, uint32(i)+boundaryOffset(ones))
			s := cur.String()
			if !ipnet.Contains(cur) || seen[s] {
				continue
			}
			seen[s] = true
			out = append(out, s)
		}
	}
	return out, nil
}

func boundaryOffset(ones int) uint32 {
	if ones >= 31 {
		return 0
	}
	return 1
}

func add(ip net.IP, n uint32) {
	v := uint32(ip[0])<<24 | uint32(ip[1])<<16 | uint32(ip[2])<<8 | uint32(ip[3])
	v += n
	ip[0], ip[1], ip[2], ip[3] = byte(v>>24), byte(v>>16), byte(v>>8), byte(v)
}

// Sweeper probes addresses for an SNMP agent.
type Sweeper struct {
	Community   CommunityFor
	Port        uint16
	Timeout     time.Duration
	Retries     int
	Concurrency int
	Log         *slog.Logger
}

func New(log *slog.Logger, community CommunityFor, port uint16) *Sweeper {
	if port == 0 {
		port = 161
	}
	return &Sweeper{Community: community, Port: port, Timeout: defaultTimeout,
		Retries: defaultRetries, Concurrency: defaultConcurrency, Log: log}
}

// Sweep probes every address and returns those that answered.
func (s *Sweeper) Sweep(ctx context.Context, addrs []string) []Responder {
	conc := s.Concurrency
	if conc <= 0 {
		conc = defaultConcurrency
	}
	sem := make(chan struct{}, conc)
	var mu sync.Mutex
	var wg sync.WaitGroup
	out := make([]Responder, 0, 32)

	for _, addr := range addrs {
		if ctx.Err() != nil {
			break
		}
		wg.Add(1)
		sem <- struct{}{}
		go func(addr string) {
			defer wg.Done()
			defer func() { <-sem }()
			if r, ok := s.probe(ctx, addr); ok {
				mu.Lock()
				out = append(out, r)
				mu.Unlock()
			}
		}(addr)
	}
	wg.Wait()
	return out
}

// probe tries each candidate community until one answers.
//
// Sequential, and deliberately: the candidates are a short list, a sweep is a
// background audit rather than an emergency, and firing every community at every
// address at once would multiply the traffic by the length of the list for no
// gain. A device that answers the first one costs exactly what it did before.
func (s *Sweeper) probe(ctx context.Context, addr string) (Responder, bool) {
	for _, community := range s.Community(addr) {
		if r, ok := s.probeWith(ctx, addr, community); ok {
			return r, true
		}
		if ctx.Err() != nil {
			break
		}
	}
	return Responder{}, false
}

func (s *Sweeper) probeWith(ctx context.Context, addr, community string,
) (Responder, bool) {
	conn := &gosnmp.GoSNMP{
		Target: addr, Port: s.Port, Version: gosnmp.Version2c,
		Community: community, Timeout: s.Timeout, Retries: s.Retries,
		Context: ctx,
	}
	if err := conn.Connect(); err != nil {
		return Responder{}, false
	}
	// Same swap the pollers do: these agents answer from whichever source
	// address the kernel picks, and a connected UDP socket drops those replies,
	// so every probe would time out and the sweep would report an empty
	// network that is in fact full of devices.
	if err := snmpadapter.UseAnySourceSocket(conn, addr, int(s.Port)); err != nil {
		return Responder{}, false
	}
	defer func() { _ = conn.Conn.Close() }()

	res, err := conn.Get([]string{oidSysDescr, oidSysObjectID, oidSysName,
		oidEntSerial})
	if err != nil || res == nil || len(res.Variables) == 0 {
		return Responder{}, false
	}

	identity := map[string]string{}
	names := map[string]string{
		oidSysDescr: "sysDescr", oidSysObjectID: "sysObjectID", oidSysName: "sysName",
		oidEntSerial: "serial",
	}
	answered := false
	for _, v := range res.Variables {
		if v.Type == gosnmp.NoSuchObject || v.Type == gosnmp.NoSuchInstance {
			continue
		}
		key := names["."+trimLeadingDot(v.Name)]
		if key == "" {
			key = names[trimLeadingDot(v.Name)]
		}
		if key == "" {
			continue
		}
		switch v.Type {
		case gosnmp.OctetString:
			identity[key] = string(v.Value.([]byte))
		case gosnmp.ObjectIdentifier:
			identity[key] = fmt.Sprint(v.Value)
		default:
			identity[key] = fmt.Sprint(v.Value)
		}
		answered = true
	}
	if !answered {
		return Responder{}, false
	}
	// An agent that answers the OID with an empty string has told us nothing. Left
	// in, it would read downstream as "this device has no serial" rather than "it
	// did not say", and an empty serial matches no device while looking like a key.
	if strings.TrimSpace(identity["serial"]) == "" {
		delete(identity, "serial")
	}
	// Nothing at the standard OID: ask the vendor. Most of a facility plane lands
	// here - PDUs, UPSs and air handlers implement no ENTITY-MIB - and a candidate
	// with no serial can only be matched by address, which a re-addressing breaks.
	if _, ok := identity["serial"]; !ok {
		if sn := vendorSerial(conn, identity["sysObjectID"],
			identity["sysDescr"]); sn != "" {
			identity["serial"] = sn
		}
	}
	// A sweep answering sysDescr but nothing else is still a responder; only the
	// serial is optional here.
	return Responder{Address: addr, Protocol: "snmp", Identity: identity}, true
}

func trimLeadingDot(s string) string {
	if len(s) > 0 && s[0] == '.' {
		return s[1:]
	}
	return s
}

// Enterprise sysObjectID prefix to the serial OIDs that vendor publishes.
//
// A slice, not a map, because the match is by prefix and order matters within a
// vendor: APC's modern rPDU2 identity table is tried before the legacy rPDU one,
// and an AP88xx strip answers both.
//
// Short on purpose. Eaton, Server Technology and Schneider PowerLogic all expose
// a serial somewhere in their trees, but no MIB was available to pin the OID, and
// a wrong OID here is worse than a missing one: it reads a firmware version or a
// part number off the neighbouring leaf and files it as a serial, which then
// matches nothing for ever. Add a vendor when its OID can be confirmed.
var vendorSerialOIDs = []struct {
	prefix string
	oids   []string
}{
	// Rack PDUs, floor PDUs and UPSs all sit under the APC enterprise, and one
	// device answers only its own. Asking for all three costs one GET.
	//
	// UNVERIFIED column index on rPDU2IdentSerialNumber: .7 is where the serial
	// sits in the conventional rPDU2Ident table shape (Index, Name, HardwareRev,
	// FirmwareRev, DateOfManufacture, ModelNumber, SerialNumber) but no MIB was
	// available to confirm it. It is tried FIRST and a wrong leaf would win, so
	// the value is sanity-checked before it is used.
	{"1.3.6.1.4.1.318", []string{oidAPCrPDU2Serial, oidAPCrPDUSerial, oidAPCUPSSerial}},
	{"1.3.6.1.4.1.13742", []string{oidRaritanSerial}},
	{"1.3.6.1.4.1.476", []string{oidLiebertSerial}},
}

func serialOIDsFor(sysObjectID string) []string {
	id := trimLeadingDot(strings.TrimSpace(sysObjectID))
	if id == "" {
		return nil
	}
	for _, v := range vendorSerialOIDs {
		if id == v.prefix || strings.HasPrefix(id, v.prefix+".") {
			return v.oids
		}
	}
	return nil
}

// plausibleSerial rejects what an identity leaf read by mistake looks like.
//
// The vendor OIDs above are tried in order and the first non-empty string wins,
// so an off-by-one column would hand back a firmware version or a model number
// and it would be filed as a serial. Those are the two neighbours in every
// identity table this reads, and both are recognisable: a version is digits and
// dots, a model number is what sysDescr already says.
//
// This does NOT try to validate a real serial - serial formats are arbitrary and
// rejecting an odd one would lose a real device. It only refuses the specific
// mistakes this lookup can make.
func plausibleSerial(sn, sysDescr string) bool {
	if sn == "" || len(sn) > 64 {
		return false
	}
	// A version string: dotted digits, with an optional leading v. The dot is what
	// makes it one - plenty of real serials are all digits and nothing else, so
	// "digits only" would have thrown those away.
	if v := strings.TrimPrefix(strings.ToLower(sn), "v"); strings.Contains(v, ".") &&
		strings.IndexFunc(v, func(r rune) bool {
			return r != '.' && (r < '0' || r > '9')
		}) < 0 {
		return false
	}
	// A model number, which sysDescr already carries. Only for strings long enough
	// for the match to mean something: a 2-character substring hit says nothing.
	if len(sn) >= 4 && sysDescr != "" &&
		strings.Contains(strings.ToLower(sysDescr), strings.ToLower(sn)) {
		return false
	}
	return true
}

// vendorSerial asks the vendor's tree for a serial the standard OID did not give.
//
// One extra GET, and only for a device that already answered - so it costs a
// round trip on a host known to be up, not a timeout. Batched, because three
// varbinds in one PDU is one round trip; retried one at a time if the batch
// errors, because a v1-era agent rejects the WHOLE PDU when any OID in it is
// unknown, and losing all three to one absent leaf is how this would silently
// find nothing.
func vendorSerial(conn *gosnmp.GoSNMP, sysObjectID, sysDescr string) string {
	oids := serialOIDsFor(sysObjectID)
	if len(oids) == 0 {
		return ""
	}
	if sn := firstSerial(conn, oids, sysDescr); sn != "" {
		return sn
	}
	if len(oids) == 1 {
		return ""
	}
	for _, oid := range oids {
		if sn := firstSerial(conn, []string{oid}, sysDescr); sn != "" {
			return sn
		}
	}
	return ""
}

func firstSerial(conn *gosnmp.GoSNMP, oids []string, sysDescr string) string {
	res, err := conn.Get(oids)
	if err != nil || res == nil {
		return ""
	}
	for _, v := range res.Variables {
		if v.Type != gosnmp.OctetString {
			continue
		}
		b, ok := v.Value.([]byte)
		if !ok {
			continue
		}
		sn := strings.TrimSpace(string(b))
		if plausibleSerial(sn, sysDescr) {
			return sn
		}
	}
	return ""
}

// Scope is the run scope the API hands over.
type Scope struct {
	Subnets []string `json:"subnets"`
}

// ParseScope reads the scope JSON a run carries.
func ParseScope(raw json.RawMessage) (Scope, error) {
	var s Scope
	if len(raw) == 0 {
		return s, nil
	}
	err := json.Unmarshal(raw, &s)
	return s, err
}
