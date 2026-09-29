// Package vault resolves a "credential_ref" credential (docs/26 Phase 4)
// against an external secrets manager.
//
// The platform never learns the actual secret for one of these: a
// credential_ref's payload is a reference string - "vault:hashicorp:secret/
// data/switches/core1" - sealed and delivered like any other credential,
// but the value it points to is resolved here, locally, by a collector
// that has network access to the vault the platform itself may not. This
// is the "passed through unresolved" half of the plan: the platform's job
// stops at delivering the reference intact.
package vault

import (
	"context"
	"fmt"
	"strings"
)

// Resolver looks up one secrets manager's references. rest is the ref with
// its "vault:<backend>:" prefix already stripped - a HashiCorp resolver
// sees a KV path, a CyberArk one sees an object query.
type Resolver interface {
	Resolve(ctx context.Context, rest string) (map[string]any, error)
}

// Registry dispatches a credential_ref by the backend named in its prefix.
// Registering nothing for a backend a ref names is a configuration gap,
// not a crash: Resolve reports it as an ordinary error, the same as any
// other credential this collector cannot currently use.
type Registry struct {
	resolvers map[string]Resolver
}

func NewRegistry() *Registry {
	return &Registry{resolvers: map[string]Resolver{}}
}

// Register attaches a Resolver under a backend name - "hashicorp",
// "cyberark" - matching the second segment of a "vault:<backend>:..." ref.
func (r *Registry) Register(backend string, resolver Resolver) {
	r.resolvers[backend] = resolver
}

// Resolve dispatches ref to the registered backend and returns the secret
// payload it names, shaped the same as any other credential's Data map.
func (r *Registry) Resolve(ctx context.Context, ref string) (map[string]any, error) {
	backend, rest, err := splitRef(ref)
	if err != nil {
		return nil, err
	}
	resolver, ok := r.resolvers[backend]
	if !ok {
		return nil, fmt.Errorf("no vault resolver registered for backend %q "+
			"(from ref %q)", backend, ref)
	}
	return resolver.Resolve(ctx, rest)
}

// splitRef parses "vault:<backend>:<rest>", where rest may itself contain
// colons (a Vault path segment, a CyberArk query), so only the first two
// separators are meaningful.
func splitRef(ref string) (backend, rest string, err error) {
	parts := strings.SplitN(ref, ":", 3)
	if len(parts) != 3 || parts[0] != "vault" || parts[1] == "" || parts[2] == "" {
		return "", "", fmt.Errorf(
			"malformed credential_ref %q: want \"vault:<backend>:<path>\"", ref)
	}
	return parts[1], parts[2], nil
}
