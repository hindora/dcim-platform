"""Enumerations shared by the ORM, the schemas and the ingest pipeline.

These are PostgreSQL native enums. `severity_t` is declared in precedence order
because Postgres orders enums by declaration and the rack/room roll-ups use
``MAX(severity)``.
"""

from __future__ import annotations

from enum import StrEnum


class Protocol(StrEnum):
    SNMP = "snmp"
    SNMP_TRAP = "snmp_trap"
    GNMI = "gnmi"
    BACNET = "bacnet"
    REDFISH = "redfish"
    MODBUS = "modbus"
    SFLOW = "sflow"
    MANUAL = "manual"


class EndpointRole(StrEnum):
    OS_AGENT = "os_agent"
    BMC = "bmc"
    NATIVE_CARD = "native_card"
    FIELD_DEVICE = "field_device"
    GATEWAY = "gateway"
    ROUTER = "router"


class CommStatus(StrEnum):
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"
    DISABLED = "DISABLED"


class Health(StrEnum):
    OK = "OK"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class Severity(StrEnum):
    CLEAR = "CLEAR"
    INFO = "INFO"
    WARNING = "WARNING"
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    CRITICAL = "CRITICAL"


class Layer(StrEnum):
    PRODUCTION = "production"
    MANAGEMENT = "management"
    POWER = "power"
    COOLING = "cooling"
    FIELDBUS = "fieldbus"


class TerminationType(StrEnum):
    INTERFACE = "interface"
    OUTLET = "outlet"
    PSU = "psu"
    NONE = "none"


class ValueType(StrEnum):
    GAUGE = "gauge"
    COUNTER = "counter"
    DELTA = "delta"
    BOOL = "bool"
    TEXT = "text"


class Quality(StrEnum):
    GOOD = "good"
    STALE = "stale"
    SUSPECT = "suspect"
    BAD = "bad"
    NO_DATA = "no_data"


class AdminState(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    MAINTENANCE = "maintenance"


class Lifecycle(StrEnum):
    PLANNED = "planned"
    IN_SERVICE = "in_service"
    MAINTENANCE = "maintenance"
    DECOMMISSIONED = "decommissioned"


# Declaration order == severity precedence. Do not reorder.
SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.CLEAR, Severity.INFO, Severity.WARNING,
    Severity.MINOR, Severity.MAJOR, Severity.CRITICAL,
)
