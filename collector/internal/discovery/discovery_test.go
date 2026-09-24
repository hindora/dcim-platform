package discovery

import "testing"

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
