"""Re-file open box conditions that were filed as fabric faults.

Revision ID: 0029
Revises: 0028

`BY_ROLE["network"]` used to be NETWORK, so every condition on a switch,
router, firewall or load balancer that had no explicit entry in the classifier
was filed as a fabric fault - and 46 of the 72 trap event types have no entry.
A firewall with a pinned control plane sat in `network` while the same fact,
arriving by poll instead of by trap, sat in `it_equipment`.

The classifier is fixed. Category is stamped at raise time and deliberately
never recomputed - rewriting history would move alarms between owners whenever
the taxonomy is edited - so the rows raised under the old default would keep
their old category until they cleared. For a long-lived alarm that is
indefinitely.

This corrects the OPEN ones only, and only where the alarm type now has an
EXPLICIT entry: `cpu_high_usage`, `memory_high_usage`, `device_restarted`,
`server_power_on`, `rack_failure`. Anything still resolved by role is left
alone, because for those the old row and the new rule genuinely disagree about
nothing - the role layer would have to be re-run per device to know, and a
migration that re-classifies by guesswork is worse than one that does less.

Cleared alarms keep what they were raised with. They are history, and history
is what the incident actually looked like at the time.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

# alarm_type -> the category the classifier now returns for it.
BOX_CONDITIONS = (
    "cpu_high_usage",
    "memory_high_usage",
    "device_restarted",
    "server_power_on",
    "rack_failure",
)

REFILE = sa.text("""
    UPDATE alarm
       SET category = 'it_equipment'
     WHERE state <> 'CLEARED'
       AND category = 'network'
       AND alarm_type = ANY(:types)
""")

# The down path restores only what this could have changed: the same alarm
# types, still open, still in it_equipment.
UNDO = sa.text("""
    UPDATE alarm
       SET category = 'network'
     WHERE state <> 'CLEARED'
       AND category = 'it_equipment'
       AND alarm_type = ANY(:types)
""")


def upgrade() -> None:
    op.get_bind().execute(REFILE, {"types": list(BOX_CONDITIONS)})


def downgrade() -> None:
    op.get_bind().execute(UNDO, {"types": list(BOX_CONDITIONS)})
