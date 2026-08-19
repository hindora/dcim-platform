package integration

import (
	"testing"
	"time"

	g "github.com/gosnmp/gosnmp"
)

// Which is cheaper against this plane: one walk of a whole table, or one walk
// per column the mapping actually wants?
//
// The adapter walks the whole table on the argument that per-column
// "multiplies the request count for identical data". That holds when most
// columns are wanted. The interface tables are the opposite case - 6 columns
// of ifTable's 22, 3 of ifXTable's 19 - so the whole-table walk pays for
// fifteen columns nobody reads, on a responder that serves the entire fleet
// from one process.
//
// This measures rather than argues.
func TestSNMPWalkStrategyCost(t *testing.T) {
	sim := RequireSimulator(t)
	dev := sim.DeviceOfType(t, "switch")
	ep := sim.SNMPEndpoint(t, dev, "os_agent")

	community, _ := ep.Credential.Data["community"].(string)
	dial := func() *g.GoSNMP {
		c := &g.GoSNMP{
			Target: ep.Address, Port: uint16(ep.Port), Community: community,
			Version: g.Version2c, Timeout: 20 * time.Second, Retries: 1,
			MaxRepetitions: 25,
		}
		if err := c.Connect(); err != nil {
			t.Fatalf("connect %s: %v", ep.Address, err)
		}
		return c
	}

	type run struct {
		name     string
		roots    []string
		varbinds int
		elapsed  time.Duration
	}

	// ifTable: the adapter wants 6 of these columns; the subtree holds 22.
	whole := run{name: "one walk of ifTable", roots: []string{"1.3.6.1.2.1.2.2.1"}}
	perCol := run{name: "one walk per wanted column", roots: []string{
		"1.3.6.1.2.1.2.2.1.7",  // ifAdminStatus
		"1.3.6.1.2.1.2.2.1.8",  // ifOperStatus
		"1.3.6.1.2.1.2.2.1.13", // ifInDiscards
		"1.3.6.1.2.1.2.2.1.14", // ifInErrors
		"1.3.6.1.2.1.2.2.1.19", // ifOutDiscards
		"1.3.6.1.2.1.2.2.1.20", // ifOutErrors
	}}

	for i := range []*run{&whole, &perCol} {
		r := []*run{&whole, &perCol}[i]
		c := dial()
		started := time.Now()
		for _, root := range r.roots {
			pdus, err := c.BulkWalkAll(root)
			if err != nil {
				c.Conn.Close()
				t.Fatalf("%s: walk %s: %v", r.name, root, err)
			}
			r.varbinds += len(pdus)
		}
		r.elapsed = time.Since(started)
		c.Conn.Close()
	}

	t.Logf("%-28s %5d varbinds in %s", whole.name, whole.varbinds,
		whole.elapsed.Round(time.Millisecond))
	t.Logf("%-28s %5d varbinds in %s", perCol.name, perCol.varbinds,
		perCol.elapsed.Round(time.Millisecond))

	if perCol.varbinds >= whole.varbinds {
		t.Errorf("per-column fetched %d varbinds against %d for the whole "+
			"table, so the mapping wants most of the table after all",
			perCol.varbinds, whole.varbinds)
	}
	ratio := float64(whole.elapsed) / float64(perCol.elapsed)
	t.Logf("per-column is %.2fx the speed and fetches %.0f%% of the varbinds",
		ratio, 100*float64(perCol.varbinds)/float64(whole.varbinds))
}
