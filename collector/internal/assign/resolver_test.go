package assign

import (
	"testing"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

func other(id, device, addr, site, community string) ResolveEntry {
	e := ResolveEntry{ID: id, DeviceID: device, DeviceName: device,
		DeviceType: "pdu", Protocol: "snmp", Role: "mgmt",
		Address: addr, Site: site}
	if community != "" {
		e.CommunitySHA256 = CommunityDigest(community)
	}
	return e
}

func TestATrapFromAnotherCollectorsDeviceIsAttributed(t *testing.T) {
	// The Phase 0 bug: with two collectors, a trap that landed on the one
	// that does not own the sender published with no device and raised
	// nothing.
	r := NewResolver()
	r.Replace([]*models.Endpoint{base()},
		[]ResolveEntry{other("ep-9", "pdu-9", "10.51.21.40", "DC2", "")}, "DC1")

	ep, ok := r.Resolve("10.51.21.40", "")
	if !ok || ep.DeviceID != "pdu-9" {
		t.Fatalf("borrowed endpoint not resolved: %+v %v", ep, ok)
	}
}

func TestACommunityOnlyTrapResolvesThroughTheDigest(t *testing.T) {
	// A trap whose UDP source is a shared socket, not the agent - the
	// simulator's, and any device behind NAT - is attributable only by its
	// community, which the platform sends as a digest.
	r := NewResolver()
	r.Replace(nil,
		[]ResolveEntry{other("ep-9", "pdu-9", "10.51.21.40", "DC2", "10.51.21.40")}, "DC1")

	ep, ok := r.Resolve("127.0.0.1", "10.51.21.40")
	if !ok || ep.ID != "ep-9" {
		t.Fatalf("community digest did not resolve: %+v %v", ep, ok)
	}
}

func TestOwnedBeatsBorrowedAndOwnSiteBeatsOthers(t *testing.T) {
	r := NewResolver()
	owned := base() // 10.50.11.19, dev-1
	r.Replace([]*models.Endpoint{owned}, []ResolveEntry{
		other("ep-x", "dev-x", "10.50.11.19", "DC1", ""), // same address, borrowed
		other("ep-a", "dev-a", "10.60.0.5", "DC2", ""),   // another site
		other("ep-b", "dev-b", "10.60.0.5", "DC1", ""),   // own site, same address
	}, "DC1")

	if ep, _ := r.Resolve("10.50.11.19", ""); ep.DeviceID != "dev-1" {
		t.Errorf("owned endpoint lost to a borrowed one: %s", ep.DeviceID)
	}
	if ep, _ := r.Resolve("10.60.0.5", ""); ep.DeviceID != "dev-b" {
		t.Errorf("another site's device beat this site's: %s", ep.DeviceID)
	}
}

func TestAnAddressTwoOtherSitesShareIsRefusedNotGuessed(t *testing.T) {
	// Overlapping RFC1918 between sites is ordinary. Filing a trap against
	// the wrong device is worse than filing it against none.
	r := NewResolver()
	r.Replace(nil, []ResolveEntry{
		other("ep-a", "dev-a", "10.51.11.25", "DC2", ""),
		other("ep-b", "dev-b", "10.51.11.25", "DC3", ""),
		other("ep-c", "dev-c", "10.51.11.25", "DC4", ""),
	}, "DC1")

	if ep, ok := r.Resolve("10.51.11.25", ""); ok {
		t.Fatalf("ambiguous address resolved to %s", ep.DeviceID)
	}
	if r.Ambiguous() == 0 {
		t.Error("the refusal was not counted")
	}
}

func TestTwoEndpointsOfOneDeviceAreNotAmbiguous(t *testing.T) {
	r := NewResolver()
	a := other("ep-snmp", "srv-1", "10.51.11.25", "DC2", "")
	b := other("ep-rf", "srv-1", "10.51.11.25", "DC2", "")
	b.Protocol = "redfish"
	r.Replace(nil, []ResolveEntry{a, b}, "DC1")

	if ep, ok := r.Resolve("10.51.11.25", ""); !ok || ep.DeviceID != "srv-1" {
		t.Fatalf("one device on one address was treated as a conflict")
	}
}

func TestRedfishEventsResolveBorrowedEndpointsByID(t *testing.T) {
	// A subscription made by a previous owner still delivers here after a
	// move, carrying the endpoint id in its Context.
	r := NewResolver()
	r.Replace(nil, []ResolveEntry{other("ep-bmc", "srv-2", "10.51.11.26", "DC1", "")}, "DC1")
	if _, ok := r.ResolveID("ep-bmc"); !ok {
		t.Fatal("borrowed endpoint not resolvable by id")
	}
}

func TestAPasswordRotationRestartsTheJob(t *testing.T) {
	// Comparing only the SNMP community let a rotated Redfish password
	// compare equal, so the job kept presenting the old one.
	a, b := base(), base()
	a.Credential = &models.Credential{Kind: "redfish_basic",
		Data: map[string]any{"username": "root", "password": "old"}}
	b.Credential = &models.Credential{Kind: "redfish_basic",
		Data: map[string]any{"username": "root", "password": "new"}}
	if !changed(a, b) {
		t.Fatal("a credential change did not restart the job")
	}
	c := base()
	c.Credential = &models.Credential{Kind: "redfish_basic",
		Data: map[string]any{"password": "old", "username": "root"}}
	if changed(a, c) {
		t.Fatal("the same credential compared different")
	}
}
