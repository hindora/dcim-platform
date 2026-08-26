"""An unmapped trap is a gap in the mapping, not a condition on the equipment.

The collector never drops a trap it cannot resolve: it emits an INFO event
carrying the raw OID, which is how the gap becomes visible instead of
disappearing into a counter. That part is right.

Raising an ALARM from it was not. There is no rule behind an unmapped OID and
no recovery trap that names it, so the row can never clear - and every unmapped
vendor trap made another one. Measured here: a CPU recovery arrived as
1.3.6.1.4.1.99999.1.37, matched nothing, and left a permanent INFO alarm on a
firewall that had just recovered.

The trap mapping itself is regenerated from the transmit path now, so that
particular OID resolves - but the guard has to stand on its own. A platform
that only behaves when its mapping is complete has the failure built in.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.alarms.service import AlarmService


class _Session:
    """Never reached: an unmapped trap must return before touching the store."""

    async def execute(self, *_a, **_kw):
        raise AssertionError("an unmapped trap must not query the alarm store")


def _event(event_type: str, **over) -> dict:
    ev = {
        "device_id": "11111111-1111-1111-1111-111111111111",
        "event_type": event_type,
        "instance": "",
        "severity": "INFO",
        "is_clear": False,
        "clears": [],
        "message": "unmapped trap 1.3.6.1.4.1.99999.1.37",
        "observed_at": datetime.now(UTC),
        "source": "snmp_trap",
    }
    ev.update(over)
    return ev


@pytest.mark.asyncio
async def test_an_unmapped_trap_raises_no_alarm():
    service = AlarmService.__new__(AlarmService)
    assert await service.handle_event(_Session(), _event("unknown_trap")) is None


@pytest.mark.asyncio
async def test_an_unattributable_trap_still_raises_nothing():
    """No device, no alarm - and no crash on the way to deciding that."""
    service = AlarmService.__new__(AlarmService)
    ev = _event("unknown_trap", device_id=None)
    assert await service.handle_event(_Session(), ev) is None


def test_the_collector_records_the_raw_oid_rather_than_dropping_it():
    """The other half of the contract, pinned where it is easy to break.

    If the collector ever starts dropping unknown traps instead, this guard
    becomes silence: no alarm AND no event, which is how a mapping gap stops
    being discoverable at all.
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "collector" / \
        "internal" / "adapters" / "snmp" / "traps.go"
    text = src.read_text(encoding="utf-8")
    assert 'ev.EventType = "unknown_trap"' in text
    assert 'ev.Message = "unmapped trap " + trapOID' in text
