package mapping

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

// RedfishEntry maps a sensor NAME pattern to a metric.
//
// By name, not by array index: BMC firmware reorders Temperatures and Fans
// between releases, so an index-based mapping quietly relabels a CPU probe as
// an inlet probe after an update.
type RedfishEntry struct {
	Match        string `yaml:"match"`
	Metric       string `yaml:"metric"`
	InstanceFrom string `yaml:"instance_from"`
}

type RedfishTemperatures struct {
	ReadingField string         `yaml:"reading_field"`
	Entries      []RedfishEntry `yaml:"entries"`
}

type RedfishFans struct {
	ReadingField string `yaml:"reading_field"`
	MetricRPM    string `yaml:"metric_rpm"`
	MetricPct    string `yaml:"metric_pct"`
	InstanceFrom string `yaml:"instance_from"`
}

type RedfishField struct {
	Field  string `yaml:"field"`
	Metric string `yaml:"metric"`
}

type RedfishPSUState struct {
	Metric            string `yaml:"metric"`
	StatusStateField  string `yaml:"status_state_field"`
	StatusHealthField string `yaml:"status_health_field"`
	HealthyState      string `yaml:"healthy_state"`
	HealthyHealth     string `yaml:"healthy_health"`
}

type RedfishPower struct {
	PowerControl []RedfishField `yaml:"power_control"`
	PowerSupply  struct {
		InstanceFrom string          `yaml:"instance_from"`
		Fields       []RedfishField  `yaml:"fields"`
		State        RedfishPSUState `yaml:"state"`
	} `yaml:"power_supplies"`
}

type RedfishMap struct {
	Version int `yaml:"version"`
	Thermal struct {
		Temperatures RedfishTemperatures `yaml:"temperatures"`
		Fans         RedfishFans         `yaml:"fans"`
	} `yaml:"thermal"`
	Power    RedfishPower `yaml:"power"`
	SkipWhen struct {
		StatusStateNotIn []string `yaml:"status_state_not_in"`
		NullReading      bool     `yaml:"null_reading"`
	} `yaml:"skip_when"`
}

// LoadRedfish reads contracts/mappings/redfish/resources.yaml.
func LoadRedfish(dir string) (*RedfishMap, error) {
	path := filepath.Join(dir, "redfish", "resources.yaml")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	var m RedfishMap
	if err := yaml.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}

	// Same rule as the SNMP mappings: a metric the registry does not define
	// would be dropped at emit time, so fail at boot instead.
	check := func(key string) error {
		if key == "" {
			return nil
		}
		if _, ok := models.ValidateMetric(key); !ok {
			return fmt.Errorf("%s: unknown metric %q", path, key)
		}
		return nil
	}
	for _, e := range m.Thermal.Temperatures.Entries {
		if err := check(e.Metric); err != nil {
			return nil, err
		}
	}
	for _, k := range []string{m.Thermal.Fans.MetricRPM, m.Thermal.Fans.MetricPct,
		m.Power.PowerSupply.State.Metric} {
		if err := check(k); err != nil {
			return nil, err
		}
	}
	for _, f := range m.Power.PowerControl {
		if err := check(f.Metric); err != nil {
			return nil, err
		}
	}
	for _, f := range m.Power.PowerSupply.Fields {
		if err := check(f.Metric); err != nil {
			return nil, err
		}
	}
	return &m, nil
}

// MatchTemperature returns the metric for a sensor name, first pattern wins.
func (m *RedfishMap) MatchTemperature(name string) (RedfishEntry, bool) {
	for _, e := range m.Thermal.Temperatures.Entries {
		if globMatch(e.Match, name) {
			return e, true
		}
	}
	return RedfishEntry{}, false
}

// globMatch matches a case-insensitive pattern where '*' stands for any run of
// characters, in any position.
//
// An earlier version handled only a leading or trailing '*', which meant a
// pattern with one in the MIDDLE - "Ckt*_Current", "Harmonic_*_Current" -
// matched nothing at all. Nothing errored: the points were simply discovered,
// counted as unmapped, and never polled, so a 42-circuit panel silently
// yielded its seven panel totals and none of its branch circuits. Anchoring
// every segment in order is what makes a mid-pattern wildcard behave.
func globMatch(pattern, s string) bool {
	if pattern == "" || pattern == "*" {
		return true
	}
	ls := strings.ToLower(s)
	segments := strings.Split(strings.ToLower(pattern), "*")

	if len(segments) == 1 {
		return ls == segments[0]
	}
	// A pattern not starting with '*' must match at the very beginning, and
	// one not ending with '*' must reach the very end.
	if segments[0] != "" {
		if !strings.HasPrefix(ls, segments[0]) {
			return false
		}
		ls = ls[len(segments[0]):]
	}
	last := segments[len(segments)-1]
	middle := segments[1 : len(segments)-1]
	if last != "" {
		if !strings.HasSuffix(ls, last) {
			return false
		}
		ls = ls[:len(ls)-len(last)]
	}
	// Remaining segments must appear in order, without overlapping.
	for _, seg := range middle {
		if seg == "" {
			continue
		}
		i := strings.Index(ls, seg)
		if i < 0 {
			return false
		}
		ls = ls[i+len(seg):]
	}
	return true
}

// GlobMatchForTest exposes the pattern matcher to the adapter's tests without
// widening the package's real surface.
func GlobMatchForTest(pattern, s string) bool { return globMatch(pattern, s) }
