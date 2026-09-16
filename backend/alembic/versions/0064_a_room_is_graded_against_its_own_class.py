"""A room is graded against the class of kit standing in it.

Revision ID: 0064
Revises: 0063

The thermal page grades every hall against ASHRAE class A1: recommended
18-27 C, allowable 15-32. A1 is the right default - it is the tightest
envelope, and an estate that has not said otherwise should be held to the
strictest thing its equipment might be - but it is a default, not a fact.

A hall of A3-rated kit is allowed to 40 C by the same standard. Graded as A1
it reads critical from 32 C, which is an alarm on equipment operating inside
its own specification, and the operator learns to skip the row. A hall of tape
is the opposite: its RATE limit is 5 K/hour rather than 20, because tape is
the one medium in a building that a fast swing destroys rather than stresses.

So the class rides on the room, and the rate limit with it:

  ashrae_class      A1 | A2 | A3 | A4.  NULL means nobody has classified it,
                    which is graded as A1 and says so on the page rather than
                    pretending the room was surveyed.
  max_rate_k_per_h  NULL means the class default of 20. A room holding tape is
                    set to 5 by hand - there is no telemetry that can tell you
                    what medium is in a rack.

Recommended limits do NOT vary by class. That is what "recommended" means: the
band you run in for efficiency and headroom, the same for every class of kit.
Only what the hardware will SURVIVE changes, which is why this column moves
the allowable envelope and leaves 18-27 alone.
"""
from alembic import op
import sqlalchemy as sa

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

_CLASSES = ("A1", "A2", "A3", "A4")


def upgrade() -> None:
    op.add_column("room", sa.Column("ashrae_class", sa.Text(), nullable=True))
    op.add_column("room", sa.Column("max_rate_k_per_h", sa.Numeric(5, 2),
                                    nullable=True))
    # A typo here is a hall silently graded against the wrong envelope, and the
    # page has no way to notice: every value produces a plausible number.
    op.create_check_constraint(
        "ck_room_ashrae_class",
        "room",
        "ashrae_class IS NULL OR ashrae_class IN ('A1','A2','A3','A4')",
    )
    op.create_check_constraint(
        "ck_room_max_rate_positive",
        "room",
        "max_rate_k_per_h IS NULL OR max_rate_k_per_h > 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_room_max_rate_positive", "room", type_="check")
    op.drop_constraint("ck_room_ashrae_class", "room", type_="check")
    op.drop_column("room", "max_rate_k_per_h")
    op.drop_column("room", "ashrae_class")
