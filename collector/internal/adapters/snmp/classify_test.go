package snmp

import (
	"testing"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

func TestEmptyPollWithOnlyTimeoutsIsATimeout(t *testing.T) {
	// A device that never answered is unreachable. Calling that a decode error
	// sends an operator hunting a MIB problem on a box that is off the network.
	misses := []models.Miss{
		{Metric: "sys_uptime", Reason: models.MissTimeout},
		{Metric: "if_table", Reason: models.MissTimeout},
	}
	err := emptyPollError(misses, &models.Endpoint{Address: "10.50.0.1"})
	if got := models.ClassifyError(err); got != models.ErrClassTimeout {
		t.Fatalf("error class %q, want %q", got, models.ErrClassTimeout)
	}
}

func TestEmptyPollWithANonTimeoutMissIsADecodeError(t *testing.T) {
	// Something answered and we could not use it: that really is a decode fault.
	misses := []models.Miss{
		{Metric: "sys_uptime", Reason: models.MissTimeout},
		{Metric: "cpu_utilization", Reason: models.MissNoSuchObject},
	}
	err := emptyPollError(misses, &models.Endpoint{Address: "10.50.0.1"})
	if got := models.ClassifyError(err); got != models.ErrClassDecode {
		t.Fatalf("error class %q, want %q", got, models.ErrClassDecode)
	}
}

func TestEmptyPollWithNoMissesIsADecodeError(t *testing.T) {
	err := emptyPollError(nil, &models.Endpoint{Address: "10.50.0.1"})
	if got := models.ClassifyError(err); got != models.ErrClassDecode {
		t.Fatalf("error class %q, want %q", got, models.ErrClassDecode)
	}
}
