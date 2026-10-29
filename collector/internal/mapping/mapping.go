// Package mapping loads the protocol-to-metric tables from contracts/mappings.
//
// Mappings are DATA. Adding an OID must not require a collector release, and
// every metric key is validated against the generated registry at load time so
// a typo fails at boot rather than silently dropping a metric nobody misses
// until a chart is empty a week later.
package mapping

import (
	"fmt"
	"io/fs"
	stdpath "path"
	"regexp"
	"strconv"

	"gopkg.in/yaml.v3"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

type Transform struct {
	Scale    *float64          `yaml:"scale"`
	Offset   *float64          `yaml:"offset"`
	EnumTrue []int64           `yaml:"enum_true"`
	ValueMap map[string]string `yaml:"map"`
	// NumberInText reads a gauge out of a DisplayString that carries its unit:
	// PDU2-MIB's overCurrentProtectorRatedCurrent is "16A", not 16. Opt-in per
	// column, so a text OID elsewhere never turns into a number by accident.
	NumberInText bool `yaml:"number_in_text"`
}

var numberInText = regexp.MustCompile(`^\s*([-+]?\d+(?:\.\d+)?)\s*[A-Za-z%]*\s*$`)

// TextNumber parses "16A", "16 A" or "16" to 16. False for anything else -
// "C1", "16-20A", an empty string - rather than guessing.
func (t *Transform) TextNumber(s string) (float64, bool) {
	if t == nil || !t.NumberInText {
		return 0, false
	}
	m := numberInText.FindStringSubmatch(s)
	if m == nil {
		return 0, false
	}
	f, err := strconv.ParseFloat(m[1], 64)
	return f, err == nil
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

// IndexRule reads a table whose row index is a PATH rather than a number.
//
// PDU2-MIB measures sensors at [pduId].[outletId].[sensorType]: one walk of
// measurementsOutletSensorValue returns rows indexed 1.7.1 (outlet 7, rms
// current), 1.7.5 (outlet 7, active power), 1.7.14 (outlet 7, on/off) and so
// on for every receptacle. The sensor TYPE is in the index, not in a column,
// so a row_filter cannot see it, and the instance the platform wants is one
// part of the index, not all of it.
//
// `where` keeps only rows whose index part (1-based) equals the value;
// `instance_part` names the part that becomes the sample's instance. Both
// optional: without them a table behaves as before (whole index = instance).
type IndexRule struct {
	InstancePart int         `yaml:"instance_part"`
	Where        *IndexWhere `yaml:"where"`
}

type IndexWhere struct {
	Part   int    `yaml:"part"`
	Equals string `yaml:"equals"`
}

type Table struct {
	Name         string     `yaml:"name"`
	IndexOID     string     `yaml:"index_oid"`
	Columns      []Column   `yaml:"columns"`
	Derived      []Derived  `yaml:"derived"`
	RowFilter    *RowFilter `yaml:"row_filter"`
	InstanceFrom string     `yaml:"instance_from"`
	Index        *IndexRule `yaml:"index"`
	// Kind marks a table whose instance is a kind of identity the platform
	// knows how to normalise. "interface" is the only one so far: a port has
	// several names depending on who is asked, and every plane has to settle
	// on one before the data lands.
	Kind string `yaml:"kind"`
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
	// SumDenominator makes the divisor numerator+denominator. Memory POOLS
	// publish used and free and no total - CISCO-MEMORY-POOL-MIB is the one in
	// front of us - so the whole is the sum of the parts. Without it a poller
	// divides by the platform's nameplate RAM instead and reports a router
	// sitting at 3% for ever.
	SumDenominator bool `yaml:"sum_denominator"`
	// MultiplyBy scales the fraction back into absolute units.
	MultiplyBy string     `yaml:"multiply_by"`
	Transform  *Transform `yaml:"transform"`
}

type Scalar struct {
	OID       string `yaml:"oid"`
	Metric    string `yaml:"metric"`
	ValueType string `yaml:"value_type"`
	// Instance names the part this reading belongs to, for a scalar OID that
	// IS a named sensor rather than a device-wide figure.
	//
	// Empty for most of them - sysUpTime and memTotalReal are properties of
	// the box. It matters where another plane publishes the SAME metric with a
	// name attached: a switch's die temperature arrives from gNMI as "CPU" and
	// from ENTITY-SENSOR-MIB at a fixed index, and if the SNMP side leaves the
	// instance empty the two become different series - and an alarm raised on
	// one can never be cleared by the other. That happened: a switch carried a
	// cpu_temp_high raised from the unlabelled SNMP reading while the labelled
	// gNMI samples sat below the clear point, with nothing able to join them.
	Instance    string     `yaml:"instance"`
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

func Load(fsys fs.FS) (*Registry, error) {
	r := &Registry{profiles: make(map[string]*Profile)}
	pattern := stdpath.Join("snmp", "*.yaml")
	paths, err := fs.Glob(fsys, pattern)
	if err != nil {
		return nil, err
	}
	if len(paths) == 0 {
		return nil, fmt.Errorf("no SNMP mapping files found under %s", pattern)
	}

	for _, filePath := range paths {
		raw, err := fs.ReadFile(fsys, filePath)
		if err != nil {
			return nil, fmt.Errorf("read %s: %w", filePath, err)
		}
		var f file
		if err := yaml.Unmarshal(raw, &f); err != nil {
			return nil, fmt.Errorf("parse %s: %w", filePath, err)
		}
		for i := range f.Profiles {
			prof := f.Profiles[i]
			if err := validate(&prof); err != nil {
				return nil, fmt.Errorf("%s: profile %q: %w", filePath, prof.Name, err)
			}
			r.profiles[prof.Name] = &prof
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
		if t.Index != nil {
			// A part number is 1-based; zero means "the whole index", and
			// a `where` with no part or no value matches nothing, which is
			// a mapping that silently records no samples.
			if t.Index.InstancePart < 0 {
				return fmt.Errorf("table %q: index.instance_part must be >= 1", t.Name)
			}
			if w := t.Index.Where; w != nil && (w.Part < 1 || w.Equals == "") {
				return fmt.Errorf("table %q: index.where needs part >= 1 and equals", t.Name)
			}
		}
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
