package snmp

import (
	"fmt"
	"testing"
)

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
	// A FEW sibling columns are walked individually. Collapsing trades round
	// trips for bytes, and that is a bad trade when the mapping reads 6 of a
	// table's 22 columns: the collapsed walk drags back the other 16 from a
	// responder serving the whole fleet from one process.
	got := walkRoots([]string{"1.3.6.1.2.1.2.2.1.7", "1.3.6.1.2.1.2.2.1.8"})
	if len(got) != 2 {
		t.Fatalf("two columns gave %v, want them walked separately", got)
	}

	// MANY sibling columns are worth one walk: at that point most of the
	// table is wanted and the extra round trips cost more than the extra
	// bytes.
	wide := make([]string, 0, wholeTableColumnRatio+1)
	for i := 1; i <= wholeTableColumnRatio+1; i++ {
		wide = append(wide, fmt.Sprintf("1.3.6.1.2.1.2.2.1.%d", i))
	}
	got = walkRoots(wide)
	if len(got) != 1 || got[0] != "1.3.6.1.2.1.2.2.1" {
		t.Fatalf("%d sibling columns gave %v, want one table root", len(wide), got)
	}

	// Columns from two different tables share only 1.3.6.1.2.1. Collapsing
	// those would walk the whole of mib-2, so they must stay separate however
	// many there are.
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
