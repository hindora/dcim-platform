"""Discovery findings as alarms, so they reach the console and the service desk.

A 02:00 sweep that finds an unrecorded box used to wait until somebody opened the
Discovery page. Three findings now raise alarms, because each is something a
person has to act on:

* ``discovery_unrecorded`` - answers on the management network, no record
  accounts for it. No device, so keyed by address.
* ``discovery_missing`` - on record, in service, silent to the last sweep of its
  address. Not raised for maintenance, which is expected to go quiet.
* ``discovery_replaced`` - a hardware field (serial, platform, model, vendor,
  UUID) changed at an address inventory knows.

Changed firmware is drift and stays quiet, as it does on the page.

The alarm is the finding's lifecycle, not an event: reconcile() raises what is
open and clears what is resolved - promoted, dismissed, acknowledged, answering
again - so nothing needs a clear sent by hand. Severity is MINOR or WARNING,
both "alert" class: an inventory discrepancy is worth a ticket, not a page.
Whether it becomes a ticket is the integration policy's decision, reached
through the same outbox as every other alarm.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.alarms.service import AlarmAction
from app.core import alert_taxonomy
from app.core.logging import get_logger
from app.repositories import discovery as disc_repo

log = get_logger("discovery.alarms")

SOURCE = "discovery"
UNRECORDED = "discovery_unrecorded"
MISSING = "discovery_missing"
REPLACED = "discovery_replaced"
TYPES = (UNRECORDED, MISSING, REPLACED)


async def _unrecorded(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT DISTINCT ON (c.address) host(c.address) AS address,
               COALESCE(NULLIF(c.identity ->> 'hostName', ''),
                        NULLIF(c.identity ->> 'sysName', '')) AS name,
               c.suggested_vendor, c.suggested_device_type
          FROM discovery_candidate c
         WHERE c.status = 'new' AND c.matched_device_id IS NULL
           AND c.address IS NOT NULL
         ORDER BY c.address, (c.serial IS NULL), c.last_seen DESC
    """))).mappings().all()
    return [dict(r) for r in rows]


