package mapping

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

// RedfishEventClass is one classified event: what it is, how bad, and whether
// it resolves an existing alarm.
type RedfishEventClass struct {
	EventType    string `yaml:"event_type"`
	Severity     string `yaml:"severity"`
	IsClear      bool   `yaml:"is_clear"`
	InstanceFrom string `yaml:"instance_from"`
}

type redfishPattern struct {
	Match             string `yaml:"match"`
	RedfishEventClass `yaml:",inline"`
}

// RedfishEventMap classifies inbound Redfish events.
type RedfishEventMap struct {
	Version     int                          `yaml:"version"`
	SeverityMap map[string]string            `yaml:"severity_map"`
	ClearSuffix string                       `yaml:"clear_suffix"`
	MessageIDs  map[string]RedfishEventClass `yaml:"message_ids"`
	Patterns    []redfishPattern             `yaml:"patterns"`
	Unknown     RedfishEventClass            `yaml:"unknown"`
}

func LoadRedfishEvents(dir string) (*RedfishEventMap, error) {
	path := filepath.Join(dir, "redfish", "events.yaml")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	var m RedfishEventMap
	if err := yaml.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	if m.ClearSuffix == "" {
		m.ClearSuffix = " cleared"
	}
	if m.Unknown.EventType == "" {
		m.Unknown = RedfishEventClass{EventType: "unknown_event", Severity: "INFO"}
	}

	// A severity typo would resolve to UNSPECIFIED, which sorts below INFO and
	// would hide the event entirely. Fail at boot instead.
	for _, c := range m.MessageIDs {
		if _, ok := models.ParseSeverity(c.Severity); !ok {
			return nil, fmt.Errorf("%s: unknown severity %q", path, c.Severity)
		}
	}
	for _, p := range m.Patterns {
		if _, ok := models.ParseSeverity(p.Severity); !ok {
			return nil, fmt.Errorf("%s: %q: unknown severity %q", path, p.Match, p.Severity)
		}
	}
	for wire, canon := range m.SeverityMap {
		if _, ok := models.ParseSeverity(canon); !ok {
			return nil, fmt.Errorf("%s: severity_map[%s]: unknown severity %q",
				path, wire, canon)
		}
	}
	return &m, nil
}

// Classify resolves one event.
//
// The order matters. MessageId wins because it is the only identifier that is
// stable across firmware; the message text is consulted only when the id is
// unmapped, which is the common case on firmware that ships a single OEM id
// for every condition.
//
// A clearing event ("<label> cleared", Severity OK) is re-matched on the
// stripped label so it lands on the same event_type as the assert. Without
// that, the clear opens a second alarm instead of resolving the first.
func (m *RedfishEventMap) Classify(messageID, message, severity string) (RedfishEventClass, bool) {
	if c, ok := m.MessageIDs[messageID]; ok {
		return m.withSeverity(c, severity), true
	}

	text := strings.TrimSpace(message)
	isClear := false
	if strings.HasSuffix(text, m.ClearSuffix) {
		text = strings.TrimSpace(strings.TrimSuffix(text, m.ClearSuffix))
		isClear = true
	}

	for _, p := range m.Patterns {
		if !globMatch(p.Match, text) {
			continue
		}
		c := m.withSeverity(p.RedfishEventClass, severity)
		if isClear {
			c.IsClear = true
			c.Severity = "CLEAR"
		}
		return c, true
	}
	return m.withSeverity(m.Unknown, severity), false
}

// withSeverity lets the event's OWN severity override the mapping's. The BMC
// knows whether this instance crossed the warning or the critical threshold;
// the mapping only knows the default for that condition.
func (m *RedfishEventMap) withSeverity(c RedfishEventClass, wire string) RedfishEventClass {
	if wire == "" {
		return c
	}
	mapped, ok := m.SeverityMap[wire]
	if !ok {
		return c
	}
	// An OK on an asserting pattern is a clear, not a downgrade to INFO.
	if mapped == "CLEAR" && !c.IsClear {
		return c
	}
	c.Severity = mapped
	return c
}

// SeverityFor maps a wire severity with no pattern context.
func (m *RedfishEventMap) SeverityFor(wire string) (string, bool) {
	s, ok := m.SeverityMap[wire]
	return s, ok
}

// InstanceFrom extracts the instance a class asks for out of the message.
//
// "Fan failed: FAN1 stopped (0 RPM)" -> "FAN1". The instance is what keeps two
// failed fans on one chassis as two alarms instead of one that flaps.
func InstanceFrom(mode, message string) string {
	if mode != "first_word_after_colon" {
		return ""
	}
	_, rest, found := strings.Cut(message, ":")
	if !found {
		return ""
	}
	fields := strings.Fields(rest)
	if len(fields) == 0 {
		return ""
	}
	return fields[0]
}
