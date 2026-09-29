"""Refusing data from a collector that does not own the endpoint.

Ingest used to accept any batch for any endpoint. With one collector that is
harmless; with two it hides the worst sharding failure there is. Two collectors
polling one device each see part of the counter increments, so every rate is
wrong, and each writes its own samples, so every chart is a sawtooth - and
nothing anywhere says so. It is also the whole of the spoofing surface: anyone
able to XADD to the stream could write readings, and states, for any device.

So the batch's `collector_id` is checked against the sharding plan, the same
plan the assignment endpoint serves. Three things are NOT refused:

- an endpoint the plan does not know yet (added since the last refresh) or
  that nobody can own. Refusing those is refusing data because this cache is
  thirty seconds behind the inventory;
- a message without a collector id, which is what a collector older than the
  field sends;
- the previous owner for a grace period after a move. The old owner learns of
  the move on its next assignment fetch and finishes polls already in flight,
  so its last minute or two of samples are real data, not an intruder.

Events are exempt entirely. A trap goes wherever the device sends it, which is
by design often not its owner - see ResolveEntry in the schemas - and the
dedup key collapses the copies.
"""

from __future__ import annotations

import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.logging import get_logger

log = get_logger("ingest.ownership")

# How often the plan is recomputed. The assignment endpoint serves the same
# plan every 30 s, so a fresher copy here would only disagree with what the
# collectors are actually running.
REFRESH_S = 30.0

# How long a previous owner's messages are still accepted after a move: two
# assignment intervals plus the refresh above, so the old owner has fetched the
# change and drained what it had in flight.
GRACE_S = 120.0


class OwnershipGuard:
    def __init__(self) -> None:
        self._owner: dict[str, str | None] = {}
        # endpoint id -> (previous owner, monotonic deadline)
        self._previous: dict[str, tuple[str, float]] = {}
        self._refreshed = float("-inf")
        self._warned: dict[str, float] = {}
        self._owners: set[str] = set()
        self._forced_at = float("-inf")
        self._force = False

    @property
    def loaded(self) -> bool:
        return self._refreshed != float("-inf")

    def due(self) -> bool:
        return self._force or time.monotonic() - self._refreshed >= REFRESH_S

    async def refresh(self, session: AsyncSession) -> None:
        now = time.monotonic()
        from app.services import collector as collectors

        plan = await collectors.ownership(session)
        if self.loaded:
            for endpoint_id, owner in plan.items():
                before = self._owner.get(endpoint_id)
                if before and before != owner:
                    self._previous[endpoint_id] = (before, now + GRACE_S)
        self._previous = {k: v for k, v in self._previous.items() if v[1] > now}
        self._owner = plan
        self._owners = {o for o in plan.values() if o}
        self._refreshed = now
        self._force = False

    def allows(self, collector_id: str, endpoint_id: str) -> bool:
        if not collector_id or not endpoint_id:
            return True
        owner = self._owner.get(endpoint_id)
        if owner is None or owner == collector_id:
            return True
        previous = self._previous.get(endpoint_id)
        return (previous is not None and previous[0] == collector_id
                and time.monotonic() < previous[1])

    def refuse(self, collector_id: str, stream: str, count: int) -> None:
        """Count a refusal, and say so in the log at most once a minute.

        A sender that owns nothing in this copy of the plan may simply be new:
        a collector registers on its first assignment fetch and starts sending
        at once, up to a refresh interval before this cache has heard of it.
        So a refusal from one asks for an early refresh - at most every ten
        seconds, so a genuine intruder cannot turn it into a query storm.
        """
        now = time.monotonic()
        if collector_id not in self._owners and now - self._forced_at >= 10.0:
            self._forced_at = now
            self._force = True
        metrics.ingest_foreign.labels(collector_id=collector_id,
                                      stream=stream).inc(count)
        if now - self._warned.get(collector_id, float("-inf")) >= 60.0:
            self._warned[collector_id] = now
            log.warning("dropped messages from a collector that does not own "
                        "the endpoint", collector_id=collector_id,
                        stream=stream, count=count)
