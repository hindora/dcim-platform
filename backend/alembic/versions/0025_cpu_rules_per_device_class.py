"""CPU thresholds that mean something for the device they are on.

Revision ID: 0025
Revises: 0024

One rule - `cpu_high`, >90% on every device type in the estate - was covering
two conditions that have nothing to do with each other.

A SERVER at 100% CPU is not a fault. It is the machine doing the job it was
bought for, and a datacenter that pages on it will page on every batch window
and every busy hour until nobody reads the page. Compute health is thermal
throttling, ECC, PSU and fan - all of which this platform already watches - and
above that it is the workload's own SLO, which lives in the workload's own
monitoring and not in the DCIM. So there is no enabled server CPU rule here.
One is seeded DISABLED so the decision is visible and reversible rather than
absent: 98% held for half an hour, which is a machine that is stuck rather than
busy.

A SWITCH at 100% CPU is a different animal entirely. That is the control plane
- the process that runs BGP, LACP, ARP/ND, LLDP and everything punted out of
the ASIC - and when it saturates the box keeps forwarding while it stops
answering. Keepalives are missed, adjacencies drop, and the failure presents as
a network problem somewhere else entirely. Causes are the classic ones: route
churn, a CoPP policy that is not catching a punt storm, an ARP scan, or a
broadcast storm behind a bridging loop. Arista, Cisco and Juniper all expose it
the same way over gNMI - /system/cpus/cpu/state/total - and the operational
convention is 80% sustained for five minutes rather than a momentary peak,
because a convergence event legitimately pins the CPU for a few seconds.

Hence: 80% for the alert, 95% for the alarm, both held for five minutes, both
scoped to the things that have a control plane.

`dwell_seconds` alongside `dwell_samples` matters here. Network gear is on the
600 s SNMP profile and the gNMI stream pushes on its own cadence, so a dwell
counted only in samples means something different on every plane. The rules ask
for both: three samples AND five minutes.

Open `cpu_high` alarms on device types the rule no longer covers are cleared,
with history. A rule that stops evaluating leaves its alarms standing forever
otherwise - there is nothing left to clear them, and a permanently open alarm
that nothing can close is worse than no alarm at all.

Re-runnable: upserts on rule name.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

# What has a control plane. `oob_switch` is in here for the same reason as the
# rest: it is a switch, and the management network going deaf is how an
# operator loses the ability to see anything else.
NETWORK_TYPES = ["switch", "router", "firewall", "load_balancer", "oob_switch"]

CONTROL_PLANE_MSG = (
    "Control-plane CPU {value}% sustained above {threshold}% - protocol "
    "keepalives and adjacencies are at risk while the box keeps forwarding"
)

UPSERT = sa.text("""
    INSERT INTO alarm_rule (name, alarm_type, metric_key, operator, threshold,
                            clear_threshold, dwell_samples, dwell_seconds,
                            clear_dwell_samples, severity, device_types,
                            message_tpl, enabled)
    VALUES (:name, :alarm_type, :metric_key, :operator, :threshold,
            :clear_threshold, :dwell_samples, :dwell_seconds,
            :clear_dwell_samples, CAST(:severity AS severity_t), :device_types,
            :message_tpl, :enabled)
    ON CONFLICT (name) DO UPDATE SET
        alarm_type          = EXCLUDED.alarm_type,
        metric_key          = EXCLUDED.metric_key,
        operator            = EXCLUDED.operator,
        threshold           = EXCLUDED.threshold,
        clear_threshold     = EXCLUDED.clear_threshold,
        dwell_samples       = EXCLUDED.dwell_samples,
        dwell_seconds       = EXCLUDED.dwell_seconds,
        clear_dwell_samples = EXCLUDED.clear_dwell_samples,
        severity            = EXCLUDED.severity,
        device_types        = EXCLUDED.device_types,
        message_tpl         = EXCLUDED.message_tpl,
        enabled             = EXCLUDED.enabled
""")

RULES = [
    # name, alarm_type, threshold, clear, dwell_samples, dwell_s, clear_dwell,
    # severity, device_types, message, enabled
    ("cpu-high", "cpu_high", 80, 70, 3, 300, 2, "WARNING",
     NETWORK_TYPES, CONTROL_PLANE_MSG, True),
    ("cpu-saturated", "cpu_saturated", 95, 85, 3, 300, 2, "MAJOR",
     NETWORK_TYPES,
     "Control-plane CPU {value}% - the device may already be missing "
     "keepalives; check for route churn, a punt storm or a bridging loop",
     True),
    ("server-cpu-saturated", "server_cpu_saturated", 98, 90, 15, 1800, 3,
     "INFO", ["server"],
     "CPU pinned at {value}% for half an hour - stuck rather than busy",
     False),
]

# Anything that is neither a control plane nor a server: PDUs, CRAHs, chillers,
# sensors. Their "CPU" is an embedded controller's housekeeping load and means
# nothing to an operator, which is most of what the old rule was reporting.
CLEAR_ORPHANS = sa.text("""
    WITH orphan AS (
        SELECT a.id, a.device_id, a.severity
        FROM alarm a
        JOIN device d ON d.id = a.device_id
        WHERE a.alarm_type = 'cpu_high'
          AND a.state <> 'CLEARED'
          AND NOT (d.device_type = ANY(:types))
    ), logged AS (
        INSERT INTO alarm_history (alarm_id, device_id, action, severity,
                                   actor, detail)
        SELECT id, device_id, 'clear', severity, 'migration:0025',
               '{"reason": "cpu_high no longer evaluates this device class"}'
        FROM orphan
        RETURNING alarm_id
    )
    UPDATE alarm SET state = 'CLEARED', cleared_at = now()
    WHERE id IN (SELECT id FROM orphan)
""")


def upgrade() -> None:
    bind = op.get_bind()
    for (name, alarm_type, threshold, clear, dwell, dwell_s, clear_dwell,
         severity, types, message, enabled) in RULES:
        bind.execute(UPSERT, {
            "name": name, "alarm_type": alarm_type,
            "metric_key": "cpu_utilization", "operator": ">",
            "threshold": threshold, "clear_threshold": clear,
            "dwell_samples": dwell, "dwell_seconds": dwell_s,
            "clear_dwell_samples": clear_dwell, "severity": severity,
            "device_types": types, "message_tpl": message, "enabled": enabled,
        })
    bind.execute(CLEAR_ORPHANS, {"types": NETWORK_TYPES})


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("""
        DELETE FROM alarm_rule WHERE name IN ('cpu-saturated',
                                              'server-cpu-saturated')
    """))
    # Back to the single estate-wide rule, thresholds and all.
    bind.execute(sa.text("""
        UPDATE alarm_rule
           SET threshold = 90, clear_threshold = 80, dwell_samples = 5,
               dwell_seconds = NULL, clear_dwell_samples = 3,
               device_types = '{}',
               message_tpl = 'CPU {value}% sustained above {threshold}%'
         WHERE name = 'cpu-high'
    """))
