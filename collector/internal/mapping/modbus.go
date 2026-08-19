package mapping

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

// ModbusPoint is one addressable quantity in one address space.
type ModbusPoint struct {
	Space string `yaml:"space"` // input | holding | discrete | coil
	Addr  uint16 `yaml:"addr"`
	Name  string `yaml:"name"`
	Dtype string `yaml:"dtype"`
	// Scale divides the raw register to give the VENDOR's unit.
	Scale float64 `yaml:"scale"`
	// Factor multiplies the vendor's unit to give the REGISTRY's unit.
	Factor   float64 `yaml:"factor"`
	Metric   string  `yaml:"metric"`
	Instance string  `yaml:"instance"`
	// Role marks a point that is not ordinary telemetry:
	//   validity      says whether the other registers mean anything yet
	//   process_value a transmitter's single measurement, whose meaning comes
	//                 from the probe role rather than from the point
	Role string `yaml:"role"`
	// Enum renders a state register as text.
	Enum map[int]string `yaml:"enum"`
}

// Value converts a raw decoded register into the registry's unit.
func (p ModbusPoint) Value(raw float64) float64 {
	v := raw
	if p.Scale != 0 {
		v /= p.Scale
	}
	if p.Factor != 0 {
		v *= p.Factor
	}
	return v
}

// ProbeBinding says what a transmitter installed in a given role measures.
type ProbeBinding struct {
	Metric   string `yaml:"metric"`
	Instance string `yaml:"instance"`
}

// ModbusTemplate is one device model's register map.
type ModbusTemplate struct {
	MapID       string                  `yaml:"-"`
	Vendor      string                  `yaml:"vendor"`
	Product     string                  `yaml:"product"`
	WordOrder   string                  `yaml:"word_order"`
	DeviceTypes []string                `yaml:"device_types"`
	ProbeRoles  map[string]ProbeBinding `yaml:"probe_roles"`
	Points      []ModbusPoint           `yaml:"points"`
}

type ModbusMap struct {
	Version   int                        `yaml:"version"`
	Templates map[string]*ModbusTemplate `yaml:"templates"`

	// Plain gear is selected by device type alone. Field transmitters cannot
	// be: an RTD and a magnetic flow meter are both "sensor" in inventory, and
	// only where the instrument is INSTALLED says which one is on the wire.
	byDeviceType map[string]*ModbusTemplate
	byProbeRole  map[string]*ModbusTemplate // deviceType + "/" + role
}

// LoadModbus reads contracts/mappings/modbus/templates.yaml.
func LoadModbus(dir string) (*ModbusMap, error) {
	path := filepath.Join(dir, "modbus", "templates.yaml")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	var m ModbusMap
	if err := yaml.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	if len(m.Templates) == 0 {
		return nil, fmt.Errorf("%s: no templates", path)
	}

	m.byDeviceType = make(map[string]*ModbusTemplate)
	m.byProbeRole = make(map[string]*ModbusTemplate)
	for id, t := range m.Templates {
		t.MapID = id
		if t.WordOrder != "big" && t.WordOrder != "swap" {
			return nil, fmt.Errorf("%s: %s: word_order must be big or swap, got %q",
				path, id, t.WordOrder)
		}
		for _, p := range t.Points {
			switch p.Space {
			case "input", "holding", "discrete", "coil":
			default:
				return nil, fmt.Errorf("%s: %s/%s: unknown address space %q",
					path, id, p.Name, p.Space)
			}
			if p.Metric != "" {
				if _, ok := models.ValidateMetric(p.Metric); !ok {
					return nil, fmt.Errorf("%s: %s/%s: unknown metric %q",
						path, id, p.Name, p.Metric)
				}
			}
			if p.Role != "" && p.Role != "validity" && p.Role != "process_value" {
				return nil, fmt.Errorf("%s: %s/%s: unknown role %q",
					path, id, p.Name, p.Role)
			}
		}
		for role, b := range t.ProbeRoles {
			if _, ok := models.ValidateMetric(b.Metric); !ok {
				return nil, fmt.Errorf("%s: %s: probe role %s: unknown metric %q",
					path, id, role, b.Metric)
			}
		}
		for _, dt := range t.DeviceTypes {
			if len(t.ProbeRoles) > 0 {
				for role := range t.ProbeRoles {
					key := dt + "/" + strings.ToLower(role)
					// Two instruments claiming the same installed role would
					// be resolved by map order, which is unstable.
					if prev, clash := m.byProbeRole[key]; clash {
						return nil, fmt.Errorf(
							"%s: %s probe role %q claimed by both %s and %s",
							path, dt, role, prev.MapID, id)
					}
					m.byProbeRole[key] = t
				}
				continue
			}
			if prev, clash := m.byDeviceType[dt]; clash {
				return nil, fmt.Errorf("%s: device type %q claimed by both %s and %s",
					path, dt, prev.MapID, id)
			}
			m.byDeviceType[dt] = t
		}
	}
	return &m, nil
}

