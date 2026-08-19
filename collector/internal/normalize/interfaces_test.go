package normalize

import "testing"

func TestInterfaceNameExpandsShortForms(t *testing.T) {
	cases := map[string]string{
		"Gi0/0":                 "GigabitEthernet0/0",
		"gi0/0":                 "GigabitEthernet0/0",
		"Te1/1/1":               "TenGigabitEthernet1/1/1",
		"Fa0/1":                 "FastEthernet0/1",
		"Hu1/1":                 "HundredGigabitEthernet1/1",
		"Fo0/49":                "FortyGigabitEthernet0/49",
		"Po10":                  "Port-channel10",
		"Vl100":                 "Vlan100",
		"Lo0":                   "Loopback0",
		"TenGigE0/0/0/1":        "TenGigabitEthernet0/0/0/1",
		"GigabitEthernet0/0":    "GigabitEthernet0/0",
		"TenGigabitEthernet1/1": "TenGigabitEthernet1/1",
		"Ethernet1":             "Ethernet1",
		"  Ethernet1  ":         "Ethernet1",
	}
	for in, want := range cases {
		if got := InterfaceName(in); got != want {
			t.Errorf("InterfaceName(%q) = %q, want %q", in, got, want)
		}
	}
}

// A name this function does not recognise is far more likely to be a vendor
// convention nobody here has seen than a mistake, and rewriting it would
// invent an identity.
func TestInterfaceNameLeavesUnknownNamesAlone(t *testing.T) {
	for _, in := range []string{
		"xe-0/0/0",       // Juniper
		"swp1",           // Cumulus
		"enp3s0f0",       // Linux
		"bond0",          // a bond, not a port
		"Gigabit uplink", // a description, not a name
		"Management",     // no port number
		"eth0",           // Linux, and NOT Ethernet0
		"",               // nothing at all
	} {
		if got := InterfaceName(in); got != trimmed(in) {
			t.Errorf("InterfaceName(%q) = %q, want it unchanged", in, got)
		}
	}
}

func trimmed(s string) string {
	out := s
	for len(out) > 0 && (out[0] == ' ' || out[0] == '\t') {
		out = out[1:]
	}
	for len(out) > 0 && (out[len(out)-1] == ' ' || out[len(out)-1] == '\t') {
		out = out[:len(out)-1]
	}
	return out
}

// "eth0" must NOT become "Ethernet0": on a Linux host eth0 is its own name,
// and a server's OS agent reports it alongside a switch reporting Ethernet0.
// Conflating them would merge two unrelated ports into one series.
func TestLinuxNamesAreNotExpanded(t *testing.T) {
	for _, in := range []string{"eth0", "eth1", "ens192", "eno1"} {
		if got := InterfaceName(in); got != in {
			t.Fatalf("InterfaceName(%q) = %q; linux names must not be expanded",
				in, got)
		}
	}
}

func TestInterfaceKeyIgnoresCaseAndPunctuation(t *testing.T) {
	same := [][2]string{
		{"GigabitEthernet0/0", "Gi0/0"},
		{"GigabitEthernet0/0", "gigabitethernet0/0"},
		{"Ethernet1_1", "Ethernet11"}, // an underscore carries no structure
		{"Port-channel10", "Po10"},
	}
	for _, pair := range same {
		if InterfaceKey(pair[0]) != InterfaceKey(pair[1]) {
			t.Errorf("%q and %q should share a key, got %q and %q",
				pair[0], pair[1], InterfaceKey(pair[0]), InterfaceKey(pair[1]))
		}
	}

	// Separators inside a port number ARE structure: these are real different
	// ports and must never collapse together.
	different := [][2]string{
		{"Ethernet1/1", "Ethernet1/2"},
		{"Ethernet1/1", "Ethernet11/1"},
		{"GigabitEthernet0/0", "GigabitEthernet0/0.100"},
	}
	for _, pair := range different {
		if InterfaceKey(pair[0]) == InterfaceKey(pair[1]) {
			t.Errorf("%q and %q must not share a key (%q)",
				pair[0], pair[1], InterfaceKey(pair[0]))
		}
	}
}

func TestInterfaceKeyIsStable(t *testing.T) {
	if InterfaceKey("") != "" {
		t.Error("an empty name should key to empty")
	}
	if got := InterfaceKey("  Gi0/0 "); got != "gigabitethernet0/0" {
		t.Errorf("got %q", got)
	}
}
