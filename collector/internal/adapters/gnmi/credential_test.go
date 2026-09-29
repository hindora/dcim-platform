package gnmi

import (
	"context"
	"testing"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

// TestPollSendsTheEndpointCredentialAsGRPCMetadata is docs/26 Phase 4's
// "wire gNMI SetCredential" made concrete: before this, nothing ever called
// SetCredential at all, so a gNMI endpoint's stored credential never
// reached the device - every poll authenticated as nobody. This proves the
// credential a Poll call is given actually lands in the real gRPC metadata
// a real server receives, not just that SetCredential was called.
func TestPollSendsTheEndpointCredentialAsGRPCMetadata(t *testing.T) {
	f := newFakeTarget(t)
	a := newAdapter(t)
	ep := endpointFor(f)
	ep.Credential = &models.Credential{
		Kind: "gnmi_basic",
		Data: map[string]any{"username": "admin", "password": "hunter2"},
	}

	if _, err := a.Poll(context.Background(), ep); err != nil {
		t.Fatalf("Poll: %v", err)
	}

	md := f.lastMetadata()
	if got := md.Get("username"); len(got) != 1 || got[0] != "admin" {
		t.Errorf("username metadata = %v, want [admin]", got)
	}
	if got := md.Get("password"); len(got) != 1 || got[0] != "hunter2" {
		t.Errorf("password metadata = %v, want [hunter2]", got)
	}
}

func TestPollWithNoCredentialSendsNoAuthMetadata(t *testing.T) {
	f := newFakeTarget(t)
	a := newAdapter(t)
	ep := endpointFor(f)

	if _, err := a.Poll(context.Background(), ep); err != nil {
		t.Fatalf("Poll: %v", err)
	}

	md := f.lastMetadata()
	if got := md.Get("username"); len(got) != 0 {
		t.Errorf("username metadata = %v, want none for a credential-less endpoint", got)
	}
}

func TestTargetOfDefaultsVerifyTLSToTrue(t *testing.T) {
	tgt, err := targetOf(&models.Endpoint{Address: "10.0.0.1"})
	if err != nil {
		t.Fatalf("targetOf: %v", err)
	}
	if !tgt.verifyTLS {
		t.Error("a new gNMI target must default verify_tls to true")
	}
}

func TestTargetOfHonoursExplicitVerifyTLSFalse(t *testing.T) {
	tgt, err := targetOf(&models.Endpoint{
		Address: "10.0.0.1", Addressing: map[string]any{"verify_tls": false},
	})
	if err != nil {
		t.Fatalf("targetOf: %v", err)
	}
	if tgt.verifyTLS {
		t.Error("addressing.verify_tls: false was not honoured")
	}
}
