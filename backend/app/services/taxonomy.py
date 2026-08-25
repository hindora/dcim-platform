"""The catalogue behind the Alarm status window.

The legend used to show eight category definitions and three example alarm
types each. That answers "what would a cooling alarm be" and not "what can this
platform actually raise, and which of those will ring". This assembles the
whole list, from the same places the raising code reads:

* `alarm_rule` - what fires off telemetry, with its severity and whether it is
  enabled. The database, not a transcription of it: a rule somebody disables
  should drop out of the legend on the next page load.
* the rules' `instances` - the plant's own fault points, which share one metric
  (`alarm_state`) and carry the point name as the instance.
* `PLATFORM_ALARM_TYPES` - what the platform raises about its own blindness.
* the classifier's `BY_ALARM_TYPE` - everything else it knows how to file,
  which is the traps and Redfish events the plane reports on its own.

Severity is stated only where it is genuinely fixed. A trap arrives with the
severity the device chose and a platform alarm escalates with how bad it is, so
those say "varies" rather than inventing a number - and because the alarm/alert
split follows severity, their class varies with it too.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.alarms.platform import PLATFORM_ALARM_TYPES
from app.core import alert_taxonomy as tax
from app.repositories import alarms as repo

#: Where a condition comes from, and therefore what the reader can do about it.
RULE = "rule"            # our threshold or state rule, tunable here
EQUIPMENT = "equipment"  # the plant asserts its own fault point
REPORTED = "reported"    # a trap or a Redfish event from the device
PLATFORM = "platform"    # we raised it about our own ability to see
PLANNED = "planned"      # the name is reserved; no detector raises it yet

ORIGINS: dict[str, dict[str, str]] = {
    RULE: {
        "label": "Evaluated here",
        "text": "A rule this platform runs over incoming telemetry. Severity "
                "and threshold are ours, and can be changed.",
    },
    EQUIPMENT: {
        "label": "Reported by the plant",
        "text": "A binary fault point the equipment asserts itself over BACnet "
                "or Modbus. We do not decide that it is true - only how loudly "
                "to say so.",
    },
    REPORTED: {
        "label": "Reported by the device",
        "text": "An SNMP trap or a Redfish event. It arrives with the severity "
                "the device chose, so its class follows that.",
    },
    PLATFORM: {
        "label": "About the monitoring",
        "text": "Raised by this platform about itself - the collector, the "
                "ingest pipeline, the database. Equipment may be perfectly "
                "healthy; what has failed is our view of it.",
    },
    PLANNED: {
        "label": "Not built yet",
        "text": "The name and the counter exist; no detector fills them. Said "
                "out loud because a category reading zero because nothing "
                "watches it is a different fact from one reading zero because "
                "the estate is well.",
    },
}


#: Words the industry writes in capitals. Sentence-casing them makes a list
#: harder to search, not easier to read - an operator scans for "UPS", not for
#: "Ups".
ACRONYMS = frozenset({
    "ups", "pdu", "rpp", "ats", "cpu", "psu", "pue", "thd", "chw", "crah",
    "cdu", "bgp", "bms", "it", "db",
})


def _humanise(key: str) -> str:
    """`Alarm_HighSupplyTemp` -> `High supply temp`, `cpu_temp_high` -> `Cpu temp high`.

    Plant point names are CamelCase behind an `Alarm_` prefix that every one of
    them carries, so keeping either would make a list of thirty-six points read
    as thirty-six variations on the word "alarm".
    """
    if key.startswith(("Alarm_", "alarm_")) and len(key) > 6:
        key = key[6:]
    parts = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key).replace("_", " ").split()
    if not parts:
        return key
    # Sentence case, except for acronyms: THD, CHW and PUE are how the plant
    # labels them, and lowercasing those would make the list harder to search,
    # not easier to read.
    def one(word: str, first: bool) -> str:
        if word.isupper() or word.lower() in ACRONYMS:
            return word.upper()
        return word.capitalize() if first else word.lower()

    return " ".join(one(w, i == 0) for i, w in enumerate(parts))


async def catalogue(session: AsyncSession) -> dict[str, Any]:
    """Every condition this platform can raise, with its class where it is fixed."""
    rules = await repo.list_rules(session)

    conditions: list[dict[str, Any]] = []
    covered: set[str] = set()

    for r in rules:
        severity = (r["severity"] or "").upper()
        cls = tax.response_class_for(severity, rule_class=r.get("response_class"))
        instances = list(r.get("instances") or ())

        if instances:
            # One rule, many points: severity is assigned per point, so the
            # points are what an operator recognises - not the rule's name.
            for point in sorted(instances):
                conditions.append({
                    "key": point,
                    "label": _humanise(point),
                    "category": None,   # depends on the equipment reporting it
                    "origin": EQUIPMENT,
                    "severity": severity,
                    "response_class": cls,
                    "enabled": bool(r["enabled"]),
                    "detail": "asserted by the equipment",
                })
            covered.add(r["alarm_type"])
            continue

        detail = r["metric_key"] or ""
        if r["metric_key"] and r["operator"] and r["threshold"] is not None:
            detail = f"{r['metric_key']} {r['operator']} {r['threshold']:g}"
        category = tax.classify(r["alarm_type"], metric_key=r["metric_key"])
        conditions.append({
            "key": r["alarm_type"],
            "label": _humanise(r["alarm_type"]),
            # `uncategorised` here does not mean nobody classified it - it means
            # the metric means different things on different equipment, and the
            # category is decided per alarm from the device's role. Null says
            # that; the word would say the classifier has a gap.
            "category": None if category == tax.UNCATEGORISED else category,
            "origin": RULE,
            "severity": severity,
            "response_class": cls,
            "enabled": bool(r["enabled"]),
            "detail": detail or "no threshold - a state, not a number",
        })
        covered.add(r["alarm_type"])

    for alarm_type in PLATFORM_ALARM_TYPES:
        if alarm_type in covered:
            continue
        conditions.append({
            "key": alarm_type,
            "label": _humanise(alarm_type),
            "category": tax.classify(alarm_type),
            "origin": PLATFORM,
            # These escalate: ingest lag is a WARNING until the pipeline stops,
            # and then it is CRITICAL. Naming one severity here would be wrong
            # half the time.
            "severity": None,
            "response_class": None,
            "enabled": True,
            "detail": "severity rises with how bad it is",
        })
        covered.add(alarm_type)

    for alarm_type, category in tax.BY_ALARM_TYPE.items():
        if alarm_type in covered:
            continue
        reserved = alarm_type in tax.RESERVED
        conditions.append({
            "key": alarm_type,
            "label": _humanise(alarm_type),
            "category": category,
            "origin": PLANNED if reserved else REPORTED,
            "severity": None,
            "response_class": None,
            "enabled": not reserved,
            "detail": ("no detector raises this yet" if reserved
                       else "as the device reports it"),
        })

    # Reading order, not alphabetical order. Down the failure chain the way an
    # operator thinks - supply, then heat, then what is being powered and
    # cooled, then whether we can see any of it - and within a category the
    # things this platform evaluates before the things it merely relays.
    # Sorting by name put five unimplemented capacity rows at the top of the
    # list, which is the worst possible first impression of a catalogue.
    category_rank = {c: i for i, c in enumerate(
        ("power", "cooling", "environmental", "it_equipment", "network",
         "visibility", "capacity", "uncategorised"))}
    origin_rank = {RULE: 0, EQUIPMENT: 1, REPORTED: 2, PLATFORM: 3, PLANNED: 4}

    conditions.sort(key=lambda c: (
        origin_rank.get(c["origin"], 9) if c["origin"] == PLANNED else 0,
        category_rank.get(c["category"], 98) if c["category"] else 99,
        origin_rank.get(c["origin"], 9),
        c["key"].lower(),
    ))

    fixed = [c for c in conditions if c["response_class"]]
    return {
        "conditions": conditions,
        "origins": [{"key": k, **v} for k, v in ORIGINS.items()],
        "summary": {
            "total": len(conditions),
            # Only the ones whose class is fixed can be counted this way; the
            # rest follow a severity that is not decided until they arrive.
            "alarm": sum(1 for c in fixed if c["response_class"] == tax.ALARM),
            "alert": sum(1 for c in fixed if c["response_class"] == tax.ALERT),
            "varies": len(conditions) - len(fixed),
            "disabled": sum(1 for c in conditions
                            if not c["enabled"] and c["origin"] != PLANNED),
            "planned": sum(1 for c in conditions if c["origin"] == PLANNED),
        },
    }
