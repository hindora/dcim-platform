"""How a power supply is named to a person.

One place, because two did it differently: the Power page's Feeds cell said
PSU1 while the alarm for the same supply said "Power supply 0".
"""

from __future__ import annotations


def psu_label(instance: str | None) -> str:
    """A supply as the faceplate names it. Redfish MemberIds on this fleet are
    0-based array indices ("0", "1") while the chassis silk-screen, the BMC's
    own Name and the technician all say PSU1 / PSU2, so a numeric id is shown
    one up. A vendor id that is already a name ("PSU.Slot.1") is kept."""
    if instance is None or instance == "":
        return "PSU?"
    if instance.isdigit():
        return f"PSU{int(instance) + 1}"
    return instance
