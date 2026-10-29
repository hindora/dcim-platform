"""A model's power budget: what every unit of a SKU is planned at.

The Power page's Allocated figure is built from this, one budget per model
rather than per device, because that is how a planner commits: an R640 is
budgeted once and every R640 inherits it. The budget sits in the model's
attributes beside its source, and the source is the whole story - a figure
with no basis is a limit nobody should plan against.

Three sources exist: a measured one ("measured: 7-day peak x 1.1"), the
importer's ("simulator nameplate: ..."), and an operator's, written here.
The importer overwrites only its own; an operator's survives every
re-import, and clearing it hands the model back to the planning rule
(rated power x the derate).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.logging import get_logger
from app.core.security import Principal, require_role
from app.db.session import get_session

log = get_logger(__name__)
router = APIRouter(prefix="/models", tags=["models"])


class BudgetBody(BaseModel):
    model_config = {"extra": "forbid"}
    #: Watts per unit of this model. Null clears the budget: the model goes
    #: back to the planning rule, rated power times the derate.
    budget_w: int | None = Field(default=None, ge=0, le=100_000)
    #: Why this figure - a reference, a measurement, a policy. Optional;
    #: the actor and date are recorded regardless.
    note: str | None = Field(default=None, max_length=200)


@router.patch("/{model_id}/budget", summary="Set or clear a model's power budget")
async def set_budget(
    model_id: str,
    body: BudgetBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    row = (await session.execute(text("""
        SELECT m.id::text AS id, m.name, m.rated_power_w,
               NULLIF(m.attributes->>'budget_w', '')::int AS budget_w,
               m.attributes->>'budget_source' AS budget_source
          FROM model m WHERE m.id = CAST(:id AS uuid)
    """), {"id": model_id})).mappings().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model not found")
    before = {"budget_w": row["budget_w"], "budget_source": row["budget_source"]}
    if body.budget_w is None:
        source = None
        await session.execute(text("""
            UPDATE model SET attributes = attributes - 'budget_w' - 'budget_source'
             WHERE id = CAST(:id AS uuid)
        """), {"id": model_id})
    else:
        stamp = datetime.now(UTC).strftime("%Y-%m-%d")
        source = f"operator: {principal.username} {stamp}"
        if body.note:
            source += f" - {body.note.strip()}"
        await session.execute(text("""
            UPDATE model
               SET attributes = COALESCE(attributes, '{}'::jsonb)
                   || jsonb_build_object('budget_w', CAST(:w AS int),
                                         'budget_source', CAST(:src AS text))
             WHERE id = CAST(:id AS uuid)
        """), {"id": model_id, "w": body.budget_w, "src": source})
    after = {"budget_w": body.budget_w, "budget_source": source}
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal), action="model.budget",
                       target_type="model", target_id=model_id, ip=ip, user_agent=agent,
                       before=before, after=after)
    await session.commit()
    log.info("model budget set", model_id=model_id, model=row["name"],
             actor=principal.username, budget_w=body.budget_w)
    return {"model_id": model_id, "model": row["name"], "rated_power_w": row["rated_power_w"],
            **after}
