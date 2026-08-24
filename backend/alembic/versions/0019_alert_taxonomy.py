"""Alarms carry their category and how they were detected.

Phase 1 of docs/18-alert-taxonomy.md. Adds the columns, stamps them at raise
time, and backfills history. Deliberately no behaviour change: nothing new
fires, and the home page keeps its five buckets from `alarm_categories.py`
until the API and UI move in phases 3 and 4. Running both taxonomies for one
step is what makes this reversible.

Why store the category rather than derive it per query, as the old roll-up
does: the classifier is role-sensitive, so deriving it means joining every
alarm through device to device_type on every count, and it means an alarm's
category can change retroactively when a device is re-typed. Stamping it at
raise time records what was true when the alarm was raised, which is what an
operator reading history needs.

`detection` is the other half - threshold, state, absence, derived, forecast -
so "show me only what analytics found" is a filter across every category rather
than a category of its own.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.core.alert_taxonomy import (
    DETECTION_BY_SOURCE,
    THRESHOLD,
    UNCATEGORISED,
    sql_case,
)

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("alarm", sa.Column("category", sa.Text(), nullable=False,
                                     server_default=UNCATEGORISED))
    op.add_column("alarm", sa.Column("detection", sa.Text(), nullable=False,
                                     server_default=THRESHOLD))
    # Rules may override the classifier for a condition the three layers get
    # wrong, and declare their own detection method.
    op.add_column("alarm_rule", sa.Column("category", sa.Text(), nullable=True))
    op.add_column("alarm_rule", sa.Column("detection", sa.Text(), nullable=True))

    # Counting by category, open alarms first: that is the roll-up's shape.
    op.create_index("ix_alarm_category", "alarm", ["category", "state"])
    op.create_index("ix_alarm_detection", "alarm", ["detection"])

    conn = op.get_bind()

    # Backfill through the same generated CASE the application uses, so history
    # and anything raised from now on are classified by one set of rules.
    conn.execute(sa.text(f"""
        UPDATE alarm a
           SET category = c.category
          FROM (
              SELECT al.id,
                     {sql_case(alarm_type_col="al.alarm_type",
                               role_col="dt.category",
                               metric_col="al.metric_key")} AS category
                FROM alarm al
                LEFT JOIN device d      ON d.id = al.device_id
                LEFT JOIN device_type dt ON dt.code = d.device_type
          ) c
         WHERE c.id = a.id
    """))

    for source, detection in DETECTION_BY_SOURCE.items():
        conn.execute(sa.text("""
            UPDATE alarm SET detection = :detection WHERE source = :source
        """), {"detection": detection, "source": source})

    # Equipment fault points are state-reported whatever raised them.
    conn.execute(sa.text("""
        UPDATE alarm SET detection = 'state'
         WHERE metric_key IN ('alarm_state', 'equipment_state')
    """))


def downgrade() -> None:
    op.drop_index("ix_alarm_detection", table_name="alarm")
    op.drop_index("ix_alarm_category", table_name="alarm")
    op.drop_column("alarm_rule", "detection")
    op.drop_column("alarm_rule", "category")
    op.drop_column("alarm", "detection")
    op.drop_column("alarm", "category")
