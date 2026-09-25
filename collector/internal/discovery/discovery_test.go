package discovery

import (
	"strings"
	"testing"
)

// Hosts decides what a sweep actually probes, so an off-by-one here is the
// difference between auditing a network and auditing a different network.

func TestHostsExcludesNetworkAndBroadcast(t *testing.T) {
	got, err := Hosts([]string{"10.51.11.96/29"})
	if err != nil {
		t.Fatalf("Hosts: %v", err)
	}
	want := []string{"10.51.11.97", "10.51.11.98", "10.51.11.99", "10.51.11.100",
		"10.51.11.101", "10.51.11.102"}
	if len(got) != len(want) {
		t.Fatalf("got %d addresses %v, want %d", len(got), got, len(want))
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("address %d = %s, want %s", i, got[i], want[i])
		}
	}
}

func TestHostsCoversAFullSlash24(t *testing.T) {
	got, err := Hosts([]string{"10.51.11.0/24"})
	if err != nil {
		t.Fatalf("Hosts: %v", err)
	}
	if len(got) != 254 {
		t.Fatalf("got %d addresses, want 254", len(got))
	}
	if got[0] != "10.51.11.1" || got[253] != "10.51.11.254" {
		t.Errorf("range runs %s..%s, want 10.51.11.1..10.51.11.254", got[0], got[253])
	}
}

func TestHostsDeduplicatesOverlappingScopes(t *testing.T) {
	// Two scopes that overlap must not probe the same address twice: it doubles
	// the load on a responder the sweep is already trying not to disturb.
	got, err := Hosts([]string{"10.51.11.0/29", "10.51.11.0/28"})
	if err != nil {
		t.Fatalf("Hosts: %v", err)
	}
	seen := map[string]bool{}
	for _, a := range got {
		if seen[a] {
			t.Fatalf("address %s probed twice", a)
		}
		seen[a] = true
	}
}

func TestHostsRefusesAScopeTooWideToAudit(t *testing.T) {
	// Refused, not truncated. Sweeping the first 4096 of 65,536 and reporting
	// "found 12" would be a lie about what was audited.
	if _, err := Hosts([]string{"10.51.0.0/16"}); err == nil {
		t.Fatal("a /16 was accepted; it should be refused as too wide")
	}
}

func TestHostsRejectsMalformedAndIPv6(t *testing.T) {
	for _, bad := range []string{"10.51.11.0", "nonsense", "::1/64"} {
		if _, err := Hosts([]string{bad}); err == nil {
			t.Errorf("Hosts(%q) was accepted", bad)
		}
	}
}

func TestPerAddressCommunityIsTheAddress(t *testing.T) {
	// A snmpsim-backed plane routes by community rather than by destination
	// address, so a wrong community is silence rather than an error - which looks
	// exactly like an empty network. Correct there, and selected by configuration
	// rather than by default, because it is correct nowhere else.
	got := PerAddressCommunity("10.51.11.99")
	if len(got) != 1 || got[0] != "10.51.11.99" {
		t.Errorf("community = %q, want just the address", got)
	}
}

func TestCommunitiesAreTriedInOrder(t *testing.T) {
	// A site cannot know which community a device it has never spoken to will
	// accept, and on v2c a wrong one is silence - so the only way to find out is
	// to try the candidates in turn.
	f := Communities("secret", "public")
	for _, addr := range []string{"10.0.0.1", "10.0.0.2"} {
		got := f(addr)
		if len(got) != 2 || got[0] != "secret" || got[1] != "public" {
			t.Errorf("communities for %s = %q, want the list in order", addr, got)
		}
	}
}

func TestCommunitiesFallsBackRatherThanProbingWithNothing(t *testing.T) {
	// A sweep configured with no community would otherwise probe every address
	// with an empty one and report a silent network - which reads as "nothing is
	// there" rather than "nobody told me how to ask".
	got := Communities()("10.0.0.1")
	if len(got) != 1 || got[0] != "public" {
		t.Errorf("empty list gave %q, want [public]", got)
	}
}

func TestCommunitiesCannotBeMutatedByItsCaller(t *testing.T) {
	// The closure hands the same slice to every probe in a sweep. Returning the
	// caller's backing array would let one mutation change what every later
	// address is asked with.
	list := []string{"first"}
	f := Communities(list...)
	list[0] = "changed"
	if got := f("10.0.0.1"); got[0] != "first" {
		t.Errorf("community = %q, want the value captured at construction", got)
	}
}

