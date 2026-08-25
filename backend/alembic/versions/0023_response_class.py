"""Alarms say whether they need a response now.

An alarm and an alert are not the same claim. ISA-18.2 and EEMUA 191 separate
them by required response: an alarm is an abnormal condition demanding operator
action, with an acknowledge lifecycle behind it; an alert is informational, is
not expected to be acted on at the console, and belongs to whoever schedules
maintenance. BACnet carries the same split at the wire in `notify_type`
(ALARM / EVENT), set per point when the plant is commissioned.

The home page had a counter labelled "Alarms" sitting among five counters
labelled "alerts", and it was neither of those things - it was the arithmetic
total of the eight categories. This adds the distinction it implied, as an
ATTRIBUTE beside `category` and `detection` rather than as a category: it
answers how urgently somebody must move, not what kind of thing is wrong. A
leak and a dirty filter on the same CDU stay in `cooling`, where the plant team
reads them, and separate on this axis instead.

The default comes from severity, which in this system already encodes
consequence - phase 2 sorted the 36 equipment points that way on purpose, with
integrity faults that threaten load as MAJOR and wear as WARNING. So the axes
agree by construction. A rule that disagrees carries its own override.

What the backfill says about the estate: of 29,500 historical roots, the
staleness rows (22,150 WARNING) become alerts and the comm losses (7,025 MAJOR)
stay alarms. That is the point of the exercise - the console has been carrying
twenty-two thousand conditions nobody was ever going to act on at 3am.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.core.alert_taxonomy import ALARM, response_sql_case

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Server default is ALARM, not ALERT. An insert that forgets to classify
    # should end up on the console and be argued about, not filed quietly as
    # informational - silence is the one failure an alarm system may not
    # produce by accident.
    op.add_column("alarm", sa.Column("response_class", sa.Text(),
                                     nullable=False, server_default=ALARM))
    # Per-rule override. Left NULL unless a condition's urgency genuinely
    # disagrees with its severity.
    op.add_column("alarm_rule", sa.Column("response_class", sa.Text(),
                                          nullable=True))

    # The strip counts open alarms by class; that is the shape of the query.
    op.create_index("ix_alarm_response_class", "alarm",
                    ["response_class", "state"])

    op.get_bind().execute(sa.text(f"""
        UPDATE alarm a
           SET response_class = {response_sql_case(severity_col="a.severity::text")}
    """))


def downgrade() -> None:
    op.drop_index("ix_alarm_response_class", table_name="alarm")
    op.drop_column("alarm_rule", "response_class")
    op.drop_column("alarm", "response_class")
