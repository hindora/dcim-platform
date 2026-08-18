"""Baseline schema: hierarchy, catalog, devices, endpoints, connections, state.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Declaration order of severity_t IS its precedence: MAX(severity) rolls racks
# and rooms up, and Postgres orders enums by declaration.
ENUMS = {
    "protocol_t": ["snmp", "snmp_trap", "gnmi", "bacnet", "redfish", "modbus", "sflow", "manual"],
    "endpoint_role_t": ["os_agent", "bmc", "native_card", "field_device", "gateway", "router"],
    "comm_status_t": ["ONLINE", "DEGRADED", "OFFLINE", "UNKNOWN", "DISABLED"],
    "health_t": ["OK", "WARNING", "CRITICAL", "UNKNOWN"],
    "severity_t": ["CLEAR", "INFO", "WARNING", "MINOR", "MAJOR", "CRITICAL"],
    "layer_t": ["production", "management", "power", "cooling", "fieldbus"],
    "termination_t": ["interface", "outlet", "psu", "none"],
    "admin_state_t": ["enabled", "disabled", "maintenance"],
    "lifecycle_t": ["planned", "in_service", "maintenance", "decommissioned"],
}


def _e(name: str) -> pg.ENUM:
    return pg.ENUM(*ENUMS[name], name=name, create_type=False)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    for name, values in ENUMS.items():
        vals = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({vals})")

    # ------------------------------------------------------------ hierarchy
    op.create_table(
        "datacenter",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("city", sa.Text),
        sa.Column("country", sa.Text),
        sa.Column("timezone", sa.Text, nullable=False, server_default="UTC"),
        sa.Column("design_it_kw", sa.Numeric(10, 2)),
        sa.Column("design_pue", sa.Numeric(4, 3)),
        sa.Column("attributes", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    op.create_table(
        "room",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("datacenter_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("datacenter.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("floor", sa.Text),
        sa.Column("room_type", sa.Text, nullable=False, server_default="data_hall"),
        sa.Column("width_m", sa.Numeric(8, 2)),
        sa.Column("depth_m", sa.Numeric(8, 2)),
        sa.Column("design_it_kw", sa.Numeric(10, 2)),
        sa.Column("attributes", pg.JSONB, nullable=False, server_default="{}"),
        sa.UniqueConstraint("datacenter_id", "name", name="uq_room_datacenter_id"),
    )

    op.create_table(
        "rack_row",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("room_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("room.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cold_aisle", sa.Text),
        sa.Column("hot_aisle", sa.Text),
        sa.UniqueConstraint("room_id", "name", name="uq_rack_row_room_id"),
    )

    op.create_table(
        "rack",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("row_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("rack_row.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False, server_default="0"),
        sa.Column("u_height", sa.Integer, nullable=False, server_default="42"),
        sa.Column("facing", sa.String(1)),
        sa.Column("floor_x", sa.Numeric(8, 2)),
        sa.Column("floor_y", sa.Numeric(8, 2)),
        sa.Column("rated_power_kw", sa.Numeric(8, 2)),
        sa.Column("rated_cool_kw", sa.Numeric(8, 2)),
        sa.Column("attributes", pg.JSONB, nullable=False, server_default="{}"),
        sa.UniqueConstraint("row_id", "name", name="uq_rack_row_id"),
    )

    # -------------------------------------------------------------- catalog
    op.create_table(
        "vendor",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("enterprise_oid", sa.Text),
    )

    op.create_table(
        "device_type",
        sa.Column("code", sa.Text, primary_key=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("is_rack_mounted", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("icon", sa.Text),
    )

    op.create_table(
        "model",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("vendor_id", pg.UUID(as_uuid=True), sa.ForeignKey("vendor.id"),
                  nullable=False),
        sa.Column("device_type", sa.Text, sa.ForeignKey("device_type.code"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("u_height", sa.Integer, nullable=False, server_default="1"),
        sa.Column("rated_power_w", sa.Integer),
        sa.Column("rated_capacity", sa.Numeric(12, 2)),
        sa.Column("capacity_unit", sa.Text),
        sa.Column("attributes", pg.JSONB, nullable=False, server_default="{}"),
        sa.UniqueConstraint("vendor_id", "name", name="uq_model_vendor_id"),
    )

    # --------------------------------------------------------------- device
    op.create_table(
        "device",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("external_id", sa.Text, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("device_type", sa.Text, sa.ForeignKey("device_type.code"), nullable=False),
        sa.Column("model_id", pg.UUID(as_uuid=True), sa.ForeignKey("model.id")),
        sa.Column("vendor_id", pg.UUID(as_uuid=True), sa.ForeignKey("vendor.id")),
        sa.Column("serial_number", sa.Text),
        sa.Column("asset_tag", sa.Text),
        sa.Column("room_id", pg.UUID(as_uuid=True), sa.ForeignKey("room.id")),
        sa.Column("rack_id", pg.UUID(as_uuid=True), sa.ForeignKey("rack.id")),
        sa.Column("u_start", sa.Integer),
        sa.Column("u_height", sa.Integer, nullable=False, server_default="1"),
        sa.Column("facing", sa.String(1)),
        sa.Column("floor_x", sa.Numeric(8, 2)),
        sa.Column("floor_y", sa.Numeric(8, 2)),
        sa.Column("primary_ip", pg.INET),
        sa.Column("mgmt_ip", pg.INET),
        sa.Column("admin_state", _e("admin_state_t"), nullable=False, server_default="enabled"),
        sa.Column("lifecycle", _e("lifecycle_t"), nullable=False, server_default="in_service"),
        sa.Column("commissioned_at", sa.DateTime(timezone=True)),
        sa.Column("decommissioned_at", sa.DateTime(timezone=True)),
        sa.Column("attributes", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("u_height >= 1", name="ck_device_u_height_positive"),
    )
    op.create_index("ix_device_device_type", "device", ["device_type"])
    op.create_index("ix_device_rack_id", "device", ["rack_id"])
    op.create_index("ix_device_room_id", "device", ["room_id"])
    op.create_index("ix_device_attributes", "device", ["attributes"],
                    postgresql_using="gin", postgresql_ops={"attributes": "jsonb_path_ops"})
    op.create_index("ix_device_name_trgm", "device", ["name"],
                    postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"})
    op.execute("""
        CREATE UNIQUE INDEX ix_device_mgmt_ip_live ON device (mgmt_ip)
        WHERE mgmt_ip IS NOT NULL AND lifecycle <> 'decommissioned'
    """)

    # The highest-value constraint in the schema: two devices can never claim
    # the same rack unit. Without it you find out months later.
    op.execute("""
        ALTER TABLE device ADD CONSTRAINT device_u_no_overlap
        EXCLUDE USING gist (
            rack_id WITH =,
            int4range(u_start, u_start + u_height, '[)') WITH &&
        ) WHERE (rack_id IS NOT NULL AND u_start IS NOT NULL)
    """)

    op.create_table(
        "interface",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("device_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        sa.Column("if_index", sa.Integer),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False, server_default="data"),
        sa.Column("speed_bps", sa.BigInteger),
        sa.Column("mac", pg.MACADDR),
        sa.Column("ip", pg.INET),
        sa.Column("admin_state", _e("admin_state_t"), nullable=False, server_default="enabled"),
        sa.Column("attributes", pg.JSONB, nullable=False, server_default="{}"),
        sa.UniqueConstraint("device_id", "name", name="uq_interface_device_id"),
    )
    op.execute("""
        CREATE UNIQUE INDEX ix_interface_device_ifindex ON interface (device_id, if_index)
        WHERE if_index IS NOT NULL
    """)

    op.create_table(
        "outlet",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("device_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("connector", sa.Text, nullable=False, server_default="C13"),
        sa.Column("rated_amps", sa.Numeric(6, 2)),
        sa.Column("phase", sa.String(1)),
        sa.Column("branch", sa.Text),
        sa.UniqueConstraint("device_id", "number", name="uq_outlet_device_id"),
    )

    op.create_table(
        "power_supply",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("device_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("connector", sa.Text, nullable=False, server_default="C14"),
        sa.Column("rated_watts", sa.Integer),
        sa.UniqueConstraint("device_id", "number", name="uq_power_supply_device_id"),
    )

    # ---------------------------------------------------- connection graph
    op.create_table(
        "connection",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("layer", _e("layer_t"), nullable=False),
        sa.Column("link_type", sa.Text),
        sa.Column("a_device_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        sa.Column("a_termination_type", _e("termination_t"), nullable=False,
                  server_default="none"),
        sa.Column("a_termination_id", pg.UUID(as_uuid=True)),
        sa.Column("b_device_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        sa.Column("b_termination_type", _e("termination_t"), nullable=False,
                  server_default="none"),
        sa.Column("b_termination_id", pg.UUID(as_uuid=True)),
        sa.Column("redundancy_side", sa.String(1)),
        sa.Column("admin_state", _e("admin_state_t"), nullable=False, server_default="enabled"),
        sa.Column("oper_state", sa.Text, nullable=False, server_default="unknown"),
        sa.Column("attributes", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_connection_layer_a", "connection", ["layer", "a_device_id"])
    op.create_index("ix_connection_layer_b", "connection", ["layer", "b_device_id"])
    # One port takes one cable; one outlet takes one cord.
    op.execute("""
        CREATE UNIQUE INDEX uq_connection_a_termination
        ON connection (a_termination_type, a_termination_id)
        WHERE a_termination_type <> 'none'
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_connection_b_termination
        ON connection (b_termination_type, b_termination_id)
        WHERE b_termination_type <> 'none'
    """)

    # ---------------------------------------------- credentials + endpoints
    op.create_table(
        "credential",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("protocol", _e("protocol_t"), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("secret_enc", sa.LargeBinary, nullable=False),
        sa.Column("secret_hint", sa.Text),
        sa.Column("rotated_at", sa.Text),
    )

    op.create_table(
        "poll_profile",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("interval_s", sa.Integer, nullable=False, server_default="30"),
        sa.Column("timeout_ms", sa.Integer, nullable=False, server_default="3000"),
        sa.Column("retries", sa.Integer, nullable=False, server_default="2"),
        sa.Column("metric_groups", pg.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("push_enabled", sa.Boolean, nullable=False, server_default="false"),
    )

    op.create_table(
        "device_endpoint",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("device_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("device.id", ondelete="CASCADE"), nullable=False),
        sa.Column("protocol", _e("protocol_t"), nullable=False),
        sa.Column("role", _e("endpoint_role_t"), nullable=False),
        sa.Column("address", pg.INET),
        sa.Column("port", sa.Integer),
        sa.Column("addressing", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("via_endpoint_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("device_endpoint.id", ondelete="SET NULL")),
        sa.Column("credential_id", pg.UUID(as_uuid=True), sa.ForeignKey("credential.id")),
        sa.Column("poll_profile_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("poll_profile.id"), nullable=False),
        sa.Column("collector_id", sa.Text),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("admin_state", _e("admin_state_t"), nullable=False, server_default="enabled"),
        sa.Column("attributes", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_device_endpoint_device", "device_endpoint", ["device_id"])
    op.create_index("ix_device_endpoint_address", "device_endpoint", ["address"])
    op.create_index("ix_device_endpoint_via", "device_endpoint", ["via_endpoint_id"])
    op.execute("""
        CREATE INDEX ix_device_endpoint_collector ON device_endpoint (collector_id)
        WHERE enabled
    """)
    # One endpoint per (device, protocol, role, address). Re-import is then an
    # upsert rather than a duplicate factory.
    op.execute("""
        CREATE UNIQUE INDEX uq_device_endpoint_identity
        ON device_endpoint (device_id, protocol, role, coalesce(host(address), ''))
    """)

    # ----------------------------------------------------------- state
    op.create_table(
        "endpoint_state",
        sa.Column("endpoint_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("device_endpoint.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("status", _e("comm_status_t"), nullable=False, server_default="UNKNOWN"),
        sa.Column("last_seen", sa.DateTime(timezone=True)),
        sa.Column("last_success", sa.DateTime(timezone=True)),
        sa.Column("last_failure", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column("last_error_class", sa.Text),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("poll_count", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("fail_count", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("timeout_count", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("auth_fail_count", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("last_latency_ms", sa.Integer),
        sa.Column("collector_id", sa.Text),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "device_state",
        sa.Column("device_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("device.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("status", _e("comm_status_t"), nullable=False, server_default="UNKNOWN"),
        sa.Column("health", _e("health_t"), nullable=False, server_default="UNKNOWN"),
        sa.Column("max_severity", _e("severity_t"), nullable=False, server_default="CLEAR"),
        sa.Column("active_alarms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_seen", sa.DateTime(timezone=True)),
        sa.Column("power_w", sa.Numeric(12, 2)),
        sa.Column("inlet_temp_c", sa.Numeric(6, 2)),
        sa.Column("cpu_util_pct", sa.Numeric(5, 2)),
        sa.Column("humidity_pct", sa.Numeric(5, 2)),
        sa.Column("metrics", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_device_state_status", "device_state", ["status"])
    op.execute("""
        CREATE INDEX ix_device_state_severity ON device_state (max_severity)
        WHERE max_severity <> 'CLEAR'
    """)

    op.create_table(
        "collector_instance",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("version", sa.Text),
        sa.Column("hostname", sa.Text),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=False),
        sa.Column("endpoints_owned", sa.Integer, nullable=False, server_default="0"),
        sa.Column("endpoints_online", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.Text, nullable=False, server_default="UNKNOWN"),
        sa.Column("stats", pg.JSONB, nullable=False, server_default="{}"),
    )

    op.create_table(
        "metric",
        sa.Column("id", sa.SmallInteger, primary_key=True, autoincrement=True),
        sa.Column("key", sa.Text, nullable=False, unique=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("unit", sa.Text, nullable=False),
        sa.Column("value_type", sa.Text, nullable=False),
        sa.Column("aggregation", sa.Text, nullable=False, server_default="avg"),
        sa.Column("min_valid", sa.Numeric),
        sa.Column("max_valid", sa.Numeric),
        sa.Column("stale_after_s", sa.Integer, nullable=False, server_default="300"),
        sa.Column("is_hot", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("deprecated_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    for t in ("metric", "collector_instance", "device_state", "endpoint_state",
              "device_endpoint", "poll_profile", "credential", "connection",
              "power_supply", "outlet", "interface", "device", "model",
              "device_type", "vendor", "rack", "rack_row", "room", "datacenter"):
        op.drop_table(t)
    for name in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")
