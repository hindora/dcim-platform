"""A trap that claims a measurement has to be able to deliver one.

Seen on a console: an OOB switch at 93 C carrying

    Temperature Alert - 0 C, limit 0 C

Nobody measured zero. The mapping had told the receiver to read the reading
from `1.3.6.1.4.1.99999.2.3` - this plane's own synthetic varbind - on a trap
sent by a Dell BMC, which carries Dell objects and puts the number inside a
text string. The lookup missed, the value stayed at its Go zero, and zero is a
plausible temperature, so it reached the operator as a measurement.

34 of the 48 entries that declared a measurement had the same fault: every
vendor variant had been handed the synthetic varbind OIDs regardless of who
sends the trap. Only the ones whose varbinds live on the trap's own enterprise
tree could ever have resolved.
"""

from __future__ import annotations

import pathlib
import re

MAPPING = (pathlib.Path(__file__).resolve().parents[2]
           / "contracts" / "mappings" / "snmp" / "traps.yaml")


def measured() -> list[dict]:
    """Every entry that declares a metric, with its trap OID and varbinds."""
    out = []
    for block in MAPPING.read_text(encoding="utf-8").split("\n  - oid: ")[1:]:
        value = re.search(r"value_varbind: (\S+)", block)
        if not value:
            continue
        limit = re.search(r"threshold_varbind: (\S+)", block)
        out.append({
            "oid": block.split("\n", 1)[0].strip(),
            "metric": re.search(r"metric: (\S+)", block).group(1),
            "value_varbind": value.group(1),
            "threshold_varbind": limit.group(1) if limit else "",
        })
    return out


def enterprise(oid: str) -> str:
    """1.3.6.1.4.1.<enterprise> - the first seven arcs."""
    return ".".join(oid.split(".")[:7])


def test_a_trap_only_names_varbinds_it_could_carry():
    """The bug, as a rule.

    A vendor's trap definition can reference that vendor's objects. Pointing it
    at another tree's OIDs is a lookup that always misses, and a miss becomes a
    zero that reads like a measurement.
    """
    stranded = [
        f"{e['oid']} ({e['metric']}) reads {e['value_varbind']}"
        for e in measured()
        if enterprise(e["oid"]) != enterprise(e["value_varbind"])
    ]
    assert not stranded, (
        "these declare a measurement they cannot resolve:\n  "
        + "\n  ".join(stranded))


def test_the_threshold_comes_from_the_same_tree_too():
    for e in measured():
        if e["threshold_varbind"]:
            assert enterprise(e["oid"]) == enterprise(e["threshold_varbind"]), (
                f"{e['oid']} reads its limit from {e['threshold_varbind']}")


def test_the_measurements_that_survive_are_real_ones():
    """Not an empty set dressed as a pass.

    The synthetic traps carry their reading and limit in adjacent enterprise
    varbinds, and Cisco sends cpmCPUTotal5minRev beside its rising threshold.
    Those pair correctly and must keep working.
    """
    rows = measured()
    assert len(rows) >= 10, "the measurement path has been emptied, not fixed"
    trees = {enterprise(e["oid"]) for e in rows}
    assert "1.3.6.1.4.1.99999" in trees, "the synthetic traps lost their numbers"


def test_every_declared_metric_names_a_value():
    """`metric:` without `value_varbind:` is the same failure with fewer steps."""
    text = MAPPING.read_text(encoding="utf-8")
    for block in text.split("\n  - oid: ")[1:]:
        if re.search(r"^\s+metric: ", block, re.M):
            assert "value_varbind:" in block, (
                f"{block.splitlines()[0]} declares a metric and no reading")
