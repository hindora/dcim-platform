"""A failed power supply is a condition, not a boolean nobody reads.

Every BMC on this fleet reports each supply's Status over Redfish, and the
collector has stored it as `psu_state` per supply instance since the Redfish
adapter was written - True when the BMC says Enabled and OK. Nothing ever
evaluated it. A chassis running on one supply looked exactly like a chassis
running on two, until the second one went.

The rule is boolean, raise on False, any instance: a supply the BMC calls
anything but healthy raises `psu_failure` on the chassis with the supply's
MemberId as the point. MAJOR, because the load is one failure from dark and
the fix is a part on a trolley, not a page at 03:00. Dwell 2 so a BMC that
briefly reports a supply during a hot-swap does not raise on the swap itself.

Revision ID: 0115
Revises: 0114
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None

_INSERT = sa.text("""
    INSERT INTO alarm_rule (name, alarm_type, metric_key, severity,
                            dwell_samples, clear_dwell_samples, device_types,
                            message_tpl, enabled, metric_kind, raise_on,
                            instances, detection)
    VALUES ('psu-failure', 'psu_failure', 'psu_state', CAST('MAJOR' AS severity_t),
            2, 2, '{}', :message_tpl, true, 'boolean', false, '{}', 'state')
    ON CONFLICT (name) DO UPDATE SET
        alarm_type = EXCLUDED.alarm_type,
        metric_key = EXCLUDED.metric_key,
        severity = EXCLUDED.severity,
        dwell_samples = EXCLUDED.dwell_samples,
        clear_dwell_samples = EXCLUDED.clear_dwell_samples,
        message_tpl = EXCLUDED.message_tpl,
        metric_kind = EXCLUDED.metric_kind,
        raise_on = EXCLUDED.raise_on,
        instances = EXCLUDED.instances,
        detection = EXCLUDED.detection
""")


def upgrade() -> None:
    op.get_bind().execute(_INSERT, {
        "message_tpl": "Power supply {point} reported failed by the BMC; the "
                       "chassis is running on its other supply",
    })


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM alarm_rule WHERE name = 'psu-failure'"))
