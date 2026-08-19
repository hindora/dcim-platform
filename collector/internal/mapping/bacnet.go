package mapping

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

// BACnetPoint maps one object NAME pattern to a metric.
//
// By name, not by instance number: instance numbers in a BMS are assigned in
// the order points were added, so inserting one alarm renumbers everything
// after it and an instance-keyed mapping silently relabels a condenser probe
// as an evaporator probe.
type BACnetPoint struct {
	Name string `yaml:"name"`
	// Metric is the canonical key. Validated against the registry at load.
	Metric string `yaml:"metric"`
	// Instance is a fixed instance label, e.g. the loop a point belongs to.
	Instance string `yaml:"instance"`
	// InstanceFrom derives the instance from the object name instead:
	//   name        the whole object name (used for alarm points)
	//   name_prefix everything before the first underscore (Ckt07_kW -> Ckt07)
	//   name_token  one underscore-separated token, chosen by TokenIndex
	InstanceFrom string `yaml:"instance_from"`
	TokenIndex   int    `yaml:"token_index"`
	// Scale converts the device's unit to the registry's. kW -> W is 1000.
	Scale float64 `yaml:"scale"`
}

type BACnetDeviceType struct {
	Points []BACnetPoint `yaml:"points"`
}

type BACnetMap struct {
	Version      int    `yaml:"version"`
	PollProperty string `yaml:"poll_property"`
	Binary       struct {
		ActiveValue float64 `yaml:"active_value"`
	} `yaml:"binary"`
	Loops       map[string]string           `yaml:"loops"`
	DeviceTypes map[string]BACnetDeviceType `yaml:"device_types"`
}

// LoadBACnet reads contracts/mappings/bacnet/objects.yaml.
func LoadBACnet(dir string) (*BACnetMap, error) {
	path := filepath.Join(dir, "bacnet", "objects.yaml")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	var m BACnetMap
	if err := yaml.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}

	// A metric the registry does not define would be dropped at emit time, so
	// the failure belongs at boot where someone is watching.
	for dt, spec := range m.DeviceTypes {
		if len(spec.Points) == 0 {
			return nil, fmt.Errorf("%s: device type %q has no points", path, dt)
		}
		for _, p := range spec.Points {
			if _, ok := models.ValidateMetric(p.Metric); !ok {
				return nil, fmt.Errorf("%s: %s/%s: unknown metric %q",
					path, dt, p.Name, p.Metric)
			}
			if p.InstanceFrom != "" && p.InstanceFrom != "name" &&
				p.InstanceFrom != "name_prefix" && p.InstanceFrom != "name_token" {
				return nil, fmt.Errorf("%s: %s/%s: unknown instance_from %q",
					path, dt, p.Name, p.InstanceFrom)
			}
		}
	}
	return &m, nil
}

// Match finds the mapping for one object name on one device type.
//
// Exact names win over patterns regardless of file order, so a specific point
// can never be shadowed by a wildcard that happens to appear above it.
func (m *BACnetMap) Match(deviceType, objectName string) (BACnetPoint, bool) {
	spec, ok := m.DeviceTypes[deviceType]
	if !ok {
		return BACnetPoint{}, false
	}
	for _, p := range spec.Points {
		if !strings.Contains(p.Name, "*") && strings.EqualFold(p.Name, objectName) {
			return p, true
		}
	}
	for _, p := range spec.Points {
		if strings.Contains(p.Name, "*") && globMatch(p.Name, objectName) {
			return p, true
		}
	}
	return BACnetPoint{}, false
}

// Instance resolves the metric instance for a matched point.
func (p BACnetPoint) InstanceFor(objectName string) string {
	switch p.InstanceFrom {
	case "name":
		return objectName
	case "name_prefix":
		if i := strings.Index(objectName, "_"); i > 0 {
			return objectName[:i]
		}
		return objectName
	case "name_token":
		parts := strings.Split(objectName, "_")
		if p.TokenIndex >= 0 && p.TokenIndex < len(parts) {
			return parts[p.TokenIndex]
		}
		return ""
	default:
		return p.Instance
	}
}

// Apply converts a raw present-value into the registry's unit.
func (p BACnetPoint) Apply(v float64) float64 {
	if p.Scale != 0 {
		return v * p.Scale
	}
	return v
}

// HasDeviceType reports whether anything is mapped for a device type. An
// unmapped type is polled for nothing, which is worth saying out loud rather
// than discovering as an empty chart.
func (m *BACnetMap) HasDeviceType(deviceType string) bool {
	_, ok := m.DeviceTypes[deviceType]
	return ok
}

// PointCount is the number of mapped patterns for a device type.
func (m *BACnetMap) PointCount(deviceType string) int {
	return len(m.DeviceTypes[deviceType].Points)
}
