"""Email notifications: the mail relay, routing rules, and the delivery log.

Reads need operator (rules hold people's addresses); every change is admin and
audited. The relay password is write-only: it goes in, and only "saved" ever
comes back out.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.alert_taxonomy import CATEGORIES, RESPONSE_CLASSES
from app.core.config import get_settings
from app.core.security import Principal, require_role
from app.db.session import get_session
from app.integrations import fingerprint as fp
from app.integrations import policy as policy_mod
from app.notifications import message as msg_mod
from app.notifications import rules as rules_mod
from app.notifications import smtp
from app.repositories import integrations as integrations_repo
from app.services import instance_settings

router = APIRouter(tags=["notifications"])

SEVERITIES = ["CRITICAL", "MAJOR", "MINOR", "WARNING", "INFO"]


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"message": str(exc)})


async def _org_name(session: AsyncSession) -> str:
    stored = await instance_settings.load(session)
    return instance_settings.resolve(stored.org_name, get_settings().org_name)[0]


# ------------------------------------------------------------------ relay


@router.get("/settings/smtp", summary="The mail relay alarms are sent through")
async def get_smtp(
    session: AsyncSession = Depends(get_session), _: Principal = Depends(require_role("operator"))
) -> dict:
    return (await smtp.load(session)).public()


@router.put("/settings/smtp", summary="Change the mail relay (admin)")
async def put_smtp(
    body: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
    p: Principal = Depends(require_role("admin")),
) -> dict:
    current = await smtp.load(session, with_password=True)
    try:
        cfg = smtp.clean_settings(body, current)
    except smtp.SmtpSettingsError as exc:
        raise _bad(exc) from exc
    before = current.public()
    await smtp.save(session, cfg, p.username, password_changed="password" in body)
    after = (await smtp.load(session)).public()
    ip, agent = audit.client_of(request)
    # The public views only: neither side of the audit row carries a password.
    await audit.record(
        session,
        actor=p.username,
        action="settings.smtp.update",
        target_type="smtp_setting",
        target_id="1",
        before=before,
        after=after,
        ip=ip,
        user_agent=agent,
    )
    await session.commit()
    return after


class TestSend(BaseModel):
    to: str


@router.post("/settings/smtp/test", summary="Send one test email through the saved relay (admin)")
async def test_smtp(
    body: TestSend,
    request: Request,
    session: AsyncSession = Depends(get_session),
    p: Principal = Depends(require_role("admin")),
) -> dict:
    to = body.to.strip()
    if not smtp.valid_email(to):
        raise _bad(ValueError(f"“{to}” is not an email address"))
    cfg = await smtp.load(session, with_password=True)
    if not cfg.host or not cfg.from_address:
        raise _bad(ValueError("save a relay host and a from address first"))
    org = await _org_name(session)
    message = smtp.base_message(
        cfg,
        org,
        [to],
        f"[TEST] {org} DCIM email is working",
        "This is a test from Settings > Notifications.\n\n"
        f"Relay: {cfg.host}:{cfg.port} ({cfg.security})\n"
        f"Requested by: {p.username}\n\n"
        "If this arrived, alarm emails will too. If it went to spam, ask your mail "
        "team to allow this sender.\n",
    )
    result = (await asyncio.to_thread(smtp.send_batch, cfg, [message]))[0]
    ip, agent = audit.client_of(request)
    await audit.record(
        session,
        actor=p.username,
        action="settings.smtp.test",
        target_type="smtp_setting",
        target_id="1",
        after={"to": to},
        ip=ip,
        user_agent=agent,
        outcome="ok" if result is None else "failed",
    )
    await session.commit()
    if result is not None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"message": str(result)})
    return {"ok": True, "to": to}


# ------------------------------------------------------------------ rules


@router.get("/notifications/options", summary="What a rule can choose from")
async def options(_: Principal = Depends(require_role("operator"))) -> dict:
    return {
        "severities": SEVERITIES,
        "categories": list(CATEGORIES),
        "response_classes": list(RESPONSE_CLASSES),
        "defaults": rules_mod.DEFAULT_POLICY,
        "max_recipients": rules_mod.MAX_RECIPIENTS,
    }


def _with_summary(rule: dict[str, Any]) -> dict[str, Any]:
    rule["summary"] = msg_mod.rule_summary(rule["policy"], notify_on_clear=rule["notify_on_clear"])
    return rule


@router.get("/notifications/rules", summary="Who is emailed about which alarms")
async def list_rules(
    session: AsyncSession = Depends(get_session), _: Principal = Depends(require_role("operator"))
) -> dict:
    return {"items": [_with_summary(r) for r in await rules_mod.list_rules(session)]}


@router.post("/notifications/rules", status_code=201, summary="Add a rule (admin)")
async def create_rule(
    body: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
    p: Principal = Depends(require_role("admin")),
) -> dict:
    try:
        fields = rules_mod.clean(body)
    except rules_mod.RuleError as exc:
        raise _bad(exc) from exc
    if await rules_mod.name_taken(session, fields["name"]):
        raise _bad(ValueError(f"a rule called “{fields['name']}” already exists"))
    rule_id = await rules_mod.create(session, fields, p.username)
    ip, agent = audit.client_of(request)
    await audit.record(
        session,
        actor=p.username,
        action="notification_rule.create",
        target_type="notification_rule",
        target_id=rule_id,
        after=fields,
        ip=ip,
        user_agent=agent,
    )
    await session.commit()
    return _with_summary(await rules_mod.get_rule(session, rule_id))


@router.patch("/notifications/rules/{rule_id}", summary="Change a rule (admin)")
async def update_rule(
    rule_id: str,
    body: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
    p: Principal = Depends(require_role("admin")),
) -> dict:
    current = await rules_mod.raw_rule(session, rule_id)
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such rule")
    try:
        fields = rules_mod.clean(body, current)
    except rules_mod.RuleError as exc:
        raise _bad(exc) from exc
    if "name" in fields and await rules_mod.name_taken(session, fields["name"], rule_id):
        raise _bad(ValueError(f"a rule called “{fields['name']}” already exists"))
    if fields:
        await rules_mod.update(session, rule_id, fields, p.username)
        ip, agent = audit.client_of(request)
        await audit.record(
            session,
            actor=p.username,
            action="notification_rule.update",
            target_type="notification_rule",
            target_id=rule_id,
            before={k: current.get(k) for k in fields},
            after=fields,
            ip=ip,
            user_agent=agent,
        )
        await session.commit()
    return _with_summary(await rules_mod.get_rule(session, rule_id))


@router.delete("/notifications/rules/{rule_id}", status_code=204, summary="Delete a rule (admin)")
async def delete_rule(
    rule_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    p: Principal = Depends(require_role("admin")),
) -> None:
    current = await rules_mod.raw_rule(session, rule_id)
    if current is None or not await rules_mod.delete(session, rule_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such rule")
    ip, agent = audit.client_of(request)
    await audit.record(
        session,
        actor=p.username,
        action="notification_rule.delete",
        target_type="notification_rule",
        target_id=rule_id,
        before={"name": current["name"], "recipients": list(current["recipients"])},
        ip=ip,
        user_agent=agent,
    )
    await session.commit()


class RulePreview(BaseModel):
    policy: dict[str, Any] = {}
    days: int = 7


@router.post("/notifications/rules/preview", summary="How many emails a policy would have sent")
async def preview(
    body: RulePreview,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict:
    """Replay a candidate policy over recent alarms - the noise estimate.

    Counts distinct CONDITIONS, which is what a rule emails about once each
    (plus a clear). The delay is ignored here: the history cannot say which of
    these would have cleared inside it.
    """
    if not 1 <= body.days <= 90:
        raise _bad(ValueError("days is between 1 and 90"))
    try:
        cleaned = rules_mod.clean({"policy": body.policy}, {"name": "x"})["policy"]
    except rules_mod.RuleError as exc:
        raise _bad(exc) from exc
    policy = {**rules_mod.resolved_policy(cleaned), "dwell_s": 0}
    alarms = await integrations_repo.recent_alarms_for_preview(session, days=body.days)
    matched: dict[str, dict[str, Any]] = {}
    by_severity: dict[str, int] = {}
    for alarm in alarms:
        if policy_mod.explain(alarm, policy):
            continue
        print_ = fp.of_alarm(alarm)
        if print_ not in matched:
            matched[print_] = alarm
            by_severity[alarm["severity"]] = by_severity.get(alarm["severity"], 0) + 1
    examples = sorted(matched.values(), key=lambda a: str(a.get("first_seen")), reverse=True)[:5]
    return {
        "days": body.days,
        "conditions": len(matched),
        "per_day": round(len(matched) / body.days, 1),
        "by_severity": by_severity,
        "sampled_alarms": len(alarms),
        "examples": [
            {
                "device_name": a.get("device_name"),
                "severity": a.get("severity"),
                "message": a.get("message"),
                "first_seen": a.get("first_seen"),
            }
            for a in examples
        ],
    }


@router.get("/notifications/deliveries", summary="Recent emails and what happened to each")
async def deliveries(
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: str | None = Query(None, alias="status"),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict:
    items, total = await rules_mod.deliveries(
        session, limit=limit, offset=offset, status=status_filter
    )
    return {"items": items, "total": total}
