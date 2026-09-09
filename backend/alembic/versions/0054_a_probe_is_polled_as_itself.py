"""Poll a rack probe as a device, not as a column on its strip.

Revision ID: 0054
Revises: 0053

Twenty rack environmental probes were carried in inventory and never spoken
to. They have no address - a probe on an AP8000's sensor port is polled by the
strip over an RJ-45 lead - so nothing ever gave them an endpoint, they had no
state row at all, and every rack page showed them with an unknown status.
Their readings arrived instead as columns on the strip, which meant a rack with
two probes at two heights reported one temperature and a probe that stopped
answering was invisible.

The estate already models exactly this shape twice: an RTU slave behind a
Modbus gateway and an MS/TP controller behind a BACnet router both take the
carrier's address and a protocol sub-address. A probe takes the strip's
address and its sensor index, and the mapping profile reads that index.

`snmp-probe-120s` matches the strip's own cadence: the probe is read through
the same agent in the same walk window, and asking faster would only poll the
strip more often.

Re-runnable: upserts on the profile name. The endpoints themselves are created
by the importer, which is what knows which strip carries which probe.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None

PROFILE = "snmp-probe-120s"
#: No `system` group: a probe has no agent of its own, so a sysUpTime read
#: here would return the STRIP's uptime and record it against the probe.
GROUPS = ["pdu_probe_apc"]


def upgrade() -> None:
    op.execute(sa.text("""
        INSERT INTO poll_profile (name, interval_s, timeout_ms, retries, metric_groups)
        VALUES (:name, 120, 3000, 2, :groups)
        ON CONFLICT (name) DO UPDATE SET
            interval_s    = EXCLUDED.interval_s,
            metric_groups = EXCLUDED.metric_groups
    """).bindparams(name=PROFILE, groups=GROUPS))


def downgrade() -> None:
    # The endpoints reference it, so they go first or the delete is refused.
    op.execute(sa.text("""
        DELETE FROM device_endpoint
         WHERE poll_profile_id = (SELECT id FROM poll_profile WHERE name = :name)
    """).bindparams(name=PROFILE))
    op.execute(sa.text("DELETE FROM poll_profile WHERE name = :name")
               .bindparams(name=PROFILE))