async def _replaced(session: AsyncSession, hardware: list[str]) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT DISTINCT ON (c.matched_device_id, c.address)
               c.matched_device_id::text AS device_id, host(c.address) AS address,
               d.name, ch.field, ch.old_value, ch.new_value
          FROM discovery_candidate c
          JOIN discovery_identity_change ch ON ch.candidate_id = c.id
          JOIN device d ON d.id = c.matched_device_id
         WHERE c.status = 'new' AND ch.acknowledged_at IS NULL
           AND ch.field = ANY(:hardware)
         ORDER BY c.matched_device_id, c.address, ch.detected_at DESC
    """), {"hardware": hardware})).mappings().all()
    return [dict(r) for r in rows]


def _missing_message(m: dict[str, Any]) -> str:
    base = f"{m['name']} did not answer the sweep of {m['subnet']}"
    if m.get("any_online"):
        return f"{base}; the poller still reaches it - check the sweep's credentials or ACL"
    if m.get("any_online") is False:
        return f"{base}; the poller cannot reach it either"
    return f"{base}; it is not polled, so nothing else is watching it"


async def desired(session: AsyncSession) -> list[dict[str, Any]]:
    """Every discovery alarm that should be open right now, with its message."""
    out: list[dict[str, Any]] = []
    for u in await _unrecorded(session):
        what = " ".join(x for x in (u.get("suggested_vendor"),
                                    u.get("suggested_device_type")) if x)
        label = u["name"] or what
        out.append({"device_id": None, "alarm_type": UNRECORDED,
                    "instance": u["address"], "severity": "MINOR",
                    "message": f"{u['address']} answers on the management network "
                               f"and is not in inventory"
                               + (f" ({label})" if label else "")})
    for m in await disc_repo.missing_devices(session):
        if m["lifecycle"] == "maintenance":
            continue
        # Unpolled is the one where this is the ONLY signal: nothing else is
        # watching the box, so its silence would otherwise go unseen.
        out.append({"device_id": m["device_id"], "alarm_type": MISSING,
                    "instance": "", "severity": "MINOR" if m.get("any_online") is None
                    else "WARNING", "message": _missing_message(m)})
    for r in await _replaced(session, sorted(disc_repo.HARDWARE_FIELDS)):
        out.append({"device_id": r["device_id"], "alarm_type": REPLACED,
                    "instance": r["address"], "severity": "MINOR",
                    "message": f"{r['name']} at {r['address']}: {r['field']} changed "
                               f"from {r['old_value']} to {r['new_value']} - a "
                               f"different box, unless somebody swapped it on purpose"})
    return out


async def _raise(session: AsyncSession, a: dict[str, Any], at: datetime) -> dict[str, Any]:
    row = (await session.execute(text("""
        INSERT INTO alarm (device_id, alarm_type, instance, severity, state, message,
                           source, first_seen, last_seen, category, detection,
                           response_class)
        VALUES (CAST(:device AS uuid), :type, :instance, CAST(:severity AS severity_t),
                'ACTIVE', :message, :source, :at, :at, :category, :detection, :klass)
        ON CONFLICT (device_id, alarm_type, instance) WHERE state <> 'CLEARED'
        DO UPDATE SET
            prev_severity    = alarm.severity,
            severity         = EXCLUDED.severity,
            message          = EXCLUDED.message,
            last_seen        = GREATEST(alarm.last_seen, EXCLUDED.last_seen),
            occurrence_count = alarm.occurrence_count + 1
        RETURNING id::text, device_id::text AS device_id, alarm_type, instance,
                  severity::text AS severity, prev_severity::text AS prev_severity,
                  occurrence_count, message
    """), {"device": a["device_id"], "type": a["alarm_type"], "instance": a["instance"],
           "severity": a["severity"], "message": a["message"], "source": SOURCE,
           "at": at, "category": alert_taxonomy.classify(a["alarm_type"]),
           "detection": alert_taxonomy.detection_for(SOURCE),
           "klass": alert_taxonomy.response_class_for(a["severity"])})).mappings().first()
    out = dict(row)
    out["change"] = ("created" if out["occurrence_count"] == 1
                     else "escalated" if out["prev_severity"] != out["severity"]
                     else "touched")
    return out


async def reconcile(session: AsyncSession) -> list[AlarmAction]:
    """Raise every discovery alarm that should be open; clear every one that
    should not. Returns the transitions, for the integration outbox."""
    now = datetime.now(UTC)
    want = await desired(session)
    keys = {(a["device_id"], a["alarm_type"], a["instance"]) for a in want}

    actions: list[AlarmAction] = []
    for a in want:
        row = await _raise(session, a, now)
        if row["change"] == "created":
            actions.append(AlarmAction("alarm_created", row))
        elif row["change"] == "escalated":
            actions.append(AlarmAction("alarm_updated", row))

    open_rows = (await session.execute(text("""
        SELECT id::text, device_id::text AS device_id, alarm_type, instance,
               severity::text AS severity, message
          FROM alarm
         WHERE source = :source AND state <> 'CLEARED'
    """), {"source": SOURCE})).mappings().all()
    stale = [r["id"] for r in open_rows
             if (r["device_id"], r["alarm_type"], r["instance"]) not in keys]
    if stale:
        cleared = (await session.execute(text("""
            UPDATE alarm SET state = 'CLEARED', cleared_at = :at, cleared_by = 'discovery'
             WHERE id = ANY(CAST(:ids AS uuid[])) AND state <> 'CLEARED'
            RETURNING id::text, device_id::text AS device_id, alarm_type, instance,
                      severity::text AS severity, message
        """), {"ids": stale, "at": now})).mappings().all()
        actions += [AlarmAction("alarm_cleared", dict(r)) for r in cleared]
    if actions:
        log.info("discovery alarms reconciled", open=len(want),
                 raised=sum(a.kind == "alarm_created" for a in actions),
                 cleared=sum(a.kind == "alarm_cleared" for a in actions))
    return actions


async def reconcile_and_enqueue(session: AsyncSession) -> int:
    """Reconcile, and hand the transitions to the ticketing outbox in the same
    transaction - the alarm and the intent to ticket it commit together."""
    from app.integrations import outbox
    actions = await reconcile(session)
    await outbox.enqueue_actions(session, actions)
    return len(actions)
