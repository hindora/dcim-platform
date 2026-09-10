"""A stopped cooling unit is raised by the POLL as well as by the trap.

Revision ID: 0056
Revises: 0055

plant_unit_stopped could only ever be raised by a trap. A trap is one UDP
datagram with no retry and no acknowledgement, so a raise is exactly as losable
as the clear that ends it - and a lost raise is the worse of the two, because a
lost clear leaves a false alarm somebody can see and argue with, while a lost
raise leaves a stopped machine that nobody is told about at all.

The platform already polls the answer. Every plant unit publishes its run
status as a boolean, on change plus a heartbeat, and the thermal page has been
reading it all along - which is how three stopped CRAHs came to sit on that
page marked Stopped with no alarm anywhere on the console.

Scoped to CRAHs and CDUs, deliberately.

"Not running" is not a fault everywhere. Standby chillers, spare pumps and
cycled-off tower cells publish exactly the same boolean by design, and a rule
that alarmed on it would report healthy N+1 redundancy as a fault every day of
the year. CRAHs and CDUs are the machines in this estate expected to run
whenever the hall is live, so they are the ones where stopped means something.
Planned work is suppressed the same way it is for every other alarm here, with
a maintenance window.

Instance filter on the run-status point, because one boolean metric carries
several points on other gear - an ATS publishes three - and this rule is about
one of them.

Dwell of one sample in both directions. A boolean is written when it CHANGES,
so the first false is not a flap that a second sample would confirm, it is the
event; and waiting for a second would mean waiting for the next heartbeat,
which is a quarter of an hour of a hall cooling itself with fewer units than it
thinks it has.

Re-runnable: upserts on the rule name.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None

NAME = "plant-unit-stopped-polled"


def upgrade() -> None:
    op.execute(sa.text("""
        INSERT INTO alarm_rule (
            name, alarm_type, enabled, device_types, metric_key, metric_kind,
            raise_on, instances, dwell_samples, clear_dwell_samples, severity,
            message_tpl, category, detection)
        VALUES (
            :name, 'plant_unit_stopped', true, ARRAY['crah','cdu'],
            'equipment_state', 'boolean',
            false, ARRAY['Unit_Running'], 1, 1, 'MAJOR',
            'Unit reports stopped', 'cooling', 'state')
        ON CONFLICT (name) DO UPDATE SET
            enabled             = EXCLUDED.enabled,
            device_types        = EXCLUDED.device_types,
            metric_key          = EXCLUDED.metric_key,
            metric_kind         = EXCLUDED.metric_kind,
            raise_on            = EXCLUDED.raise_on,
            instances           = EXCLUDED.instances,
            dwell_samples       = EXCLUDED.dwell_samples,
            clear_dwell_samples = EXCLUDED.clear_dwell_samples,
            severity            = EXCLUDED.severity,
            message_tpl         = EXCLUDED.message_tpl,
            category            = EXCLUDED.category,
            detection           = EXCLUDED.detection
    """).bindparams(name=NAME))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM alarm_rule WHERE name = :name")
               .bindparams(name=NAME))