// TemplateFor selects the template a device is polled with.
//
// Selection happens before the first request, from inventory alone: Modbus has
// no discovery, so there is nothing to ask. The identity FC43 reports is used
// afterwards to CHECK the choice, which is the only order the protocol allows.
//
// The probe role is consulted first because it is more specific. A device type
// of "sensor" covers both an RTD transmitter and a magnetic flow meter, and
// they share neither registers nor scaling - what separates them is where the
// instrument is installed.
func (m *ModbusMap) TemplateFor(deviceType, probeRole string) (*ModbusTemplate, bool) {
	if probeRole != "" {
		if t, ok := m.byProbeRole[deviceType+"/"+strings.ToLower(probeRole)]; ok {
			return t, true
		}
	}
	if t, ok := m.byDeviceType[deviceType]; ok {
		return t, true
	}
	return nil, false
}

// ProbeRolesFor lists every installed role known for a device type, so an
// endpoint missing one can be told what it should have said.
func (m *ModbusMap) ProbeRolesFor(deviceType string) []string {
	var out []string
	prefix := deviceType + "/"
	for key := range m.byProbeRole {
		if strings.HasPrefix(key, prefix) {
			out = append(out, strings.TrimPrefix(key, prefix))
		}
	}
	return out
}

// TemplateByID looks a template up by its map identity.
func (m *ModbusMap) TemplateByID(id string) (*ModbusTemplate, bool) {
	t, ok := m.Templates[id]
	return t, ok
}

// Validity returns the point that says whether this device's registers mean
// anything yet, if it publishes one.
//
// A Modbus register carries no quality flag: two bytes reading zero are
// indistinguishable from two bytes never sampled. Real meters publish a
// separate status point for exactly this reason.
func (t *ModbusTemplate) Validity() (ModbusPoint, bool) {
	for _, p := range t.Points {
		if p.Role == "validity" {
			return p, true
		}
	}
	return ModbusPoint{}, false
}

// ProcessValue returns a transmitter's single measurement point.
func (t *ModbusTemplate) ProcessValue() (ModbusPoint, bool) {
	for _, p := range t.Points {
		if p.Role == "process_value" {
			return p, true
		}
	}
	return ModbusPoint{}, false
}

// Telemetry returns the points that produce samples, with the probe role
// applied when the template is a field transmitter.
//
// An unrecognised probe role yields nothing rather than a default binding: a
// transmitter whose installed location is unknown is measuring something, and
// guessing which loop it belongs to puts a condenser reading on a chilled
// water chart.
func (t *ModbusTemplate) Telemetry(probeRole string) []ModbusPoint {
	if len(t.ProbeRoles) > 0 {
		pv, ok := t.ProcessValue()
		if !ok {
			return nil
		}
		binding, ok := t.ProbeRoles[strings.ToLower(probeRole)]
		if !ok {
			return nil
		}
		pv.Metric = binding.Metric
		pv.Instance = binding.Instance
		out := []ModbusPoint{pv}
		for _, p := range t.Points {
			if p.Role == "" && p.Metric != "" {
				out = append(out, p)
			}
		}
		return out
	}

	out := make([]ModbusPoint, 0, len(t.Points))
	for _, p := range t.Points {
		if p.Role == "" && p.Metric != "" {
			out = append(out, p)
		}
	}
	return out
}

// ProbeRoleNames lists the roles a transmitter template understands.
func (t *ModbusTemplate) ProbeRoleNames() []string {
	out := make([]string, 0, len(t.ProbeRoles))
	for r := range t.ProbeRoles {
		out = append(out, r)
	}
	return out
}
