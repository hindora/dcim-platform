"""Import every meter's panel schedule, and report what it found.

The reading half lives in `meter_schedule`; this is the part that decides what
to do with a label - which device it names, and what to record when it names
nothing this platform knows.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import meter_channels as repo
from app.services.meter_schedule import read_schedule_async

log = structlog.get_logger(__name__)

# An EV2-84 has 84 CT channels; the number is in the model name. 42 is the
# mid-size unit and the safe default - asking a 24-channel meter about channel
# 30 costs a timeout, so the default is deliberately not the largest.
_MODEL_CHANNELS = re.compile(r"EV2-(\d+)")
_DEFAULT_CHANNELS = 42


def channels_of(model: str) -> int:
    m = _MODEL_CHANNELS.search(model or "")
    return int(m.group(1)) if m else _DEFAULT_CHANNELS


async def import_all(session: AsyncSession, *,
                     timeout: float = 2.0) -> dict[str, Any]:
    """Read every meter's schedule and record it.

    One meter's silence does not fail the import: a controller that is down
    during a commissioning run is a normal Tuesday, and the other forty
    schedules are still worth having. A meter that answered NOTHING keeps the
    schedule it had - see `replace_for_meter` - because "unreachable" and
    "has no channels" must not look the same in this table.
    """
    found = await repo.meters(session)
    report: dict[str, Any] = {
        "meters_seen": len(found), "meters_read": 0, "meters_silent": 0,
        "channels": 0, "clamped": 0, "unresolved": [], "errors": [],
    }

    for meter in found:
        try:
            chans = await read_schedule_async(
                meter["ip"], channels_of(meter["model"]), timeout=timeout)
        except Exception as exc:
            log.warning("meter schedule read failed", meter=meter["name"],
                        ip=meter["ip"], error=str(exc))
            report["errors"].append({"meter": meter["name"], "error": str(exc)})
            continue

        if not chans:
            report["meters_silent"] += 1
            continue

        names = sorted({c.label for c in chans if c.label})
        by_name = await repo.resolve_names(session, names)

        rows = []
        for c in chans:
            branch = by_name.get(c.label) if c.label else None
            if c.label and branch is None:
                # Recorded with the label and no device: the meter says
                # something is clamped there and this platform cannot say
                # what. That is a finding - a device this DCIM has not got,
                # or a name that has drifted - and dropping the row would
                # hide it.
                report["unresolved"].append(
                    {"meter": meter["name"], "channel": c.instance,
                     "label": c.label})
            rows.append({"instance": c.instance, "branch_device_id": branch,
                         "label": c.label, "source": "bacnet"})

        await repo.replace_for_meter(session, meter["id"], rows)
        report["meters_read"] += 1
        report["channels"] += len(rows)
        report["clamped"] += sum(1 for r in rows if r["branch_device_id"])

    await session.commit()
    report["totals"] = await repo.stats(session)
    log.info("meter schedules imported", **{
        k: v for k, v in report.items() if not isinstance(v, (list, dict))})
    return report
