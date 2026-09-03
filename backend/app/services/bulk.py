"""Bulk operations: per-row transactions, a row-level report, and one audit row
each.

Those three are the contract, not implementation notes (docs/21 §10).

A batch that fails on row 3 must not discard rows 1 and 2, and must not roll
back a successful move because a later one collided - so each row runs in its
own savepoint. The exception is an explicitly atomic move, because moving half a
rack is sometimes worse than moving none of it.

And the report is row-level. "2 failed" in a toast is how a feature becomes
distrusted: the operator cannot tell which two, cannot retry them, and cannot
find out why. Every failure carries the object's name, a stable machine key and
a sentence written for a person.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger("bulk")

#: Constraint or error text -> (stable key, sentence template).
#:
#: Every constraint the bulk paths can hit is translated. Without this the
#: feature returns "duplicate key value violates unique constraint
#: ix_device_mgmt_ip_live" to a facilities technician, which is a way of saying
#: nothing at all.
_TRANSLATIONS: tuple[tuple[str, str, str], ...] = (
    ("device_u_no_overlap", "rack_unit_occupied",
     "those rack units are already occupied"),
    ("ix_device_mgmt_ip_live", "mgmt_ip_in_use",
     "another live device already has that management address"),
    ("ix_device_serial_unique", "serial_in_use",
     "another asset already carries that serial number"),
    ("ix_device_asset_tag_unique", "asset_tag_in_use",
     "another asset already carries that asset tag"),
    ("ck_part_stock_nonneg", "insufficient_stock",
     "that would take stock below zero"),
    ("ck_support_contract_dates", "contract_dates_invalid",
     "a contract cannot end before it starts"),
    ("uq_tag_kv", "tag_exists", "that tag already exists"),
    ("violates foreign key", "referenced",
     "something still refers to this and would be orphaned"),
)


@dataclass
class RowResult:
    device_id: str
    name: str | None = None
    error: str | None = None
    message: str | None = None


@dataclass
class BulkReport:
    succeeded: int = 0
    failed: list[RowResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "succeeded": self.succeeded,
            "failed": [
                {"device_id": r.device_id, "name": r.name,
                 "error": r.error, "message": r.message}
                for r in self.failed
            ],
        }


#: Application refusals, keyed by exception type. Checked BEFORE the string
#: table: these are expected outcomes with a stable key of their own, and
#: matching them by substring would be fragile and would log them as surprises.
def _typed(exc: Exception) -> tuple[str, str] | None:
    from app.repositories.lifecycle import IllegalTransitionError
    from app.repositories.parts import InsufficientStockError
    from app.repositories.reservations import ReservationConflictError
    from app.repositories.tags import UnknownObjectTypeError

    for kind, key in ((IllegalTransitionError, "illegal_transition"),
                      (InsufficientStockError, "insufficient_stock"),
                      (ReservationConflictError, "reservation_conflict"),
                      (UnknownObjectTypeError, "invalid_object_type")):
        if isinstance(exc, kind):
            # These already carry a sentence written for a person - the allowed
            # transitions, the counts, the occupying device.
            return key, str(exc)
    return None


def translate(exc: Exception) -> tuple[str, str]:
    """Turn a refusal into something an operator can act on."""
    typed = _typed(exc)
    if typed:
        return typed
    text = str(exc)
    for needle, key, message in _TRANSLATIONS:
        if needle in text:
            return key, message
    # Unmatched is reported as unmatched rather than dressed up. A wrong
    # explanation is worse than an honest "we do not know".
    log.warning("untranslated bulk failure", error=text[:400])
    return "rejected", "the database refused this change"


_CONSTRAINT = re.compile(r'constraint "([a-z_0-9]+)"')


def constraint_of(exc: Exception) -> str | None:
    match = _CONSTRAINT.search(str(exc))
    return match.group(1) if match else None


async def run(session: AsyncSession, items: list[dict[str, Any]],
              apply: Callable[[dict[str, Any]], Awaitable[None]],
              *, atomic: bool = False,
              describe: Callable[[dict[str, Any]], tuple[str, str | None]]
              | None = None,
              enrich: Callable[[dict[str, Any], Exception],
                               Awaitable[str | None]] | None = None,
              ) -> BulkReport:
    """Apply `apply` to every item, one savepoint each.

    `atomic=True` runs them in one savepoint instead, so the first failure
    discards the lot - which is what somebody moving a whole rack wants, and
    exactly what somebody retagging four hundred assets does not.

    `enrich` gets a chance to make a message specific: "U20-U23 is occupied by
    SRV-DC1-HA-R2-09" rather than "those rack units are already occupied". It
    runs AFTER the savepoint has rolled back, so its own query sees a clean
    session.
    """
    report = BulkReport()

    def describe_item(item: dict[str, Any]) -> tuple[str, str | None]:
        if describe:
            return describe(item)
        return str(item.get("device_id") or ""), None

    if atomic:
        try:
            async with session.begin_nested():
                for item in items:
                    await apply(item)
            report.succeeded = len(items)
        except Exception as exc:
            key, message = translate(exc)
            # Which row broke it is still worth naming, even though none applied.
            report.failed = [
                RowResult(device_id=i, name=n, error=key,
                          message=f"{message} (nothing was applied: this batch "
                                  f"was all-or-nothing)")
                for i, n in [describe_item(item) for item in items]
            ][:1]
        return report

    for item in items:
        identifier, name = describe_item(item)
        try:
            async with session.begin_nested():
                await apply(item)
            report.succeeded += 1
        except Exception as exc:
            key, message = translate(exc)
            if enrich:
                try:
                    better = await enrich(item, exc)
                    if better:
                        message = better
                except Exception:
                    pass
            report.failed.append(
                RowResult(device_id=identifier, name=name,
                          error=key, message=message))
    return report
