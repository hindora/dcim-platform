package snmp

import "testing"

func TestCommonOIDPrefix(t *testing.T) {
	cases := []struct {
		name string
		in   []string
		want string
	}{
		{"ifTable columns", []string{
			"1.3.6.1.2.1.2.2.1.7", "1.3.6.1.2.1.2.2.1.8", "1.3.6.1.2.1.2.2.1.5",
		}, "1.3.6.1.2.1.2.2.1"},
		{"entity sensor columns", []string{
			"1.3.6.1.2.1.99.1.1.1.1", "1.3.6.1.2.1.99.1.1.1.3", "1.3.6.1.2.1.99.1.1.1.4",
		}, "1.3.6.1.2.1.99.1.1.1"},
		{"single column walks itself", []string{
			"1.3.6.1.2.1.25.3.3.1.2",
		}, "1.3.6.1.2.1.25.3.3.1.2"},
		{"leading dots tolerated", []string{
			".1.3.6.1.2.1.2.2.1.7", "1.3.6.1.2.1.2.2.1.8",
		}, "1.3.6.1.2.1.2.2.1"},
		{"empty", nil, ""},
	}
	for _, c := range cases {
		if got := commonOIDPrefix(c.in); got != c.want {
			t.Fatalf("%s: got %q, want %q", c.name, got, c.want)
		}
	}
}

func TestCommonOIDPrefixDoesNotOverReach(t *testing.T) {
	// Columns .1 and .15 share ...1.1, and a walk root of ...1.1 would be
	// wrong: it must stop at the table, not at a column that happens to be a
	// string prefix of another.
	got := commonOIDPrefix([]string{"1.3.6.1.2.1.31.1.1.1.1", "1.3.6.1.2.1.31.1.1.1.15"})
	if got != "1.3.6.1.2.1.31.1.1.1" {
		t.Fatalf("got %q, want the table root", got)
	}
}

func TestWalkRootsCollapsesOnlyRealTableColumns(t *testing.T) {
	// Siblings of one table: a single walk.
	got := walkRoots([]string{"1.3.6.1.2.1.2.2.1.7", "1.3.6.1.2.1.2.2.1.8"})
	if len(got) != 1 || got[0] != "1.3.6.1.2.1.2.2.1" {
		t.Fatalf("sibling columns gave %v, want one table root", got)
	}

	// Columns from two different tables share only 1.3.6.1.2.1. Collapsing
	// those would walk the whole of mib-2, so they must stay separate.
	mixed := []string{"1.3.6.1.2.1.2.2.1.7", "1.3.6.1.2.1.31.1.1.1.15"}
	got = walkRoots(mixed)
	if len(got) != 2 {
		t.Fatalf("cross-table columns collapsed to %v; that walks all of mib-2", got)
	}

	if got := walkRoots([]string{"1.3.6.1.2.1.25.3.3.1.2"}); len(got) != 1 {
		t.Fatalf("single column gave %v", got)
	}
	if got := walkRoots(nil); got != nil {
		t.Fatalf("empty gave %v", got)
	}
}
