package snmp

import (
	"errors"
	"strings"
	"testing"

	g "github.com/gosnmp/gosnmp"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

func TestARefusedWriteIsReportedAsWhatTheDeviceSaid(t *testing.T) {
	cases := []struct {
		status g.SNMPError
		kind   error
		says   string
	}{
		{g.NoSuchName, models.ErrProtocolStatus, "no such control"},
		{g.WrongValue, models.ErrProtocolStatus, "refused the value"},
		{g.BadValue, models.ErrProtocolStatus, "refused the value"},
		{g.NoAccess, models.ErrAuth, "read-only"},
		{g.NotWritable, models.ErrAuth, "read-only"},
		{g.AuthorizationError, models.ErrAuth, "read-only"},
		{g.GenErr, models.ErrProtocolStatus, "SNMP error"},
	}
	for _, c := range cases {
		err := setError(c.status)
		if !errors.Is(err, c.kind) {
			t.Errorf("%s: wrong class: %v", c.status, err)
		}
		if !strings.Contains(err.Error(), c.says) {
			t.Errorf("%s: %q does not say %q", c.status, err, c.says)
		}
	}
}
