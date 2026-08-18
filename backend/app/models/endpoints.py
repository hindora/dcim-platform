"""Device endpoints, credentials and poll profiles.

One inventory device routinely has SEVERAL protocol endpoints - a server has an
OS SNMP agent on its production NIC, a BMC SNMP agent on its management IP, and
Redfish on that same BMC. Field devices (BACnet MS/TP actuators, Modbus RTU
slaves) have no IP at all and are reached THROUGH a parent, which is what
``via_endpoint_id`` expresses.

Collector assignment, credentials, poll interval and communication health are
properties of the ENDPOINT. Device health is derived from its endpoints.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, Index, Integer, LargeBinary, Text
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import AdminState, EndpointRole, Protocol


def _enum(py_enum, name: str) -> Enum:
    return Enum(py_enum, name=name, values_callable=lambda e: [m.value for m in e])


class Credential(Base):
    """Device credentials, encrypted at rest with AES-256-GCM.

    ``secret_enc`` is nonce || ciphertext || tag. The key comes from
    DCIM_CREDENTIAL_KEY and never touches the database. ``secret_hint`` is the
    only part any API may return.
    """

    __tablename__ = "credential"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    protocol: Mapped[Protocol] = mapped_column(_enum(Protocol, "protocol_t"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)   # snmp_v2c|snmp_v3|http_basic|none
    secret_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_hint: Mapped[str | None] = mapped_column(Text)
    rotated_at: Mapped[str | None] = mapped_column(Text)


class PollProfile(Base):
    __tablename__ = "poll_profile"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    interval_s: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=3000)
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    # Mapping profile names from contracts/mappings/*, e.g. ["system","interfaces"].
    metric_groups: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list)
    push_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DeviceEndpoint(Base, TimestampMixin):
    __tablename__ = "device_endpoint"
    __table_args__ = (
        Index("ix_device_endpoint_device", "device_id"),
        Index("ix_device_endpoint_collector", "collector_id",
              postgresql_where="enabled"),
        # Trap/event source resolution runs source-IP -> endpoint on every
        # inbound packet; this index is the fallback behind the Redis cache.
        Index("ix_device_endpoint_address", "address"),
        Index("ix_device_endpoint_via", "via_endpoint_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), nullable=False)
    protocol: Mapped[Protocol] = mapped_column(_enum(Protocol, "protocol_t"), nullable=False)
    role: Mapped[EndpointRole] = mapped_column(
        _enum(EndpointRole, "endpoint_role_t"), nullable=False)

    # NULL only for a pure sub-device addressed entirely through its parent.
    address: Mapped[str | None] = mapped_column(INET)
    port: Mapped[int | None] = mapped_column(Integer)

    # Protocol-specific addressing, e.g.
    #   snmp    {"community_ref": "cred"}
    #   gnmi    {"target": "10.51.11.25"}
    #   bacnet  {"instance": 2001} | {"network": 2001, "mac": 12}
    #   modbus  {"unit_id": 7}
    #   redfish {"base": "/redfish/v1", "verify_tls": false}
    addressing: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    via_endpoint_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("device_endpoint.id", ondelete="SET NULL"))
    credential_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("credential.id"))
    poll_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("poll_profile.id"), nullable=False)

    collector_id: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    admin_state: Mapped[AdminState] = mapped_column(
        _enum(AdminState, "admin_state_t"), nullable=False, default=AdminState.ENABLED)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    credential: Mapped[Credential | None] = relationship(lazy="joined")
    poll_profile: Mapped[PollProfile] = relationship(lazy="joined")
    via: Mapped[DeviceEndpoint | None] = relationship(remote_side=[id], lazy="selectin")
