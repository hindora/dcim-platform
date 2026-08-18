"""Seed reference data: device types, poll profiles, metric registry.

Revision ID: 0003
Revises: 0002

The metric rows are generated from contracts/metrics/registry.yaml via the
generated ``app.core.metrics_gen`` module, so the database and the collector can
never disagree about what a metric means. Re-running is an upsert; a metric that
disappears from the registry is marked deprecated rather than deleted, because
hypertable rows still reference its id.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.core.metrics_gen import METRICS

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


# (code, display_name, category, is_rack_mounted)
# Floor-standing plant is located by room, not by rack unit: emitting a rack
# position for a chiller would be a lie the UI then renders.
DEVICE_TYPES = [
    ("server", "Server", "it", True),
    ("switch", "Switch", "network", True),
    ("router", "Router", "network", True),
    ("firewall", "Firewall", "network", True),
    ("load_balancer", "Load Balancer", "network", True),
    ("oob_switch", "OOB Management Switch", "network", True),
    ("ups", "UPS", "power", False),
    ("pdu", "Rack PDU", "power", True),
    ("floor_pdu", "Floor PDU", "power", False),
    ("rpp", "Remote Power Panel", "power", False),
    ("mcc", "Motor Control Center", "power", False),
    ("mpp", "Mechanical Power Panel", "power", False),
    ("switchgear", "Switchgear", "power", False),
    ("ats", "Automatic Transfer Switch", "power", False),
    ("generator", "Generator", "power", False),
    ("utility_feed", "Utility Feed", "power", False),
    ("energy_monitor", "Energy Monitor", "power", False),
    ("chiller", "Chiller", "cooling", False),
    ("cooling_tower", "Cooling Tower", "cooling", False),
    ("pump", "Pump", "cooling", False),
    ("crah", "CRAH", "cooling", False),
    ("crac", "CRAC", "cooling", False),
    ("cdu", "Coolant Distribution Unit", "cooling", True),
    ("valve", "Valve", "cooling", False),
    ("sensor", "Environmental Sensor", "environment", True),
    ("modbus_gateway", "Modbus Gateway", "facility", False),
    ("bacnet_router", "BACnet Router", "facility", False),
]

# (name, interval_s, timeout_ms, retries, metric_groups, push_enabled)
POLL_PROFILES = [
    ("snmp-server-30s", 30, 3000, 2, ["system", "interfaces", "host_resources"], False),
    ("snmp-network-30s", 30, 3000, 2, ["system", "interfaces"], False),
    ("snmp-power-30s", 30, 3000, 2, ["system"], False),
    ("snmp-sensor-10s", 10, 3000, 2, ["system", "entity_sensors"], False),
    ("snmp-bmc-60s", 60, 5000, 1, ["system"], False),
    ("redfish-60s", 60, 8000, 1, [], True),
    ("bacnet-10s", 10, 5000, 2, [], True),
    ("modbus-30s", 30, 3000, 2, [], False),
    ("gnmi-stream", 0, 5000, 1, ["interfaces"], True),
]


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text("""
            INSERT INTO device_type (code, display_name, category, is_rack_mounted)
            VALUES (:code, :display_name, :category, :is_rack_mounted)
            ON CONFLICT (code) DO UPDATE
              SET display_name = EXCLUDED.display_name,
                  category = EXCLUDED.category,
                  is_rack_mounted = EXCLUDED.is_rack_mounted
        """),
        [{"code": c, "display_name": d, "category": cat, "is_rack_mounted": r}
         for c, d, cat, r in DEVICE_TYPES],
    )

    conn.execute(
        sa.text("""
            INSERT INTO poll_profile
                (name, interval_s, timeout_ms, retries, metric_groups, push_enabled)
            VALUES (:name, :interval_s, :timeout_ms, :retries, :metric_groups, :push_enabled)
            ON CONFLICT (name) DO UPDATE
              SET interval_s = EXCLUDED.interval_s,
                  timeout_ms = EXCLUDED.timeout_ms,
                  retries = EXCLUDED.retries,
                  metric_groups = EXCLUDED.metric_groups,
                  push_enabled = EXCLUDED.push_enabled
        """),
        [{"name": n, "interval_s": i, "timeout_ms": t, "retries": r,
          "metric_groups": g, "push_enabled": p}
         for n, i, t, r, g, p in POLL_PROFILES],
    )

    conn.execute(
        sa.text("""
            INSERT INTO metric (key, display_name, unit, value_type, aggregation,
                                min_valid, max_valid, stale_after_s, is_hot)
            VALUES (:key, :display_name, :unit, :value_type, :aggregation,
                    :min_valid, :max_valid, :stale_after_s, :is_hot)
            ON CONFLICT (key) DO UPDATE
              SET display_name = EXCLUDED.display_name,
                  unit = EXCLUDED.unit,
                  value_type = EXCLUDED.value_type,
                  aggregation = EXCLUDED.aggregation,
                  min_valid = EXCLUDED.min_valid,
                  max_valid = EXCLUDED.max_valid,
                  stale_after_s = EXCLUDED.stale_after_s,
                  is_hot = EXCLUDED.is_hot,
                  deprecated_at = NULL
        """),
        [{"key": m.key, "display_name": m.display_name, "unit": m.unit,
          "value_type": m.value_type, "aggregation": m.aggregation,
          "min_valid": m.min_valid, "max_valid": m.max_valid,
          "stale_after_s": m.stale_after_s, "is_hot": m.hot}
         for m in METRICS.values()],
    )

    # Anything the registry no longer defines is deprecated, never removed.
    conn.execute(
        sa.text("""
            UPDATE metric SET deprecated_at = now()
            WHERE deprecated_at IS NULL AND key <> ALL(:keys)
        """),
        {"keys": list(METRICS.keys())},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM metric"))
    conn.execute(sa.text("DELETE FROM poll_profile"))
    conn.execute(sa.text("DELETE FROM device_type"))
