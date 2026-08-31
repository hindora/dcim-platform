"""A recovery trap resolves an alarm. It does not raise one.

Found on a live console. A PDU showed two rows side by side:

    Outlet Off   MAJOR   08:37:41
    Outlet On    INFO    08:49:32

The fault that had ended, and beside it the news that it had ended, filed as a
second thing to look at. The relay had closed twelve minutes earlier and the
platform still carried the outage.

The mechanism is the mapping. A trap marked `is_clear` names the event types it
resolves, and a clear reaches the same alarm key as its raise. A recovery
without that marking matches no open alarm, so the ingest path does the only
other thing it can: files it as new. The generator's own comment says exactly
this - "a missing entry means a clear that raises a brand new alarm instead" -
and it had been true of `outletOn` and `serverPowerOn` all along.

These tests read the generated mapping rather than the generator, because the
mapping is what the collector loads.
"""

from __future__ import annotations

import pathlib
import re

import pytest

MAPPING = (pathlib.Path(__file__).resolve().parents[2]
           / "contracts" / "mappings" / "snmp" / "traps.yaml")

#: Names that read like a recovery. Not a heuristic the code relies on - it is
#: the net this test casts, and anything caught in it either clears something
#: or is named below as a deliberate exception.
RECOVERY_SUFFIXES = ("_on", "_up", "_normal", "_restored", "_ok", "_established",
                     "_cleared", "_closed")

#: Recovery-shaped events that genuinely are their own condition.
#:
#: A restart is the case that matters: a device that has just rebooted is news,
#: and treating coldStart as a clear would silently resolve alarms nobody fixed.
NOT_CLEARS = {
    "device_restarted",
}


def entries() -> list[dict]:
    """Every trap block in the mapping, as {event_type, is_clear, oid}."""
    out = []
    for block in MAPPING.read_text(encoding="utf-8").split("\n  - oid: ")[1:]:
        event = re.search(r"event_type: (\S+)", block)
        if not event:
            continue
        out.append({"oid": block.split("\n", 1)[0].strip(),
                    "event_type": event.group(1),
                    "is_clear": "is_clear: true" in block,
                    "clears": re.search(r"clears: \[([^\]]*)\]", block)})
    return out


def test_the_mapping_is_there_to_read():
    assert MAPPING.exists(), f"no mapping at {MAPPING}"
    assert len(entries()) > 100


def test_every_recovery_shaped_trap_resolves_something():
    """The bug, generalised.

    `outlet_on` and `server_power_on` were the two that slipped through. This
    catches the next one at generation time rather than on somebody's console.
    """
    rows = entries()
    clearing = {e["event_type"] for e in rows if e["is_clear"]}
    stranded = sorted({
        e["event_type"] for e in rows
        if not e["is_clear"]
        and e["event_type"].endswith(RECOVERY_SUFFIXES)
        and e["event_type"] not in clearing
        and e["event_type"] not in NOT_CLEARS
    })
    assert not stranded, (
        f"these read as recoveries and clear nothing: {stranded}. A recovery "
        f"that resolves no alarm is filed as a new one, which puts the good "
        f"news on the console beside the fault it ended.")


def test_the_outlet_pair_is_wired():
    """The exact pair that was found broken."""
    clears_outlet = [e for e in entries()
                     if e["is_clear"] and e["clears"]
                     and "outlet_off" in e["clears"].group(1)]
    assert clears_outlet, "nothing clears outlet_off"


def test_a_clear_always_names_what_it_clears():
    """`is_clear` without `clears` would resolve nothing at all - the same
    outcome as no marking, arrived at more elaborately."""
    for e in entries():
        if e["is_clear"]:
            assert e["clears"] and e["clears"].group(1).strip(), (
                f"{e['event_type']} at {e['oid']} is marked a clear but names "
                f"no event type to resolve")


@pytest.mark.parametrize("event", ["link_up", "cpu_normal_trap", "outlet_off"])
def test_known_recoveries_stayed_wired(event):
    """A regression net around the pairs that already worked, so a change to
    the table cannot quietly drop one while adding another."""
    rows = entries()
    cleared = {c.strip() for e in rows if e["is_clear"] and e["clears"]
               for c in e["clears"].group(1).split(",")}
    raised = {e["event_type"] for e in rows if not e["is_clear"]}
    assert cleared, "the mapping resolves nothing at all"
    # Either this event is raised and cleared, or it is not in the plane.
    if event in raised:
        assert event in cleared, f"{event} can be raised but never cleared"
