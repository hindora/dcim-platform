package mapping

import (
	"os"
	"path/filepath"
	"testing"
)

func writeMapping(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	snmpDir := filepath.Join(dir, "snmp")
	if err := os.MkdirAll(snmpDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(snmpDir, "test.yaml"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestLoadRejectsAnUnknownMetric(t *testing.T) {
	// The collector refuses to emit unregistered keys, so a mapping naming one
	// is dead weight that looks like it works. Fail at boot instead.
	dir := writeMapping(t, `
version: 1
profiles:
  - name: broken
    scalars:
      - oid: 1.3.6.1.2.1.1.3.0
        metric: cpu_utilisation_typo
        value_type: gauge
`)
	_, err := Load(dir)
	if err == nil {
		t.Fatal("Load accepted a mapping with an unknown metric")
	}
}

func TestLoadRejectsAValueTypeThatContradictsTheRegistry(t *testing.T) {
	// if_in_octets is a counter. Declaring it a gauge here would skip rate
	// derivation and chart a ramp instead of throughput.
	dir := writeMapping(t, `
version: 1
profiles:
  - name: broken
    tables:
      - name: t
        columns:
          - oid: 1.3.6.1.2.1.31.1.1.1.6
            metric: if_in_octets
            value_type: gauge
`)
	_, err := Load(dir)
	if err == nil {
		t.Fatal("Load accepted a value_type contradicting the registry")
	}
}

func TestLoadAcceptsAValidProfile(t *testing.T) {
	dir := writeMapping(t, `
version: 1
profiles:
  - name: good
    scalars:
      - oid: 1.3.6.1.2.1.1.3.0
        metric: sys_uptime
        value_type: counter
        counter_bits: 32
        transform: {scale: 0.01}
    tables:
      - name: hr_storage_ram
        row_filter: {oid: 1.3.6.1.2.1.25.2.3.1.2, equals: 1.3.6.1.2.1.25.2.1.2}
        columns:
          - oid: 1.3.6.1.2.1.25.2.3.1.5
            metric: memory_total
            value_type: gauge
            scale_by_column: 1.3.6.1.2.1.25.2.3.1.4
        derived:
          - metric: memory_utilization
            value_type: gauge
            numerator: 1.3.6.1.2.1.25.2.3.1.6
            denominator: 1.3.6.1.2.1.25.2.3.1.5
            transform: {scale: 100}
        aggregate: max
`)
	reg, err := Load(dir)
	if err != nil {
		t.Fatalf("Load failed on a valid profile: %v", err)
	}

	p, ok := reg.Profile("good")
	if !ok {
		t.Fatal("profile not registered")
	}
	if len(p.Scalars) != 1 || p.Scalars[0].CounterBits != 32 {
		t.Fatalf("scalar not parsed: %+v", p.Scalars)
	}
	if got := p.Scalars[0].Transform.Apply(12345); got != 123.45 {
		t.Fatalf("TimeTicks scaling gave %v, want 123.45", got)
	}
	tbl := p.Tables[0]
	if tbl.RowFilter == nil || tbl.Aggregate != "max" {
		t.Fatalf("table options not parsed: %+v", tbl)
	}
	if tbl.Columns[0].ScaleByColumn == "" {
		t.Fatal("scale_by_column not parsed; hrStorage would report allocation units as bytes")
	}
	if len(tbl.Derived) != 1 {
		t.Fatal("derived metric not parsed")
	}
}

func TestLoadFailsWhenNoMappingsExist(t *testing.T) {
	// Starting with zero mappings would poll every device and emit nothing,
	// which reads as "all devices silent".
	if _, err := Load(t.TempDir()); err == nil {
		t.Fatal("Load succeeded with no mapping files")
	}
}

func TestShippedMappingsAreValid(t *testing.T) {
	// Guards the real contracts/mappings tree, not just a fixture.
	reg, err := Load(filepath.Join("..", "..", "..", "contracts", "mappings"))
	if err != nil {
		t.Fatalf("shipped mappings failed to load: %v", err)
	}
	for _, want := range []string{"system", "interfaces", "host_resources"} {
		if _, ok := reg.Profile(want); !ok {
			t.Fatalf("profile %q missing from the shipped mappings", want)
		}
	}
}

// A scalar that IS a named sensor must be able to say so.
//
// Two planes publish a switch's die temperature: gNMI calls it "CPU", and
// ENTITY-SENSOR-MIB carries it at a fixed index. While the SNMP side had no way
// to name it, the two were different series for one reading - and a switch sat
// with a cpu_temp_high raised from the unlabelled SNMP sample while the gNMI
// samples ran below the clear point, with nothing able to join them up.
func TestScalarCarriesItsInstance(t *testing.T) {
	reg, err := Load("../../../contracts/mappings")
	if err != nil {
		t.Fatalf("load mappings: %v", err)
	}
	prof, ok := reg.Profile("network_sensors")
	if !ok {
		t.Fatal("network_sensors profile is missing")
	}
	var found, named int
	{
		for _, s := range prof.Scalars {
			found++
			if s.Instance != "" {
				named++
			}
			if s.Metric == "cpu_temperature" && s.Instance != "CPU" {
				t.Errorf("die temperature published as instance %q; gNMI calls "+
					"it CPU, and two names for one sensor is two series",
					s.Instance)
			}
		}
	}
	if found == 0 {
		t.Fatal("network_sensors declares no scalars")
	}
	if named != found {
		t.Errorf("%d of %d chassis sensors publish no instance", found-named, found)
	}
}
