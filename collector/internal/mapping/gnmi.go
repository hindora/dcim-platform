package mapping

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

// GNMILeaf maps one leaf inside a returned subtree to a metric.
type GNMILeaf struct {
	// At is a slash-separated path RELATIVE to the enclosing subtree or list
	// entry. Module prefixes are stripped when matching - see GNMIMap.
	At     string  `yaml:"at"`
	Metric string  `yaml:"metric"`
	Scale  float64 `yaml:"scale"`
	// Enum maps a symbolic value to a number. openconfig reports link state
	// and port speed as names, not integers.
	Enum map[string]float64 `yaml:"enum"`
	// Instance overrides the enclosing list key.
	Instance string `yaml:"instance"`
}

// Value applies the scale a leaf declares.
func (l GNMILeaf) Value(v float64) float64 {
	if l.Scale != 0 {
		return v * l.Scale
	}
	return v
}

// GNMIList is a keyed list inside a subtree, such as the interface list.
type GNMIList struct {
	At  string `yaml:"at"`
	Key string `yaml:"key"`
	// Kind marks a list whose key is an identity the platform normalises.
	// See the SNMP Table.Kind for why.
	Kind   string     `yaml:"kind"`
	Leaves []GNMILeaf `yaml:"leaves"`
}

// GNMIEntryOverride retargets one list entry, chosen by its key value.
type GNMIEntryOverride struct {
	Metric   string `yaml:"metric"`
	Instance string `yaml:"instance"`
}

// GNMISubscription is one subtree to ask for and what to take from it.
type GNMISubscription struct {
	Name string `yaml:"name"`
	Path string `yaml:"path"`
	// SampleInterval is what the device is asked to send at in STREAM mode.
	// The device may send faster or slower; it is a request, not a contract.
	SampleInterval time.Duration `yaml:"sample_interval"`
	// List is the primary keyed list, when the subtree is one.
	List *GNMIList `yaml:"list"`
	// Lists are additional keyed lists nested inside the subtree.
	Lists []GNMIList `yaml:"lists"`
	// Leaves hang directly off the subtree root.
	Leaves  []GNMILeaf                   `yaml:"leaves"`
	Entries map[string]GNMIEntryOverride `yaml:"entries"`
}

type GNMIMap struct {
	Version       int                `yaml:"version"`
	Encoding      string             `yaml:"encoding"`
	Subscriptions []GNMISubscription `yaml:"subscriptions"`
}

// LoadGNMI reads contracts/mappings/gnmi/paths.yaml.
func LoadGNMI(dir string) (*GNMIMap, error) {
	path := filepath.Join(dir, "gnmi", "paths.yaml")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	var m GNMIMap
	if err := yaml.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	if len(m.Subscriptions) == 0 {
		return nil, fmt.Errorf("%s: no subscriptions", path)
	}

	check := func(sub, where string, leaves []GNMILeaf) error {
		for _, l := range leaves {
			if _, ok := models.ValidateMetric(l.Metric); !ok {
				return fmt.Errorf("%s: %s/%s%s: unknown metric %q",
					path, sub, where, l.At, l.Metric)
			}
		}
		return nil
	}
	for _, s := range m.Subscriptions {
		if s.Path == "" {
			return nil, fmt.Errorf("%s: subscription %q has no path", path, s.Name)
		}
		if err := check(s.Name, "", s.Leaves); err != nil {
			return nil, err
		}
		if s.List != nil {
			if s.List.Key == "" {
				return nil, fmt.Errorf("%s: %s: a list needs a key", path, s.Name)
			}
			if err := check(s.Name, s.List.At+"/", s.List.Leaves); err != nil {
				return nil, err
			}
		}
		for _, l := range s.Lists {
			if l.Key == "" {
				return nil, fmt.Errorf("%s: %s: a list needs a key", path, s.Name)
			}
			if err := check(s.Name, l.At+"/", l.Leaves); err != nil {
				return nil, err
			}
		}
		for name, e := range s.Entries {
			if e.Metric != "" {
				if _, ok := models.ValidateMetric(e.Metric); !ok {
					return nil, fmt.Errorf("%s: %s: entry %s: unknown metric %q",
						path, s.Name, name, e.Metric)
				}
			}
		}
	}
	return &m, nil
}

// PathElems splits a mapping path like "/interfaces" into gNMI path elements.
func PathElems(p string) []string {
	p = strings.Trim(p, "/")
	if p == "" {
		return nil
	}
	return strings.Split(p, "/")
}

// ------------------------------------------------------- tree walking

// LocalName strips an RFC 7951 module prefix.
//
// A JSON_IETF key is "openconfig-interfaces:interfaces" at a module boundary
// and "interfaces" elsewhere, and vendors disagree about where the boundary
// is. Matching on the local name is what lets one mapping serve Arista, Cisco
// and Juniper without three copies.
func LocalName(key string) string {
	if i := strings.IndexByte(key, ':'); i >= 0 {
		return key[i+1:]
	}
	return key
}

// Descend walks a decoded JSON tree by a slash-separated path, ignoring module
// prefixes on each key.
func Descend(node any, path string) (any, bool) {
	cur := node
	for _, want := range strings.Split(strings.Trim(path, "/"), "/") {
		if want == "" {
			continue
		}
		obj, ok := cur.(map[string]any)
		if !ok {
			return nil, false
		}
		if v, ok := obj[want]; ok {
			cur = v
			continue
		}
		found := false
		for k, v := range obj {
			if LocalName(k) == want {
				cur, found = v, true
				break
			}
		}
		if !found {
			return nil, false
		}
	}
	return cur, true
}

// AsList returns a node as a list of objects.
//
// A single-entry list is often encoded as a bare object rather than an array,
// which is legal and catches out anything that type-asserts to a slice.
func AsList(node any) []map[string]any {
	switch v := node.(type) {
	case []any:
		out := make([]map[string]any, 0, len(v))
		for _, item := range v {
			if m, ok := item.(map[string]any); ok {
				out = append(out, m)
			}
		}
		return out
	case map[string]any:
		return []map[string]any{v}
	default:
		return nil
	}
}
