"""The advisory budget (docs/33 task 3.8, docs/35 section 6).

ISA-18.2's point about alarms holds for advisories too: a queue that produces
more than people can read is not read at all, and then the one finding that
mattered is lost in it. So each detector may open a bounded number of NEW
advisory findings a day, and process findings (L2 rules, L3 baselines) share
an estate-wide budget on top. Beyond either, new findings are held back and
counted rather than opened.

What it never touches:

* the ALERT lane - PU-3, GN-1, CD-6 and anything an operator promotes. Those
  are alarms; a safety alarm is never rate-limited;
* reopens - a flap inside the reopen window is one finding with a count
  (docs/29 section 8.2), so chatter is already collapsed before it gets here;
* data-quality MASKING - an L0 detector over budget stops opening findings,
  but keeps hiding the bad readings from the rules, which is its other job.

A process detector held back on two UTC days running is switched off, with a
note saying why. It is producing more findings than anyone will act on, and
the fix is a person lowering its sensitivity, not the queue absorbing it.
Data-quality detectors are never switched off: losing their masking would let
the rules reason from bad readings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any


@dataclass
class Budget:
    per_detector: int
    estate_process: int
    day: date
    #: detector id -> {"opened": n, "held": n} for `day`, as stored.
    today: dict[str, dict[str, int]] = field(default_factory=dict)
    #: increments made this sweep, flushed at the end.
    delta: dict[str, dict[str, int]] = field(default_factory=dict)
    #: detector id -> layer, for the estate-wide process count.
    layers: dict[str, int] = field(default_factory=dict)

    def _count(self, did: str, what: str) -> int:
        return (self.today.get(did, {}).get(what, 0)
                + self.delta.get(did, {}).get(what, 0))

    def _bump(self, did: str, what: str) -> None:
        d = self.delta.setdefault(did, {"opened": 0, "held": 0})
        d[what] += 1

    def process_opened(self) -> int:
        ids = set(self.today) | set(self.delta)
        return sum(self._count(i, "opened") for i in ids if self.layers.get(i) in (2, 3))

    def allow(self, did: str, layer: int, lane: str) -> str | None:
        """None if a new finding may open, else why it is held back."""
        self.layers[did] = layer
        if lane == "alert":
            return None
        if self._count(did, "opened") >= self.per_detector:
            return "detector_daily_cap"
        if layer in (2, 3) and self.process_opened() >= self.estate_process:
            return "estate_daily_cap"
        return None

    def opened(self, did: str, layer: int) -> None:
        self.layers[did] = layer
        self._bump(did, "opened")

    def held(self, did: str, layer: int) -> None:
        self.layers[did] = layer
        self._bump(did, "held")


def to_switch_off(rows: dict[tuple[str, date], dict[str, int]], did: str, layer: int,
                  lane: str, today: date) -> bool:
    """Held back on today AND yesterday: a process detector over its budget."""
    if lane == "alert" or layer not in (2, 3):
        return False
    yday = today - timedelta(days=1)
    return (rows.get((did, today), {}).get("held", 0) > 0
            and rows.get((did, yday), {}).get("held", 0) > 0)


def switch_off_note(did: str, cap: int, today: date) -> str:
    return (f"Switched off by the daily budget on {today.isoformat()}: {did} wanted to "
            f"open more than {cap} findings a day on two days running, more than anyone "
            f"will act on. Lower its sensitivity, then turn it back on.")


def view(rows: dict[tuple[str, date], dict[str, int]], did: str, cap: int,
         today: date) -> dict[str, Any]:
    t = rows.get((did, today), {})
    y = rows.get((did, today - timedelta(days=1)), {})
    return {"cap": cap, "today_opened": t.get("opened", 0), "today_held": t.get("held", 0),
            "yesterday_held": y.get("held", 0)}
