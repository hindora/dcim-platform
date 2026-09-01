"""A trap that names a sub-object has to be able to name it.

An alarm is keyed on `(device, alarm_type, instance)`. With no instance, every
outlet on a 42-way rack PDU folds into one row: two receptacles failing look
like one condition re-asserting itself, and the operator is left to walk the
cords. With a WRONG instance the row is worse than absent, because it points at
a receptacle that is fine.

The failure mode is the same one `test_trap_measurements` exists for. A varbind
lookup that misses does not raise - it leaves the field at its zero value, and
an empty instance is indistinguishable from a device-scoped condition that never
had one. So the rule is identical: a trap may only read varbinds its own vendor
could have sent.
"""

from __future__ import annotations

import pathlib
import re

MAPPING = (pathlib.Path(__file__).resolve().parents[2]
           / "contracts" / "mappings" / "snmp" / "traps.yaml")

#: Conditions that belong to one receptacle rather than to the strip.
OUTLET_EVENTS = {"outlet_on", "outlet_off", "outlet_failure",
                 "outlet_current_high"}


def entries() -> list[dict]:
    """Every mapping entry, with the fields this file reasons about."""
    out = []
    for block in MAPPING.read_text(encoding="utf-8").split("\n  - oid: ")[1:]:
        instance = re.search(r"instance_from_varbind: (\S+)", block)
        event = re.search(r"event_type: (\S+)", block)
        out.append({
            "oid": block.split("\n", 1)[0].strip(),
            "event_type": event.group(1) if event else "",
            "instance_from_varbind": instance.group(1) if instance else "",
            "vendor": (re.search(r"# \w+ \[(\w+)\]", block) or [None, ""])[1]
            if re.search(r"# \w+ \[(\w+)\]", block) else "",
        })
    return out


def enterprise(oid: str) -> str:
    """1.3.6.1.4.1.<enterprise> - the first seven arcs."""
    return ".".join(oid.split(".")[:7])


def test_an_instance_varbind_is_one_the_sender_could_carry():
    """Same rule as measurements, one field over.

    ifDescr is the exception and it is a real one: it lives in MIB-2, which
    every agent implements, so a link trap from any vendor resolves it.
    """
    stranded = [
        f"{e['oid']} ({e['event_type']}) reads {e['instance_from_varbind']}"
        for e in entries()
        if e["instance_from_varbind"]
        and not e["instance_from_varbind"].startswith("1.3.6.1.2.1.")
        and enterprise(e["oid"]) != enterprise(e["instance_from_varbind"])
    ]
    assert not stranded, (
        "these name an instance varbind they cannot resolve:\n  "
        + "\n  ".join(stranded))


def test_apc_outlet_conditions_name_the_outlet():
    """The feature, as a rule.

    APC publishes the outlet's name in its rPDU2 switched-status table, so an
    outlet condition from an APC strip can say which receptacle. If this ever
    goes empty, per-outlet alarms silently collapse back into one row per PDU.
    """
    named = [e for e in entries()
             if e["event_type"] in OUTLET_EVENTS
             and e["oid"].startswith("1.3.6.1.4.1.318.")
             and e["instance_from_varbind"]]
    assert named, "APC outlet conditions no longer carry an outlet identity"


def test_outlet_identity_is_not_claimed_where_it_cannot_be_delivered():
    """Raritan carries the outlet in the OID INDEX, not in a named column.

    Pointing its outlet traps at a name varbind it never sends would resolve to
    nothing and put an empty instance on every Raritan outlet alarm - which
    looks exactly like a mapping that works. Until the receiver can extract an
    instance from a varbind's index, those entries carry none, and that absence
    is deliberate rather than forgotten.
    """
    for e in entries():
        if (e["event_type"] in OUTLET_EVENTS
                and e["oid"].startswith("1.3.6.1.4.1.13742.")):
            assert not e["instance_from_varbind"], (
                f"{e['oid']} claims a Raritan outlet name varbind; PDU2-MIB "
                f"carries that identity in the sensor OID's index instead")
