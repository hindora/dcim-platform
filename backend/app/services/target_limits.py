"""Per-address poll limits and pool budget shares (docs/26 Phases 5 and 9).

A facility limits how hard a DCIM poller may drive its equipment, and the
collector enforces it (collector/internal/throttle). Two settings live here:

- a TARGET limit: polls in flight at one address and the minimum gap between
  their starts - what Kepware calls a device's inter-request delay, what a
  Moxa MGate's master limit and a single RS-485 line impose, and what a
  small PDU or UPS network card needs. Set per protocol on the pool (one
  management network, usually one gateway model), overridable per endpoint.
- each collector's SHARE of its pool's points-per-second budget: the budget
  times the share of the pool's endpoints that collector owns. A failover's
  survivor owns them all, so it gets the whole budget; nothing is lost or
  doubled when ownership moves.
"""

from __future__ import annotations

from typing import Any

#: Protocols polled by the scheduler. gNMI streams and traps are pushed by
#: the device and never polled, so a poll limit does not apply to them.
PROTOCOLS = ("snmp", "redfish", "bacnet", "modbus")
MAX_CONCURRENT = 64
MAX_INTERVAL_MS = 60_000


class TargetLimitError(ValueError):
    """A limit an operator sent that cannot be applied, with a message fit to
    show them as-is."""


def validate_limit(value: Any, where: str = "target_limit") -> dict[str, int] | None:
    """One limit: {"max_concurrent"?, "min_interval_ms"?}. None, or nothing
    set, is no limit."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TargetLimitError(f"{where} must be an object")
    unknown = set(value) - {"max_concurrent", "min_interval_ms"}
    if unknown:
        raise TargetLimitError(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
    out: dict[str, int] = {}
    mc = value.get("max_concurrent")
    if mc is not None:
        if not isinstance(mc, int) or isinstance(mc, bool) or not 1 <= mc <= MAX_CONCURRENT:
            raise TargetLimitError(
                f"{where}.max_concurrent must be a whole number from 1 to {MAX_CONCURRENT}")
        out["max_concurrent"] = mc
    gap = value.get("min_interval_ms")
    if gap is not None:
        if (not isinstance(gap, int) or isinstance(gap, bool)
                or not 0 <= gap <= MAX_INTERVAL_MS):
            raise TargetLimitError(
                f"{where}.min_interval_ms must be a whole number from 0 to {MAX_INTERVAL_MS}")
        if gap:
            out["min_interval_ms"] = gap
    return out or None


def validate_limits(value: Any) -> dict[str, dict[str, int]]:
    """A pool's per-protocol defaults. Protocols with no limit are dropped."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TargetLimitError("target_limits must be an object keyed by protocol")
    out: dict[str, dict[str, int]] = {}
    for proto, limit in value.items():
        if proto not in PROTOCOLS:
            raise TargetLimitError(
                f"target_limits: {proto!r} is not a polled protocol "
                f"(one of {', '.join(PROTOCOLS)})")
        clean = validate_limit(limit, f"target_limits.{proto}")
        if clean:
            out[proto] = clean
    return out


def resolve(override: Any, pool_limits: Any, protocol: str) -> dict[str, int] | None:
    """What a collector enforces for one endpoint: its own override, else
    its pool's default for the protocol. Stored values are trusted (the API
    validated them); anything malformed reads as no limit, never a refusal
    to serve the assignment."""
    if isinstance(override, dict) and override:
        return dict(override)
    if isinstance(pool_limits, dict):
        limit = pool_limits.get(protocol)
        if isinstance(limit, dict) and limit:
            return dict(limit)
    return None


def budget_shares(budgets: dict[str, int | None], owners: dict[str, str | None],
                  pool_of: dict[str, str | None], collector_id: str) -> dict[str, float]:
    """Pure: this collector's points-per-second share of each budgeted pool -
    budget x (its endpoints in the pool / the pool's owned endpoints).

    Only owned endpoints count toward the total: an unowned endpoint is
    polled by nobody, so it uses none of the budget. A pool where this
    collector owns nothing gets no entry - it has nothing to throttle."""
    total: dict[str, int] = {}
    mine: dict[str, int] = {}
    for eid, owner in owners.items():
        pool = pool_of.get(eid)
        if owner is None or pool is None or not budgets.get(pool):
            continue
        total[pool] = total.get(pool, 0) + 1
        if owner == collector_id:
            mine[pool] = mine.get(pool, 0) + 1
    return {pool: round(float(budgets[pool] or 0) * n / total[pool], 3)
            for pool, n in mine.items()}
