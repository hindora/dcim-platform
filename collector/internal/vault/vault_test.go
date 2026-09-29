package vault

import (
	"context"
	"errors"
	"testing"
)

type fakeResolver struct {
	got string
	out map[string]any
	err error
}

func (f *fakeResolver) Resolve(_ context.Context, rest string) (map[string]any, error) {
	f.got = rest
	return f.out, f.err
}

func TestRegistryDispatchesByBackendAndStripsThePrefix(t *testing.T) {
	reg := NewRegistry()
	hc := &fakeResolver{out: map[string]any{"password": "x"}}
	reg.Register("hashicorp", hc)

	out, err := reg.Resolve(context.Background(), "vault:hashicorp:switches/core1")
	if err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	if hc.got != "switches/core1" {
		t.Errorf("resolver saw rest = %q, want the prefix stripped", hc.got)
	}
	if out["password"] != "x" {
		t.Errorf("Resolve returned %+v", out)
	}
}

func TestRegistryRejectsAnUnregisteredBackend(t *testing.T) {
	reg := NewRegistry()
	_, err := reg.Resolve(context.Background(), "vault:cyberark:some-id")
	if err == nil {
		t.Fatal("expected an error for an unregistered backend")
	}
}

func TestRegistryRejectsAMalformedRef(t *testing.T) {
	reg := NewRegistry()
	reg.Register("hashicorp", &fakeResolver{})
	for _, bad := range []string{
		"", "not-a-ref", "vault:", "vault:hashicorp", "vault:hashicorp:",
		"nothashicorp:hashicorp:path",
	} {
		if _, err := reg.Resolve(context.Background(), bad); err == nil {
			t.Errorf("ref %q was accepted, want a malformed-ref error", bad)
		}
	}
}

func TestRegistryPropagatesTheResolverError(t *testing.T) {
	reg := NewRegistry()
	want := errors.New("vault is sealed")
	reg.Register("hashicorp", &fakeResolver{err: want})
	_, err := reg.Resolve(context.Background(), "vault:hashicorp:x")
	if !errors.Is(err, want) {
		t.Errorf("error = %v, want it to wrap %v", err, want)
	}
}
