// Package normalize turns the several names a device has for one port into
// one name.
//
// A physical port has a different identity depending on who is asked. SNMP
// offers ifIndex, ifName, ifDescr and ifAlias, and agents disagree about which
// carries the short form. openconfig offers `name`. Vendors abbreviate in the
// CLI and expand in the MIB, or the reverse. The same port is therefore
// "GigabitEthernet0/0", "Gi0/0", "gi0/0" and "2" depending on the question.
//
// If the collector emits those verbatim, one port becomes several series. That
// is not a cosmetic problem: no chart shows the port's real traffic, the two
// ends of a link cannot be correlated, and an alarm on Gi0/0 and one on
// GigabitEthernet0/0 are two separate alarms about one cable.
//
// So every adapter passes interface names through here before they become a
// metric instance. This is one half of the answer; the other is in the ingest
// worker, which maps whatever arrives onto the name INVENTORY holds, because
// inventory is the only authority on what the port is called.
package normalize

import (
	"strings"
	"unicode"
)

// abbreviations are the short forms real gear emits, mapped to the long form
// the same vendors use in openconfig and in most ifDescr values.
//
// Order matters: the longest prefix must be tried first, or "Te" swallows
// "TenGigE" and "Gi" swallows "GigabitEthernet".
var abbreviations = []struct{ short, long string }{
	{"HundredGigE", "HundredGigabitEthernet"},
	{"TwentyFiveGigE", "TwentyFiveGigE"},
	{"FortyGigE", "FortyGigabitEthernet"},
	{"TenGigE", "TenGigabitEthernet"},
	{"TenGig", "TenGigabitEthernet"},
	{"GigabitEthernet", "GigabitEthernet"},
	{"FastEthernet", "FastEthernet"},
	{"Port-channel", "Port-channel"},
	{"Ethernet", "Ethernet"},
	{"Management", "Management"},
	{"Loopback", "Loopback"},
	{"Vlan", "Vlan"},
	{"Hu", "HundredGigabitEthernet"},
	{"Fo", "FortyGigabitEthernet"},
	{"Twe", "TwentyFiveGigE"},
	{"Te", "TenGigabitEthernet"},
	{"Gi", "GigabitEthernet"},
	{"Fa", "FastEthernet"},
	{"Po", "Port-channel"},
	{"Ma", "Management"},
	{"Mgmt", "Management"},
	{"Lo", "Loopback"},
	{"Vl", "Vlan"},
}

// "Et" and "Eth" are deliberately NOT expanded, though Cisco and Arista both
// accept them as abbreviations for Ethernet.
//
// A Linux host calls its first NIC eth0, and a server's OS agent reports that
// alongside a switch reporting Ethernet0. Expanding one into the other merges
// two unrelated ports into a single series, which is worse than the problem
// this package exists to solve - and unlike a missed expansion, it is
// invisible: the series looks fine and its numbers are the sum of two
// different cables.
//
// Nothing in this device plane reports the short form. If gear that does turns
// up, the safe fix is a per-device-type rule, not a global one.

// InterfaceName expands a short form and trims surrounding space.
//
// Expansion happens only when the prefix is followed by a digit, so a port
// called "Gi0/0" expands and a description called "Gigabit uplink" does not.
// Everything else is returned unchanged: a name this function does not
// recognise is far more likely to be a vendor convention nobody here has seen
// than a mistake, and rewriting it would invent an identity.
func InterfaceName(s string) string {
	name := strings.TrimSpace(s)
	if name == "" {
		return ""
	}

	prefix, rest := splitPrefix(name)
	if prefix == "" || rest == "" {
		return name
	}
	// The remainder has to start with a digit for this to be a port number.
	if !unicode.IsDigit(rune(rest[0])) {
		return name
	}

	for _, a := range abbreviations {
		if strings.EqualFold(prefix, a.short) {
			return a.long + rest
		}
	}
	return name
}

// splitPrefix divides a name into its leading letters and the remainder.
func splitPrefix(s string) (string, string) {
	for i, r := range s {
		if !unicode.IsLetter(r) && r != '-' {
			return s[:i], s[i:]
		}
	}
	return s, ""
}

// InterfaceKey is the form used to COMPARE two names for the same port.
//
// Case and separators vary between what an agent reports and what inventory
// holds - "Ethernet1/1", "ethernet1/1" and "Ethernet1_1" all occur - and none
// of that changes which port is meant. The key is for lookups only; the name
// stored and displayed is always inventory's.
func InterfaceKey(s string) string {
	expanded := InterfaceName(s)
	var b strings.Builder
	b.Grow(len(expanded))
	for _, r := range expanded {
		switch {
		case unicode.IsLetter(r) || unicode.IsDigit(r):
			b.WriteRune(unicode.ToLower(r))
		case r == '/' || r == '.' || r == ':':
			// Structural separators inside a port number are meaningful:
			// Ethernet1/1 and Ethernet11 are different ports.
			b.WriteRune(r)
		}
	}
	return b.String()
}
