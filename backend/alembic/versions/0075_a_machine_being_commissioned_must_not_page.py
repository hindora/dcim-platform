"""Shelving gains a REASON, so a second thing is allowed to shelve.

Migration 0043 added `installed` and said what it was for: racked and cabled,
in the elevations and in the capacity figures, and paging nobody. The first two
happened. The third never did - nothing outside `staleness.py` read `lifecycle`
at all, so a machine mid-commissioning either alarmed all night or was invisible,
which is the exact sentence 0043 was written to delete.

WHY A REASON COLUMN AND NOT A SECOND FLAG. `shelved_by_window IS NULL` is the
platform's definition of "an alarm somebody should look at", and it is spelled
out in thirteen queries across five repositories. A second suppression source
expressed as a second column means thirteen predicates that are now each half
right, and a fourteenth written next month by somebody who finds one of the old
ones and copies it. `shelved_reason IS NULL` replaces all of them: ONE predicate,
and adding a third reason later is a new string rather than a new clause.

`shelved_by_window` stays, and stays a nullable FK, for the reason 0046 gave it -
"3 alarms shelved" on a window page is how somebody discovers the window was
scoped too widely, and only the window id can answer by which. It is now the
DETAIL behind one of the reasons rather than the mark itself.

WHY THE BACKFILL TOUCHES `installed` DEVICES TOO. The states exist already and
operators have been moving devices through them; those devices' alarms are on
the console right now. A migration that only preserved the window behaviour
would leave the bug it is fixing in place for every row that predates it.

Revision ID: 0075
Revises: 0074
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None

#: Kept in step with `app.repositories.alarms.SHELVE_REASONS`. A CHECK rather
#: than an enum: the set will grow, and ADD VALUE on an enum cannot be used in
#: the transaction that adds it (see 0043), which makes every future reason a
#: two-migration dance for no benefit.
REASONS = ("maintenance_window", "not_commissioned")


def upgrade() -> None:
    op.add_column("alarm", sa.Column("shelved_reason", sa.Text))
    op.create_check_constraint(
        "alarm_shelved_reason_known", "alarm",
        "shelved_reason IS NULL OR shelved_reason IN "
        f"({', '.join(repr(r) for r in REASONS)})")

    # Every currently shelved alarm keeps being shelved, for the reason it was.
    op.execute("""
        UPDATE alarm SET shelved_reason = 'maintenance_window'
         WHERE shelved_by_window IS NOT NULL
    """)
    # And the rows the feature was supposed to have covered all along.
    #
    # `lifecycle::text = 'installed'`, not `lifecycle = 'installed'`. This is the
    # first migration to actually USE a label 0043 added, and it walked straight
    # into the trap 0043 wrote down: alembic's env.py wraps the whole upgrade in
    # ONE transaction (`context.begin_transaction()` around `run_migrations()`),
    # so on a fresh database 0043's `ALTER TYPE ... ADD VALUE` and this statement
    # are the same transaction, and PostgreSQL refuses with "unsafe use of new
    # value". It only fails on a fresh build - an existing database has had the
    # label committed for thirty migrations - which is exactly the shape of bug
    # that passes locally and fails in CI.
    #
    # Casting the COLUMN to text makes the literal an ordinary string comparison
    # with no enum value to resolve, which is legal in any transaction. Any
    # future migration referencing `in_stock`, `installed` or `retired` needs the
    # same cast.
    op.execute("""
        UPDATE alarm a SET shelved_reason = 'not_commissioned'
          FROM device d
         WHERE d.id = a.device_id
           AND a.state <> 'CLEARED'
           AND a.shelved_reason IS NULL
           AND d.lifecycle::text = 'installed'
    """)

    # The predicate every open-alarm query now uses. Partial, like 0046's, and
    # for the same reason: the shelved set is a small minority of the table.
    op.execute("""
        CREATE INDEX ix_alarm_shelved_reason ON alarm (shelved_reason)
        WHERE shelved_reason IS NOT NULL
    """)


def downgrade() -> None:
    """Leave the window marks correct and drop the rest.

    A row shelved only because its device was `installed` has nowhere to go: the
    older code has one suppression source and that device is not in a window. It
    becomes visible again, which is the old behaviour, bug included.
    """
    op.execute("DROP INDEX IF EXISTS ix_alarm_shelved_reason")
    op.drop_constraint("alarm_shelved_reason_known", "alarm", type_="check")
    op.drop_column("alarm", "shelved_reason")
