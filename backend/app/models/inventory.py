"""Physical hierarchy, catalog, devices and the layered connection graph."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, MACADDR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import AdminState, Layer, Lifecycle, TerminationType


def _enum(py_enum, name: str) -> Enum:
    return Enum(py_enum, name=name, values_callable=lambda e: [m.value for m in e])


# ---------------------------------------------------------------- hierarchy

class Datacenter(Base, TimestampMixin):
    __tablename__ = "datacenter"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="UTC")
    design_it_kw: Mapped[float | None] = mapped_column(Numeric(10, 2))
    design_pue: Mapped[float | None] = mapped_column(Numeric(4, 3))
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    rooms: Mapped[list[Room]] = relationship(back_populates="datacenter",
                                             cascade="all, delete-orphan")


class Room(Base):
    __tablename__ = "room"
    __table_args__ = (UniqueConstraint("datacenter_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    datacenter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("datacenter.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    floor: Mapped[str | None] = mapped_column(Text)
    room_type: Mapped[str] = mapped_column(Text, nullable=False, default="data_hall")
    width_m: Mapped[float | None] = mapped_column(Numeric(8, 2))
    depth_m: Mapped[float | None] = mapped_column(Numeric(8, 2))
    design_it_kw: Mapped[float | None] = mapped_column(Numeric(10, 2))
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    datacenter: Mapped[Datacenter] = relationship(back_populates="rooms")
    rows: Mapped[list[Row]] = relationship(back_populates="room",
                                           cascade="all, delete-orphan")


class Row(Base):
    # Named `rack_row` rather than `row`: `row` is a reserved word in SQL and
    # every hand-written query would need quoting.
    __tablename__ = "rack_row"
    __table_args__ = (UniqueConstraint("room_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    room_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("room.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cold_aisle: Mapped[str | None] = mapped_column(Text)
    hot_aisle: Mapped[str | None] = mapped_column(Text)

    room: Mapped[Room] = relationship(back_populates="rows")
    racks: Mapped[list[Rack]] = relationship(back_populates="row",
                                             cascade="all, delete-orphan")


class Rack(Base):
    __tablename__ = "rack"
    __table_args__ = (UniqueConstraint("row_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    row_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rack_row.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    u_height: Mapped[int] = mapped_column(Integer, nullable=False, default=42)
    facing: Mapped[str | None] = mapped_column(String(1))
    floor_x: Mapped[float | None] = mapped_column(Numeric(8, 2))
    floor_y: Mapped[float | None] = mapped_column(Numeric(8, 2))
    rated_power_kw: Mapped[float | None] = mapped_column(Numeric(8, 2))
    rated_cool_kw: Mapped[float | None] = mapped_column(Numeric(8, 2))
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    row: Mapped[Row] = relationship(back_populates="racks")
    devices: Mapped[list[Device]] = relationship(back_populates="rack")


# ------------------------------------------------------------------ catalog

class Vendor(Base):
    __tablename__ = "vendor"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # Used by the trap mapping: the wire OID of a notification is rooted in the
    # vendor's enterprise tree, not in ours.
    enterprise_oid: Mapped[str | None] = mapped_column(Text)


class DeviceType(Base):
    __tablename__ = "device_type"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    is_rack_mounted: Mapped[bool] = mapped_column(nullable=False, default=True)
    icon: Mapped[str | None] = mapped_column(Text)


class Model(Base):
    __tablename__ = "model"
    __table_args__ = (UniqueConstraint("vendor_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendor.id"), nullable=False)
    device_type: Mapped[str] = mapped_column(ForeignKey("device_type.code"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    u_height: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rated_power_w: Mapped[int | None] = mapped_column(Integer)
    rated_capacity: Mapped[float | None] = mapped_column(Numeric(12, 2))
    capacity_unit: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


# ------------------------------------------------------------------- device

class Device(Base, TimestampMixin):
    __tablename__ = "device"
    __table_args__ = (
        Index("ix_device_device_type", "device_type"),
        Index("ix_device_rack_id", "rack_id"),
        Index("ix_device_room_id", "room_id"),
        Index("ix_device_mgmt_ip_live", "mgmt_ip", unique=True,
              postgresql_where="mgmt_ip IS NOT NULL AND lifecycle <> 'decommissioned'"),
        CheckConstraint("u_height >= 1", name="u_height_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    # The upstream system's id. Seed re-import is idempotent on this column.
    external_id: Mapped[str | None] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    device_type: Mapped[str] = mapped_column(ForeignKey("device_type.code"), nullable=False)
    model_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model.id"))
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vendor.id"))
    serial_number: Mapped[str | None] = mapped_column(Text)
    asset_tag: Mapped[str | None] = mapped_column(Text)

    # Placement. rack_id is NULL for floor-standing plant; room_id then carries it.
    room_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("room.id"))
    rack_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("rack.id"))
    u_start: Mapped[int | None] = mapped_column(Integer)
    u_height: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    facing: Mapped[str | None] = mapped_column(String(1))
    floor_x: Mapped[float | None] = mapped_column(Numeric(8, 2))
    floor_y: Mapped[float | None] = mapped_column(Numeric(8, 2))

    primary_ip: Mapped[str | None] = mapped_column(INET)
    mgmt_ip: Mapped[str | None] = mapped_column(INET)

    admin_state: Mapped[AdminState] = mapped_column(
        _enum(AdminState, "admin_state_t"), nullable=False, default=AdminState.ENABLED)
    lifecycle: Mapped[Lifecycle] = mapped_column(
        _enum(Lifecycle, "lifecycle_t"), nullable=False, default=Lifecycle.IN_SERVICE)
    commissioned_at: Mapped[datetime | None] = mapped_column()
    decommissioned_at: Mapped[datetime | None] = mapped_column()
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    rack: Mapped[Rack | None] = relationship(back_populates="devices")
    interfaces: Mapped[list[Interface]] = relationship(
        back_populates="device", cascade="all, delete-orphan")
    outlets: Mapped[list[Outlet]] = relationship(
        back_populates="device", cascade="all, delete-orphan")
    psus: Mapped[list[PowerSupply]] = relationship(
        back_populates="device", cascade="all, delete-orphan")


class Interface(Base):
    __tablename__ = "interface"
    __table_args__ = (
        UniqueConstraint("device_id", "name"),
        Index("ix_interface_device_ifindex", "device_id", "if_index", unique=True,
              postgresql_where="if_index IS NOT NULL"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), nullable=False)
    if_index: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="data")
    speed_bps: Mapped[int | None] = mapped_column()
    mac: Mapped[str | None] = mapped_column(MACADDR)
    ip: Mapped[str | None] = mapped_column(INET)
    admin_state: Mapped[AdminState] = mapped_column(
        _enum(AdminState, "admin_state_t"), nullable=False, default=AdminState.ENABLED)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    device: Mapped[Device] = relationship(back_populates="interfaces")


class Outlet(Base):
    __tablename__ = "outlet"
    __table_args__ = (UniqueConstraint("device_id", "number"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    connector: Mapped[str] = mapped_column(Text, nullable=False, default="C13")
    rated_amps: Mapped[float | None] = mapped_column(Numeric(6, 2))
    phase: Mapped[str | None] = mapped_column(String(1))
    branch: Mapped[str | None] = mapped_column(Text)

    device: Mapped[Device] = relationship(back_populates="outlets")


class PowerSupply(Base):
    __tablename__ = "power_supply"
    __table_args__ = (UniqueConstraint("device_id", "number"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    connector: Mapped[str] = mapped_column(Text, nullable=False, default="C14")
    rated_watts: Mapped[int | None] = mapped_column(Integer)

    device: Mapped[Device] = relationship(back_populates="psus")


# ------------------------------------------------------- connection graph

class Connection(Base):
    """One layered graph, not three tables.

    Terminations are polymorphic because a power cord lands on an OUTLET and a
    PSU, not on interfaces: ``a_termination_id`` therefore has no foreign key
    and is validated in the repository layer. Direction is A = source/upstream,
    B = load/downstream, and is meaningful on power, cooling and fieldbus.
    """

    __tablename__ = "connection"
    __table_args__ = (
        Index("ix_connection_layer_a", "layer", "a_device_id"),
        Index("ix_connection_layer_b", "layer", "b_device_id"),
        # One port takes one cable; one outlet takes one cord.
        Index("uq_connection_a_termination", "a_termination_type", "a_termination_id",
              unique=True, postgresql_where="a_termination_type <> 'none'"),
        Index("uq_connection_b_termination", "b_termination_type", "b_termination_id",
              unique=True, postgresql_where="b_termination_type <> 'none'"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    layer: Mapped[Layer] = mapped_column(_enum(Layer, "layer_t"), nullable=False)
    link_type: Mapped[str | None] = mapped_column(Text)

    a_device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), nullable=False)
    a_termination_type: Mapped[TerminationType] = mapped_column(
        _enum(TerminationType, "termination_t"), nullable=False,
        default=TerminationType.NONE)
    a_termination_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    b_device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), nullable=False)
    b_termination_type: Mapped[TerminationType] = mapped_column(
        _enum(TerminationType, "termination_t"), nullable=False,
        default=TerminationType.NONE)
    b_termination_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # Without this you cannot answer the only question that matters during an
    # event: "is this load still fed from the other side?"
    redundancy_side: Mapped[str | None] = mapped_column(String(1))
    admin_state: Mapped[AdminState] = mapped_column(
        _enum(AdminState, "admin_state_t"), nullable=False, default=AdminState.ENABLED)
    oper_state: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
