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
    """Declared in the order `lifecycle_t` declares them, which is the order the
    state is read as a progression.

    All seven, not the original four. `in_stock`, `installed` and `retired` were
    added to the database by migration 0043 and to `TRANSITIONS` with it, but not
    here - so this enum described a type that had not existed for thirty
    migrations. Nothing broke only because every read of the column goes through
    raw SQL and comes back as a string: the first `select(Device)` anybody writes
    would have got `'in_stock' is not among the defined enum values` on a row
    that is perfectly valid.
    """

    PLANNED = "planned"
    IN_STOCK = "in_stock"
    INSTALLED = "installed"
    IN_SERVICE = "in_service"
    MAINTENANCE = "maintenance"
    DECOMMISSIONED = "decommissioned"
    RETIRED = "retired"

    @classmethod
    def pattern(cls) -> str:
        """The request-validation pattern, built from the states themselves.

        The two transition endpoints each carried this list spelled out by hand,
        which is how a state gets added to the database and then rejected at the
        API with a 422 that says nothing about why.
        """
        return "^(" + "|".join(m.value for m in cls) + ")$"


# Declaration order == severity precedence. Do not reorder.
SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.CLEAR, Severity.INFO, Severity.WARNING,
    Severity.MINOR, Severity.MAJOR, Severity.CRITICAL,
)
