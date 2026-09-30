package config

import "testing"

// docs/26 Phase 9: FDR needs a BBMD to register with - an enabled flag and no
// address would register nowhere and simply never discover anything, with no
// error telling anyone why.
func TestValidateRejectsFDREnabledWithoutABBMD(t *testing.T) {
	c := Default()
	c.DCIM.token = "t"
	c.Protocols.BACnet.FDR.Enabled = true

	if err := c.Validate(); err == nil {
		t.Fatal("expected an error for fdr.enabled with no bbmd address")
	}
}

func TestValidateAcceptsFDRWithABBMD(t *testing.T) {
	c := Default()
	c.DCIM.token = "t"
	c.Protocols.BACnet.FDR.Enabled = true
	c.Protocols.BACnet.FDR.BBMD = "10.51.3.1:47808"

	if err := c.Validate(); err != nil {
		t.Fatalf("Validate: %v", err)
	}
}

func TestValidateAcceptsFDRDisabledWithoutABBMD(t *testing.T) {
	c := Default() // FDR disabled by default, no bbmd set
	c.DCIM.token = "t"

	if err := c.Validate(); err != nil {
		t.Fatalf("Validate: %v", err)
	}
}
