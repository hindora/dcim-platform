"""Lifecycle transitions, and the two records every one of them writes."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.repositories import lifecycle as repo
from app.repositories.lifecycle import TRANSITIONS, IllegalTransitionError

__all__ = ["TRANSITIONS", "IllegalTransitionError", "history", "transition"]


async def history(session: AsyncSession, device_id: str) -> list[dict[str, Any]]:
    return await repo.history(session, device_id)


async def transition(session: AsyncSession, *, device_id: str, to_state: str,
                     actor: str, reason: str | None = None,
                     change_ref: str | None = None,
                     ip: str | None = None,
                     user_agent: str | None = None) -> dict[str, Any]:
    """Move a device, and write BOTH records.

    `device_lifecycle_event` and `audit_log` are not redundant. The event is the
    business record an operator reads on the asset's Lifecycle tab, carrying the
    reason a change board asks for. The audit row is evidence, generic and
    credential-scrubbed, on the same trail as every other privileged action. One
    cannot be reconstructed from the other, and dropping either loses a question
    somebody asks.
    """
    current = await repo.current_state(session, device_id)
    if current is None:
        raise LookupError("device not found")
    if to_state == current:
        raise IllegalTransitionError(current, to_state)
    if to_state not in TRANSITIONS.get(current, ()):
        raise IllegalTransitionError(current, to_state)

    event = await repo.record_transition(
        session, device_id=device_id, from_state=current, to_state=to_state,
        actor=actor, reason=reason, change_ref=change_ref)

    await audit.record(
        session, actor=actor, action="device.lifecycle",
        target_type="device", target_id=device_id, ip=ip, user_agent=user_agent,
        before={"lifecycle": current},
        after={"lifecycle": to_state, "reason": reason, "change_ref": change_ref})

    return event
