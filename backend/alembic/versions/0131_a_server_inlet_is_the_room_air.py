"""A server's inlet is the room's air: inlet alarms are environmental.

alert_taxonomy now pins inlet_temp_high / inlet_temp_critical to
ENVIRONMENTAL. A server's inlet is the air it breathes - the point ASHRAE's
envelope is judged at - so the condition belongs to the space whichever
device reports it. Resolved by the reporting device's role, a stopped chiller
plant (all-chillers, 2026-11-04) put 180 servers at critical inlet in the IT
column of the home page, beside their real IT faults.

This re-files the alarms already stored, open and history alike, so the
console does not show one hall's inlets in two columns, and drops any rule's
explicit IT override for these two types so the classifier decides.

Revision ID: 0131
Revises: 0130
"""

from __future__ import annotations

from alembic import op

revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None

_TYPES = "('inlet_temp_high', 'inlet_temp_critical')"


def upgrade() -> None:
    op.execute(f"""
        UPDATE alarm SET category = 'environmental'
         WHERE alarm_type IN {_TYPES} AND category IS DISTINCT FROM 'environmental'
    """)
    op.execute(f"""
        UPDATE alarm_rule SET category = NULL
         WHERE alarm_type IN {_TYPES} AND category = 'it_equipment'
    """)


def downgrade() -> None:
    # The old resolution by role: a server's inlet filed as IT, a probe's as
    # environmental.
    op.execute(f"""
        UPDATE alarm a SET category = 'it_equipment'
          FROM device d
         WHERE d.id = a.device_id AND a.alarm_type IN {_TYPES}
           AND d.device_type IN ('server', 'switch', 'router', 'firewall',
                                 'load_balancer', 'storage', 'oob_switch')
    """)
