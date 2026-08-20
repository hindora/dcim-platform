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
	// This plane routes by community rather than by destination address, so a
	// wrong community is silence rather than an error - which looks exactly
	// like an empty network.
	if got := PerAddressCommunity("10.51.11.99"); got != "10.51.11.99" {
		t.Errorf("community = %q, want the address", got)
	}
}

func TestFixedCommunityIgnoresTheAddress(t *testing.T) {
	f := FixedCommunity("public")
	if f("10.0.0.1") != "public" || f("10.0.0.2") != "public" {
		t.Error("FixedCommunity should return the same community for every address")
	}
}
