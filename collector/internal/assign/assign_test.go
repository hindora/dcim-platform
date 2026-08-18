package assign

import (
	"testing"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

func base() *models.Endpoint {
	return &models.Endpoint{
		ID: "ep-1", DeviceID: "dev-1", DeviceName: "SRV01",
		Protocol: "snmp", Role: "os_agent",
		Address: "10.50.11.19", Port: 161,
		Credential: &models.Credential{
			Kind: "snmp_v2c",
			Data: map[string]any{"community": "10.50.11.19"},
		},
		Poll: models.PollProfile{
			IntervalS: 30, TimeoutMs: 3000, Retries: 2,
			MetricGroups: []string{"system", "interfaces"},
		},
	}
}

func TestRenameDoesNotRestartTheJob(t *testing.T) {
	// Restarting a job resets counter baselines. Doing that because somebody
	// renamed a device would put a gap in every throughput chart.
	a, b := base(), base()
	b.DeviceName = "SRV01-renamed"
	b.Vendor = "Some Other Vendor"
	b.Model = "New Model"

	if changed(a, b) {
		t.Fatal("a cosmetic change was treated as a poll-affecting change")
	}
}

func TestAddressPortAndIntervalChangesRestart(t *testing.T) {
	cases := map[string]func(*models.Endpoint){
		"address":  func(e *models.Endpoint) { e.Address = "10.51.11.25" },
		"port":     func(e *models.Endpoint) { e.Port = 1161 },
		"protocol": func(e *models.Endpoint) { e.Protocol = "gnmi" },
		"interval": func(e *models.Endpoint) { e.Poll.IntervalS = 60 },
		"timeout":  func(e *models.Endpoint) { e.Poll.TimeoutMs = 9000 },
		"retries":  func(e *models.Endpoint) { e.Poll.Retries = 0 },
		"groups":   func(e *models.Endpoint) { e.Poll.MetricGroups = []string{"system"} },
		"group order": func(e *models.Endpoint) {
			e.Poll.MetricGroups = []string{"interfaces", "system"}
		},
		"community": func(e *models.Endpoint) {
			e.Credential.Data["community"] = "10.51.11.25"
		},
	}
	for name, mutate := range cases {
		a, b := base(), base()
		mutate(b)
		if !changed(a, b) {
			t.Fatalf("%s change was not detected", name)
		}
	}
}

func TestDiffClassifiesAddedRemovedAndChanged(t *testing.T) {
	c := &Client{current: map[string]*models.Endpoint{}}

	stays := base()
	goes := base()
	goes.ID = "ep-gone"
	c.current[stays.ID] = stays
	c.current[goes.ID] = goes

	moved := base()
	moved.Address = "10.51.11.25"
	arrives := base()
	arrives.ID = "ep-new"

	d := c.diff(map[string]*models.Endpoint{
		moved.ID:   moved,
		arrives.ID: arrives,
	})

	if len(d.Added) != 1 || d.Added[0].ID != "ep-new" {
		t.Fatalf("Added = %v, want just ep-new", ids(d.Added))
	}
	if len(d.Removed) != 1 || d.Removed[0].ID != "ep-gone" {
		t.Fatalf("Removed = %v, want just ep-gone", ids(d.Removed))
	}
	if len(d.Changed) != 1 || d.Changed[0].ID != "ep-1" {
		t.Fatalf("Changed = %v, want just ep-1", ids(d.Changed))
	}
	if d.Empty() {
		t.Fatal("diff reported empty despite changes")
	}
}

func TestIdenticalAssignmentIsAnEmptyDiff(t *testing.T) {
	ep := base()
	c := &Client{current: map[string]*models.Endpoint{ep.ID: ep}}

	same := base()
	if d := c.diff(map[string]*models.Endpoint{same.ID: same}); !d.Empty() {
		t.Fatalf("unchanged assignment produced a diff: %+v", d)
	}
}

func TestCommunityIsSafeOnANilCredential(t *testing.T) {
	// Endpoints for protocols that need no credential carry none.
	var c *models.Credential
	if got := c.Community(); got != "" {
		t.Fatalf("nil credential returned %q", got)
	}
	empty := &models.Credential{}
	if got := empty.Community(); got != "" {
		t.Fatalf("credential with no data returned %q", got)
	}
}

func ids(eps []*models.Endpoint) []string {
	out := make([]string, 0, len(eps))
	for _, e := range eps {
		out = append(out, e.ID)
	}
	return out
}
