"""Raise the SNMP poll interval to 120 s.

Revision ID: 0010
Revises: 0009

Measured against the simulator's device plane: SNMP demand was 24.7 polls a
second (310 server agents at 30 s, 308 BMC agents at 60 s, 178 power devices at
30 s, 98 network devices at 30 s) against a responder that delivers about 3.
Eight times oversubscribed, so the collector was permanently behind and
endpoints flapped DEGRADED on transient timeouts.

At 120 s the same 894 endpoints ask for 7.5 polls a second. That is a 3.3x
reduction and still above what this plane can serve - the honest number for a
sweep that fits would be nearer 300 s - but it is the interval asked for, and
the remaining gap is a property of one snmpsim process serving 894 agents
rather than of any device.

snmp-sensor is deliberately left at 10 s: it currently has no endpoints (the
environmental probes on this plane are Modbus RTU behind a gateway), so it
contributes nothing to the load, and 10 s is the right cadence for temperature
and humidity if it is ever used.

The profiles are renamed with it. A row called snmp-server-30s that polls every
120 s is a trap for whoever reads it next, and the name is what the importer
selects on.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

# (old name, new name, new interval)
CHANGES = [
    ("snmp-server-30s", "snmp-server-120s", 120),
    ("snmp-network-30s", "snmp-network-120s", 120),
    ("snmp-power-30s", "snmp-power-120s", 120),
    ("snmp-bmc-60s", "snmp-bmc-120s", 120),
]

RENAME = sa.text("""
    UPDATE poll_profile
       SET name = :new, interval_s = :interval
     WHERE name = :old
""")


def upgrade() -> None:
    conn = op.get_bind()
    for old, new, interval in CHANGES:
        conn.execute(RENAME, {"old": old, "new": new, "interval": interval})
    # Endpoints reference the profile by id, so nothing needs re-pointing: the
    # rename is invisible to them and the new interval reaches the collector on
    # its next assignment refresh, without a restart.


def downgrade() -> None:
    conn = op.get_bind()
    originals = {
        "snmp-server-120s": ("snmp-server-30s", 30),
        "snmp-network-120s": ("snmp-network-30s", 30),
        "snmp-power-120s": ("snmp-power-30s", 30),
        "snmp-bmc-120s": ("snmp-bmc-60s", 60),
    }
    for new, (old, interval) in originals.items():
        conn.execute(RENAME, {"old": new, "new": old, "interval": interval})
