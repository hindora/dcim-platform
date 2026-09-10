"""A Raritan-hosted probe needs its own poll profile.

Revision ID: 0055
Revises: 0054

0054 gave rack probes an endpoint of their own, addressed as the strip plus a
sensor index, and read them with `pdu_probe_apc`. Every probe in the estate sat
on an APC strip at the time, so one profile was every probe.

The spine and management racks have since been instrumented, and those racks
carry Raritan PX2 strips. An APC AP9335 does not plug into a PX2 sensor port,
so the part fitted there is a DPX2 - and the two vendors publish an external
sensor differently. APC gives one index a temperature column and a humidity
column. Raritan gives each channel its own SLOT in one value column: a T1H1 at
slot 1 reads temperature at 1 and humidity at 2. Reading a DPX2 with the APC
profile asks for OIDs the strip does not implement, which is not an error - it
is silence, and the probe reads as fitted, polled and empty.

Same cadence as the APC profile and for the same reason: the probe is read
through the strip's own agent, in the same walk window, so asking faster only
polls the strip more often.

Re-runnable: upserts on the profile name. The endpoints are created by the
importer, which is what knows which strip carries which probe and therefore
which of the two profiles it needs.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

PROFILE = "snmp-probe-raritan-120s"
#: No `system` group, for the same reason as the APC profile: a probe has no
#: agent of its own, so a sysUpTime read here would return the STRIP's uptime
#: and record it against the probe.
GROUPS = ["pdu_probe_raritan"]


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
