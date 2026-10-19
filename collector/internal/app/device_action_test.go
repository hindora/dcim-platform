package app

import (
	"strings"
	"testing"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

func epOf(proto string, addressing map[string]any) *models.Endpoint {
	return &models.Endpoint{ID: "ep1", Protocol: proto, Address: "10.52.12.26", Addressing: addressing}
}

func TestOnlyOutletControlAndTheOneResetGetThrough(t *testing.T) {
	snmp, rf := epOf("snmp", nil), epOf("redfish", nil)
	ok := []struct {
		name string
		act  deviceAction
		ep   *models.Endpoint
	}{
		{"APC outlet off", deviceAction{Op: "snmp_set", Type: "integer", Value: 2,
			OID: "1.3.6.1.4.1.318.1.1.26.9.2.4.1.5.12"}, snmp},
		{"APC outlet cycle", deviceAction{Op: "snmp_set", Type: "integer", Value: 3,
			OID: "1.3.6.1.4.1.318.1.1.26.9.2.4.1.5.1"}, snmp},
		{"Raritan outlet on", deviceAction{Op: "snmp_set", Type: "integer", Value: 1,
			OID: "1.3.6.1.4.1.13742.6.4.1.2.1.2.1.24"}, snmp},
		{"Raritan outlet off", deviceAction{Op: "snmp_set", Type: "integer", Value: 0,
			OID: "1.3.6.1.4.1.13742.6.4.1.2.1.2.1.3"}, snmp},
		{"graceful restart", deviceAction{Op: "redfish_action", ActionPath: resetActionPath,
			Body: map[string]any{"ResetType": "GracefulRestart"}}, rf},
		{"power on", deviceAction{Op: "redfish_action", ActionPath: resetActionPath,
			Body: map[string]any{"ResetType": "On"}}, rf},
	}
	for _, c := range ok {
		if err := validateAction(&c.act, c.ep); err != nil {
			t.Errorf("%s was refused: %v", c.name, err)
		}
	}
}

func TestAnythingElseIsRefusedHere(t *testing.T) {
	snmp, rf := epOf("snmp", nil), epOf("redfish", nil)
	bad := []struct {
		name, want string
		act        deviceAction
		ep         *models.Endpoint
	}{
		{"no endpoint", "does not own", deviceAction{Op: "snmp_set"}, nil},
		{"sysName", "not a PDU outlet-control OID", deviceAction{Op: "snmp_set", Type: "integer",
			Value: 1, OID: "1.3.6.1.2.1.1.5.0"}, snmp},
		{"another vendor's tree", "not a PDU outlet-control OID", deviceAction{Op: "snmp_set",
			Type: "integer", Value: 1, OID: "1.3.6.1.4.1.9.2.1.1.0"}, snmp},
		{"APC column, not the outlet", "not a PDU outlet-control OID", deviceAction{Op: "snmp_set",
			Type: "integer", Value: 1, OID: "1.3.6.1.4.1.318.1.1.26.9.2.4.1.5"}, snmp},
		{"trailing junk", "not a PDU outlet-control OID", deviceAction{Op: "snmp_set",
			Type: "integer", Value: 1, OID: "1.3.6.1.4.1.318.1.1.26.9.2.4.1.5.12.0"}, snmp},
		{"a string write", "only INTEGER", deviceAction{Op: "snmp_set", Type: "octet",
			OID: "1.3.6.1.4.1.318.1.1.26.9.2.4.1.5.1"}, snmp},
		{"a command the MIB lacks", "not an outlet command", deviceAction{Op: "snmp_set",
			Type: "integer", Value: 9, OID: "1.3.6.1.4.1.318.1.1.26.9.2.4.1.5.1"}, snmp},
		{"SNMP aimed at a BMC", "aimed at a redfish", deviceAction{Op: "snmp_set"}, rf},
		{"a different Redfish action", "not an allowed Redfish action", deviceAction{
			Op: "redfish_action", ActionPath: "Actions/Manager.ResetToDefaults",
			Body: map[string]any{"ResetType": "ResetAll"}}, rf},
		{"a reset that does not exist", "not an allowed reset", deviceAction{
			Op: "redfish_action", ActionPath: resetActionPath,
			Body: map[string]any{"ResetType": "Nuke"}}, rf},
		{"extra body keys", "not an allowed reset", deviceAction{
			Op: "redfish_action", ActionPath: resetActionPath,
			Body: map[string]any{"ResetType": "On", "Force": true}}, rf},
		{"Redfish aimed at SNMP", "aimed at a snmp", deviceAction{Op: "redfish_action"}, snmp},
		{"an unknown operation", "unknown device action", deviceAction{Op: "shell"}, snmp},
	}
	for _, c := range bad {
		err := validateAction(&c.act, c.ep)
		if err == nil {
			t.Errorf("%s was allowed", c.name)
			continue
		}
		if !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: got %q, want it to say %q", c.name, err, c.want)
		}
	}
}

func TestWritePortComesFromTheEndpointNotTheCommand(t *testing.T) {
	cases := []struct {
		addressing map[string]any
		want       int
	}{
		{nil, 0},
		{map[string]any{}, 0},
		{map[string]any{"set_port": float64(1161)}, 1161}, // JSON numbers
		{map[string]any{"set_port": 1161}, 1161},
		{map[string]any{"set_port": "1161"}, 1161}, // typed by an operator
		{map[string]any{"set_port": "junk"}, 0},
	}
	for _, c := range cases {
		if got := setPortOf(epOf("snmp", c.addressing)); got != c.want {
			t.Errorf("%v -> %d, want %d", c.addressing, got, c.want)
		}
	}
}
