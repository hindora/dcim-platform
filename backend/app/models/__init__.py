"""ORM models.

Importing this package registers every table on ``Base.metadata``, which is what
Alembic autogenerate and the test fixtures rely on. Keep the re-exports.
"""

from app.db.base import Base
from app.models.endpoints import Credential, DeviceEndpoint, PollProfile
from app.models.enums import (
    AdminState,
    CommStatus,
    EndpointRole,
    Health,
    Layer,
    Lifecycle,
    Protocol,
    Quality,
    Severity,
    TerminationType,
    ValueType,
)
from app.models.inventory import (
    Connection,
    Datacenter,
    Device,
    DeviceType,
    Interface,
    Model,
    Outlet,
    PowerSupply,
    Rack,
    Room,
    Row,
    Vendor,
)
from app.models.state import (
    CollectorInstance,
    DeviceState,
    EndpointState,
    Metric,
)

__all__ = [
    "AdminState",
    "Base",
    "CollectorInstance",
    "CommStatus",
    "Connection",
    "Credential",
    "Datacenter",
    "Device",
    "DeviceEndpoint",
    "DeviceState",
    "DeviceType",
    "EndpointRole",
    "EndpointState",
    "Health",
    "Interface",
    "Layer",
    "Lifecycle",
    "Metric",
    "Model",
    "Outlet",
    "PollProfile",
    "PowerSupply",
    "Protocol",
    "Quality",
    "Rack",
    "Room",
    "Row",
    "Severity",
    "TerminationType",
    "ValueType",
    "Vendor",
]
