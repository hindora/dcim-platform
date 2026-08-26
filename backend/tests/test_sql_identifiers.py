"""CTE names must not be words Postgres has already spoken for.

`), window AS (` shipped, passed every test, and failed on the first sweep
against a real database: WINDOW is a clause of SELECT, so a CTE cannot be
called that. The tests around it read the SQL as text and asserted on its
shape - they never asked a database to parse it, and nothing else in the suite
does either.

This is the cheap half of that gap. It cannot tell whether a query is correct;
it can tell whether the names in it are ones Postgres will reject outright,
which is the failure that reaches production as a stack trace in a background
sweep nobody is watching.

The list is the RESERVED words from the Postgres keyword table - the ones that
cannot be used as an identifier without quoting. Non-reserved keywords like
`name` or `value` are legal and are deliberately absent.
"""

from __future__ import annotations

import pathlib
import re

import pytest

# Postgres 16, "Key Words" appendix: category RESERVED, plus the few marked
# "reserved (can be function or type name)" that still cannot open a CTE.
RESERVED = {
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc",
    "asymmetric", "both", "case", "cast", "check", "collate", "column",
    "constraint", "create", "current_catalog", "current_date", "current_role",
    "current_time", "current_timestamp", "current_user", "default",
    "deferrable", "desc", "distinct", "do", "else", "end", "except", "false",
    "fetch", "for", "foreign", "from", "grant", "group", "having", "in",
    "initially", "intersect", "into", "lateral", "leading", "limit",
    "localtime", "localtimestamp", "not", "null", "offset", "on", "only",
    "or", "order", "placing", "primary", "references", "returning", "select",
    "session_user", "some", "symmetric", "table", "then", "to", "trailing",
    "true", "union", "unique", "user", "using", "variadic", "when", "where",
    "window", "with",
    # Reserved-ish: legal as a function or type name, still not as a CTE.
    "authorization", "binary", "collation", "concurrently", "cross",
    "current_schema", "freeze", "full", "ilike", "inner", "is", "isnull",
    "join", "left", "like", "natural", "notnull", "outer", "overlaps",
    "right", "similar", "tablesample", "verbose",
}

BACKEND = pathlib.Path(__file__).resolve().parents[1]
SQL_DIRS = ("app/alarms", "app/repositories", "app/services", "app/api")

# `foo AS (` at the start of a CTE, after WITH or a comma.
CTE = re.compile(r"(?:WITH|,)\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s*\(", re.I)


def sql_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for d in SQL_DIRS:
        out.extend((BACKEND / d).rglob("*.py"))
    return out


@pytest.mark.parametrize("path", sql_files(), ids=lambda p: p.name)
def test_no_cte_is_named_after_a_reserved_word(path: pathlib.Path):
    body = path.read_text(encoding="utf-8")
    bad = sorted({name for name in CTE.findall(body)
                  if name.lower() in RESERVED})
    assert not bad, (
        f"{path.relative_to(BACKEND)} names a CTE after a reserved word: "
        f"{', '.join(bad)}. Postgres will reject the statement at parse time, "
        f"which surfaces as a stack trace in whatever background sweep runs it."
    )


def test_the_guard_would_have_caught_the_one_that_shipped():
    """The regression this exists for, so the matcher itself is tested."""
    shipped = """
        WITH candidate AS (SELECT 1), window AS (SELECT 2) SELECT * FROM window
    """
    found = {n.lower() for n in CTE.findall(shipped)}
    assert "window" in found
    assert found & RESERVED


def test_ordinary_names_are_not_flagged():
    fine = "WITH measured AS (SELECT 1), pair AS (SELECT 2) SELECT 1"
    assert not {n.lower() for n in CTE.findall(fine)} & RESERVED