func TestSerialIsInThePlaceTheAPIReadsIt(t *testing.T) {
	// The serial travels inside `identity`, which is already a free-form blob on
	// the results contract - so adding it needed no change to what the API
	// accepts. That only holds if the key is exactly what the API looks for.
	if names := map[string]string{
		oidSysDescr: "sysDescr", oidSysObjectID: "sysObjectID",
		oidSysName: "sysName", oidEntSerial: "serial",
	}; names[oidEntSerial] != "serial" {
		t.Fatalf("serial key = %q, want serial", names[oidEntSerial])
	}
}

func TestTheSerialOIDIsTheChassisEntity(t *testing.T) {
	// entPhysicalSerialNum (ENTITY-MIB) for entity 1. The whole reason it is worth
	// one extra OID in the probe: it is the only key that survives a device being
	// re-addressed, and matching on address alone reported a moved machine as
	// brand new.
	const want = "1.3.6.1.2.1.47.1.1.1.1.11.1"
	if oidEntSerial != want {
		t.Errorf("serial OID = %s, want entPhysicalSerialNum.1 (%s)", oidEntSerial, want)
	}
}

func TestVendorSerialIsChosenByEnterprisePrefix(t *testing.T) {
	// A real discovery tool reads sysObjectID and then asks that vendor, because
	// no standard MIB carries a serial for facility gear: UPS-MIB has no serial
	// object and Modbus/BACnet define no serial register. Prefix matching is what
	// makes one table cover every model a vendor ships.
	cases := []struct {
		name        string
		sysObjectID string
		wantFirst   string
	}{
		{"APC rack PDU", "1.3.6.1.4.1.318.1.3.5.4", oidAPCrPDU2Serial},
		{"APC, leading dot", ".1.3.6.1.4.1.318.1.3.5.1", oidAPCrPDU2Serial},
		{"Raritan PX2", "1.3.6.1.4.1.13742.6.3.2.14", oidRaritanSerial},
		{"Vertiv Liebert", "1.3.6.1.4.1.476.1.42.2.10.2.1.1", oidLiebertSerial},
	}
	for _, c := range cases {
		got := serialOIDsFor(c.sysObjectID)
		if len(got) == 0 || got[0] != c.wantFirst {
			t.Errorf("%s: first serial OID = %v, want %s", c.name, got, c.wantFirst)
		}
	}
	// A vendor with no confirmed OID asks for nothing rather than guessing. An
	// unknown enterprise must NOT fall through to another vendor's tree.
	for _, id := range []string{"1.3.6.1.4.1.9.1.1861", "1.3.6.1.4.1.534.2.14", ""} {
		if got := serialOIDsFor(id); len(got) != 0 {
			t.Errorf("sysObjectID %q resolved to %v, want no vendor probe", id, got)
		}
	}
	// 3181 is not under 318. Prefix matching on the raw string would have said it
	// was, and every Cisco-adjacent enterprise would get asked for an APC serial.
	if got := serialOIDsFor("1.3.6.1.4.1.3181.1"); len(got) != 0 {
		t.Errorf("enterprise 3181 matched APC's 318: %v", got)
	}
}

func TestPlausibleSerialRefusesTheNeighbouringLeaf(t *testing.T) {
	// The vendor OIDs are tried in order and the first non-empty string wins, so an
	// off-by-one column hands back whatever sits beside the serial in the identity
	// table. That is a firmware version or a model number, and filed as a serial it
	// would match nothing for ever while looking like a key.
	const descr = "APC Rack PDU AP8886, firmware v6.8.2"
	for _, sn := range []string{"6.8.2", "v6.8.2", "1.6.0", "AP8886", "",
		strings.Repeat("x", 65)} {
		if plausibleSerial(sn, descr) {
			t.Errorf("accepted %q as a serial", sn)
		}
	}
	// And it must not reject real ones. An all-digit serial is ordinary - rejecting
	// "digits only" as a version would have thrown those away.
	for _, sn := range []string{"YUGLKTRR", "5A1703T99999", "1234567890",
		"SN-ABC-123", "7e3f2a1"} {
		if !plausibleSerial(sn, descr) {
			t.Errorf("rejected %q, which is a serial", sn)
		}
	}
}
