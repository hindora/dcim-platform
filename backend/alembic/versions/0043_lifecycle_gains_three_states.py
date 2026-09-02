"""lifecycle_t gains in_stock, installed and retired - and nothing else.

Three states earn a row because each changes what the system DOES, not because
the vocabulary reads better (docs/19 B5):

  in_stock   received, not placed. No rack, nothing polls it, and it is still an
             asset somebody owns and has to find.
  installed  racked and cabled, not yet accepted. It must appear in elevations
             and in capacity, and it must NOT page anyone. No state does both
             today, which is why a machine mid-commissioning either alarms all
             night or is invisible.
  retired    decommissioned and disposed. Leaves the estate, keeps its history.

The ten states the original plan proposed collapse to seven: `installed`,
`commissioned` and `operational` all mean "racked and working", `operational`
renames the existing `in_service`, and `ordered`/`received` are procurement -
real work, belonging to a purchasing system this platform is not.

THIS MIGRATION DOES NOTHING ELSE, and that is deliberate rather than tidy.
PostgreSQL 12+ permits ALTER TYPE ... ADD VALUE inside a transaction but forbids
USING the new label in the same one: a migration that added `in_stock` and then
ran `UPDATE device SET lifecycle = 'in_stock'` fails with "unsafe use of new
value". Alembic wraps each migration in a transaction and will not warn you.
Adding the labels alone, one migration early, is what makes 0044 and everything
after it able to reference them.

Revision ID: 0043
Revises: 0042
"""

from __future__ import annotations

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

# Order matters: Postgres sorts an enum by declaration order, and lifecycle is
# read as a progression. Each new label is placed where it actually falls.
NEW_STATES = (
    ("in_stock", "AFTER 'planned'"),
    ("installed", "AFTER 'in_stock'"),
    ("retired", "AFTER 'decommissioned'"),
)


def upgrade() -> None:
    for label, position in NEW_STATES:
        op.execute(
            f"ALTER TYPE lifecycle_t ADD VALUE IF NOT EXISTS '{label}' {position}")


# Where a row goes when its state stops existing. Every one of these is
# lossy, and the mapping is a data decision written down rather than left to
# whoever runs the rollback at 2am.
RETIRING = {
    # Owned, not in service, and not yet placed anywhere.
    "in_stock": "planned",
    # Racked and cabled. `in_service` is the least-wrong survivor - it keeps the
    # machine in capacity and in elevations, which is true, at the cost of it
    # starting to alarm, which is the whole reason `installed` exists. `planned`
    # would be worse: it would read as not-yet-delivered hardware that is
    # physically in a rack.
    "installed": "in_service",
    "retired": "decommissioned",
}


def downgrade() -> None:
    """Put the rows back on states the older code understands.

    The LABELS stay. PostgreSQL cannot drop a value from an enum, and removing
    one means recreating the type and rewriting every column that uses it - for
    three labels that are inert the moment nothing references them. Downgrading
    all the way to base drops `lifecycle_t` outright in 0001, so they do not
    survive a full rollback either way.

    What matters is that no row is left holding a state the code being rolled
    back to has never heard of, which would fail on the way into its enum. That
    is what this does.
    """
    for old, new in RETIRING.items():
        op.execute(
            f"UPDATE device SET lifecycle = '{new}' WHERE lifecycle = '{old}'")
