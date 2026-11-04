"""What a finding is about, and what probably caused it.

Two questions an operator asks of every finding before reading the numbers:
is this cooling or power, and why is it happening. Both answers are built
from facts the platform already holds - the detector's subject, and the
cooling incident the correlator tied the finding to - never generated.

CATEGORY uses the alarm taxonomy's own keys (app.core.alert_taxonomy), so a
finding and an alarm about the same thing wear the same colour and word.
`environmental` is the air at the rack (an effect); `cooling` is the plant
that conditions it (a cause). A data-quality finding is `visibility`: the
reading cannot be trusted, which is not a fault in the rack.

CAUSE is structured, not prose: the client words it, and a resolved cause
reads differently from an open one ("cleared, waiting to settle").
"""

from __future__ import annotations

from typing import Any

#: Detector family (the id before the dash) -> taxonomy category.
_FAMILY: dict[str, str] = {
    "DQ": "visibility",
    "RM": "environmental", "TH": "environmental",
    "CR": "cooling", "CH": "cooling", "CD": "cooling", "CT": "cooling",
    "PU": "cooling", "VA": "cooling", "PL": "cooling",
    "PD": "power", "UP": "power", "GN": "power", "AT": "power",
    "SV": "it_equipment",
    "NW": "network",
}

#: Exceptions inside a family. SV-4 compares a server's power with its
#: workload: an electrical question about IT equipment, read on the power side.
_DETECTOR: dict[str, str] = {"SV-4": "power"}

#: The category of a device type, for a detector nobody has mapped yet.
_BY_TYPE: dict[str, str] = {
    "pdu": "power", "rpp": "power", "ups": "power", "mpp": "power", "mcc": "power",
    "switchgear": "power", "generator": "power", "ats": "power",
    "crah": "cooling", "cdu": "cooling", "chiller": "cooling", "pump": "cooling",
    "cooling_tower": "cooling", "valve": "cooling", "sensor": "environmental",
    "switch": "network", "router": "network", "firewall": "network",
}


def category_of(detector_id: str, layer: str | None = None,
                device_type: str | None = None) -> str:
    if detector_id in _DETECTOR:
        return _DETECTOR[detector_id]
    fam = detector_id.split("-", 1)[0]
    if fam in _FAMILY:
        return _FAMILY[fam]
    if layer == "L0":
        return "visibility"
    return _BY_TYPE.get(device_type or "", "it_equipment")


#: Incident tier -> the category of the CAUSE.
_TIER_CATEGORY = {"power": "power", "plant": "cooling", "loop": "cooling",
                  "unit": "cooling", "zone": "environmental", "server": "it_equipment",
                  "room": "environmental"}


def cause_of(row: dict[str, Any]) -> dict[str, Any]:
    """The likely cause of one finding, from the incident it belongs to.

    `kind`:
    - `incident`: a device rooted it - a stopped CRAH, a tripped feeder.
    - `room`: heat across the room with no cooling alarm behind it.
    - `none`: nothing upstream explains it; the subject itself is the lead.
    """
    if not row.get("inc_id"):
        return {"kind": "none"}
    ev = row.get("inc_evidence") or {}
    tier = row.get("inc_tier") or ""
    root = ev.get("root") or {}
    if tier == "room":
        return {"kind": "room", "category": "environmental", "incident_id": row["inc_id"],
                "state": row.get("inc_state"), "room_name": row.get("inc_room_name"),
                "opened_at": row.get("inc_opened_at"), "resolved_at": row.get("inc_resolved_at")}
    return {
        "kind": "incident",
        "category": _TIER_CATEGORY.get(tier, "cooling"),
        "incident_id": row["inc_id"],
        "state": row.get("inc_state"),
        "tier": tier,
        "root_device_id": row.get("inc_root_device_id"),
        "root_device_name": row.get("inc_root_device_name"),
        "root_alarm_type": root.get("alarm_type"),
        "peers": len(ev.get("peer_alarm_ids") or []),
        # Air side tied by room, not by a measured zone: say so, don't claim
        # this exact unit cools this exact rack (docs/33 Phase 2 status).
        "by_room": ev.get("air_side") == "room",
        "opened_at": row.get("inc_opened_at"),
        "resolved_at": row.get("inc_resolved_at"),
    }
