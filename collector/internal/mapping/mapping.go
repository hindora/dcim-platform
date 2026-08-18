// Package mapping loads the protocol-to-metric tables from contracts/mappings.
//
// Mappings are DATA. Adding an OID must not require a collector release, and
// every metric key is validated against the generated registry at load time so
// a typo fails at boot rather than silently dropping a metric nobody misses
// until a chart is empty a week later.
package mapping

import (
	"fmt"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

type Transform struct {
	Scale    *float64          `yaml:"scale"`
	Offset   *float64          `yaml:"offset"`
	EnumTrue []int64           `yaml:"enum_true"`
	ValueMap map[string]string `yaml:"map"`
}

type Column struct {
	OID           string     `yaml:"oid"`
	Metric        string     `yaml:"metric"`
	ValueType     string     `yaml:"value_type"`
	CounterBits   int        `yaml:"counter_bits"`
	Transform     *Transform `yaml:"transform"`
	ScaleByColumn string     `yaml:"scale_by_column"`
	PrecisionFrom string     `yaml:"precision_from"`
}

type Derived struct {
	Metric      string     `yaml:"metric"`
	ValueType   string     `yaml:"value_type"`
	Numerator   string     `yaml:"numerator"`
	Denominator string     `yaml:"denominator"`
	Transform   *Transform `yaml:"transform"`
}

type RowFilter struct {
	OID       string `yaml:"oid"`
	Equals    string `yaml:"equals"`
	EqualsInt *int64 `yaml:"equals_int"`
}

type Table struct {
	Name         string     `yaml:"name"`
	IndexOID     string     `yaml:"index_oid"`
	Columns      []Column   `yaml:"columns"`
	Derived      []Derived  `yaml:"derived"`
	RowFilter    *RowFilter `yaml:"row_filter"`
	InstanceFrom string     `yaml:"instance_from"`
	// avg|max|min|sum - collapse every row into one device-scoped sample.
	Aggregate string `yaml:"aggregate"`
}

// DerivedScalar computes a metric from two scalar OIDs. It exists because
// several MIBs report a total and a remainder rather than the quantity an
// operator actually wants: UCD gives memTotalReal and memAvailReal, never
// "memory used".
type DerivedScalar struct {
	Metric      string `yaml:"metric"`
	ValueType   string `yaml:"value_type"`
	Numerator   string `yaml:"numerator"`
	Denominator string `yaml:"denominator"`
	// OneMinus turns a remaining-fraction into a consumed-fraction.
	OneMinus bool `yaml:"one_minus"`
	// MultiplyBy scales the fraction back into absolute units.
	MultiplyBy string     `yaml:"multiply_by"`
	Transform  *Transform `yaml:"transform"`
}

type Scalar struct {
	OID         string     `yaml:"oid"`
	Metric      string     `yaml:"metric"`
	ValueType   string     `yaml:"value_type"`
	CounterBits int        `yaml:"counter_bits"`
	Transform   *Transform `yaml:"transform"`
}

type Profile struct {
	Name           string          `yaml:"name"`
	Scalars        []Scalar        `yaml:"scalars"`
	DerivedScalars []DerivedScalar `yaml:"derived_scalars"`
	Tables         []Table         `yaml:"tables"`
}

type file struct {
	Version  int       `yaml:"version"`
	Profiles []Profile `yaml:"profiles"`
}

// Registry holds every loaded profile, keyed by name.
type Registry struct {
	profiles map[string]*Profile
}

func Load(dir string) (*Registry, error) {
	r := &Registry{profiles: make(map[string]*Profile)}
	pattern := filepath.Join(dir, "snmp", "*.yaml")
	paths, err := filepath.Glob(pattern)
	if err != nil {
		return nil, err
	}
	if len(paths) == 0 {
		return nil, fmt.Errorf("no SNMP mapping files found under %s", pattern)
	}

	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read %s: %w", path, err)
		}
		var f file
		if err := yaml.Unmarshal(raw, &f); err != nil {
			return nil, fmt.Errorf("parse %s: %w", path, err)
		}
		for i := range f.Profiles {
			p := f.Profiles[i]
			if err := validate(&p); err != nil {
				return nil, fmt.Errorf("%s: profile %q: %w", path, p.Name, err)
			}
			r.profiles[p.Name] = &p
		}
	}
	return r, nil
}

// validate rejects any metric key the registry does not define. The collector
// refuses to emit unknown keys, so a mapping that names one is dead weight that
// looks like it works.
func validate(p *Profile) error {
	check := func(metric, valueType string) error {
		def, ok := models.ValidateMetric(metric)
		if !ok {
			return fmt.Errorf("unknown metric %q", metric)
		}
		if valueType != "" && valueType != def.ValueType {
			return fmt.Errorf("metric %q declared %s here but %s in the registry",
				metric, valueType, def.ValueType)
		}
		return nil
	}
	for _, s := range p.Scalars {
		if err := check(s.Metric, s.ValueType); err != nil {
			return err
		}
	}
	for _, d := range p.DerivedScalars {
		if err := check(d.Metric, d.ValueType); err != nil {
			return err
		}
	}
	for _, t := range p.Tables {
		for _, c := range t.Columns {
			if err := check(c.Metric, c.ValueType); err != nil {
				return err
			}
		}
		for _, d := range t.Derived {
			if err := check(d.Metric, d.ValueType); err != nil {
				return err
			}
		}
	}
	return nil
}

func (r *Registry) Profile(name string) (*Profile, bool) {
	p, ok := r.profiles[name]
	return p, ok
}

func (r *Registry) Names() []string {
	out := make([]string, 0, len(r.profiles))
	for name := range r.profiles {
		out = append(out, name)
	}
	return out
}

// Apply runs a transform over a numeric value.
func (t *Transform) Apply(v float64) float64 {
	if t == nil {
		return v
	}
	if t.Scale != nil {
		v *= *t.Scale
	}
	if t.Offset != nil {
		v += *t.Offset
	}
	return v
}

// Bool converts an integer enumeration to a boolean, e.g. ifOperStatus 1 = up.
func (t *Transform) Bool(v int64) bool {
	if t == nil || len(t.EnumTrue) == 0 {
		return v != 0
	}
	for _, want := range t.EnumTrue {
		if v == want {
			return true
		}
	}
	return false
}
