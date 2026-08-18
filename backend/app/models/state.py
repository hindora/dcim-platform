"""Current operational state: small, hot, mutable, and kept apart from history.

Nothing here is ever queried by scanning a hypertable. "Is this device up?" must
be one indexed row read, not an ORDER BY over time-series data.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Enum, ForeignKey, Index, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import CommStatus, Health, Severity


def _enum(py_enum, name: str) -> Enum:
    return Enum(py_enum, name=name, values_callable=lambda e: [m.value for m in e])


class EndpointState(Base):
    __tablename__ = "endpoint_state"

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device_endpoint.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[CommStatus] = mapped_column(
        _enum(CommStatus, "comm_status_t"), nullable=False, default=CommStatus.UNKNOWN)
    last_seen: Mapped[datetime | None] = mapped_column()
    last_success: Mapped[datetime | None] = mapped_column()
    last_failure: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_class: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    poll_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    fail_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    timeout_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    auth_fail_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer)
    collector_id: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column()


class DeviceState(Base):
    """Derived state. `status` comes from the endpoints, `health` from alarms.

    ``metrics`` holds only registry metrics flagged ``hot``. It exists so the
    rack view renders 42 devices in one query instead of 250 hypertable reads,
    and it is capped by that flag rather than growing without limit.
    """

    __tablename__ = "device_state"
    __table_args__ = (
        Index("ix_device_state_status", "status"),
        Index("ix_device_state_severity", "max_severity",
              postgresql_where="max_severity <> 'CLEAR'"),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[CommStatus] = mapped_column(
        _enum(CommStatus, "comm_status_t"), nullable=False, default=CommStatus.UNKNOWN)
    health: Mapped[Health] = mapped_column(
        _enum(Health, "health_t"), nullable=False, default=Health.UNKNOWN)
    max_severity: Mapped[Severity] = mapped_column(
        _enum(Severity, "severity_t"), nullable=False, default=Severity.CLEAR)
    active_alarms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_seen: Mapped[datetime | None] = mapped_column()

    power_w: Mapped[float | None] = mapped_column(Numeric(12, 2))
    inlet_temp_c: Mapped[float | None] = mapped_column(Numeric(6, 2))
    cpu_util_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    humidity_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))

    # {"metric_key": {"v": <value>, "t": "<iso8601>", "q": "good"}}
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime | None] = mapped_column()


class CollectorInstance(Base):
    __tablename__ = "collector_instance"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str | None] = mapped_column(Text)
    hostname: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column()
    last_heartbeat: Mapped[datetime] = mapped_column(nullable=False)
    endpoints_owned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    endpoints_online: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="UNKNOWN")
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class Metric(Base):
    """Metric dimension. Loaded from contracts/metrics/registry.yaml.

    Hypertables store ``metric_id smallint``, never the text key - a text label
    per row inflates the table and its indexes for no benefit. A metric removed
    from the registry is marked deprecated, never deleted, because history rows
    still reference the id.
    """

    __tablename__ = "metric"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    value_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregation: Mapped[str] = mapped_column(Text, nullable=False, default="avg")
    min_valid: Mapped[float | None] = mapped_column(Numeric)
    max_valid: Mapped[float | None] = mapped_column(Numeric)
    stale_after_s: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    is_hot: Mapped[bool] = mapped_column(nullable=False, default=False)
    deprecated_at: Mapped[datetime | None] = mapped_column()
