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


def downgrade() -> None:
    """Not reversible, and saying so is better than pretending.

    PostgreSQL cannot drop a value from an enum. Undoing this means recreating
    the type, rewriting every column that uses it, and deciding what happens to
    rows already holding a label that is going away - which is a data decision,
    not a schema one, and cannot be made here.
    """
    raise NotImplementedError(
        "cannot remove a value from a PostgreSQL enum; "
        "recreate lifecycle_t by hand and migrate the rows that use the new "
        "labels first")
