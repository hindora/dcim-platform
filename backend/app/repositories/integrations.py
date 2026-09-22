"""SQL for integrations, the outbox and the ticket links."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Everything about an integration except the two ciphertexts. No query in this
# module selects `secret_enc` or `webhook_secret_enc` by accident, because no
# query selects `*`: the decrypting callers ask for them by name, once, below.
_INTEGRATION_COLS = """
    id::text, kind, name, enabled, base_url, cloud_id, config, version,
    secret_hint, secret_kind, secret_expires_at,
    webhook_token, webhook_id, webhook_expires_at,
    (webhook_secret_enc IS NOT NULL) AS webhook_configured,
    created_at, updated_at, updated_by
"""


# ------------------------------------------------------------- integrations

async def list_integrations(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text(f"""
        SELECT {_INTEGRATION_COLS},
               (SELECT count(*) FROM integration_outbox o
                 WHERE o.integration_id = i.id AND o.state = 'pending') AS pending,
               (SELECT count(*) FROM integration_outbox o
                 WHERE o.integration_id = i.id AND o.state = 'dead')    AS dead,
               (SELECT max(o.created_at) FROM integration_outbox o
                 WHERE o.integration_id = i.id AND o.state = 'done')    AS last_delivered,
               (SELECT count(*) FROM jira_link l
                 WHERE l.integration_id = i.id AND l.closed_at IS NULL) AS open_tickets
          FROM integration i
         ORDER BY name
    """))).mappings().all()
    return [dict(r) for r in rows]


async def get_integration(session: AsyncSession, integration_id: str
                          ) -> dict[str, Any] | None:
    row = (await session.execute(text(f"""
        SELECT {_INTEGRATION_COLS} FROM integration i
         WHERE id = CAST(:id AS uuid)
    """), {"id": integration_id})).mappings().first()
    return dict(row) if row else None


async def active(session: AsyncSession) -> list[dict[str, Any]]:
    """Every enabled integration. The dispatcher's first question each pass."""
    rows = (await session.execute(text(f"""
        SELECT {_INTEGRATION_COLS} FROM integration i WHERE enabled ORDER BY name
    """))).mappings().all()
    return [dict(r) for r in rows]


async def secrets_of(session: AsyncSession, integration_id: str
                     ) -> dict[str, Any] | None:
    """The ciphertexts, by themselves, under neutral names.

    Two things are deliberate. It is a SEPARATE query, so reading an
    integration for display cannot accidentally carry a credential into a
    response body. And it renames the columns on the way out - `blob`,
    `webhook_blob` - so that this module is the ONLY one in the application
    that names `secret_enc` at all, which is what `test_secret_access` checks
    and what makes the check meaningful rather than an allowlist that grows.
    """
    row = (await session.execute(text("""
        SELECT secret_enc          AS blob,
               webhook_secret_enc  AS webhook_blob,
               secret_kind         AS kind
          FROM integration WHERE id = CAST(:id AS uuid)
    """), {"id": integration_id})).mappings().first()
    return dict(row) if row else None


async def create_integration(session: AsyncSession, *, kind: str, name: str,
                             base_url: str, cloud_id: str | None,
                             config: dict[str, Any], blob: bytes,
                             secret_hint: str, secret_kind: str,
                             secret_expires_at: datetime | None,
                             actor: str) -> dict[str, Any]:
    row = (await session.execute(text(f"""
        INSERT INTO integration (kind, name, base_url, cloud_id, config,
                                 secret_enc, secret_hint, secret_kind,
                                 secret_expires_at, updated_by)
        VALUES (:kind, :name, :base_url, :cloud_id, CAST(:config AS jsonb),
                :secret_enc, :secret_hint, :secret_kind,
                :secret_expires_at, :actor)
        RETURNING {_INTEGRATION_COLS}
    """), {"kind": kind, "name": name, "base_url": base_url,
           "cloud_id": cloud_id, "config": json.dumps(config),
           "secret_enc": blob, "secret_hint": secret_hint,
           "secret_kind": secret_kind, "secret_expires_at": secret_expires_at,
           "actor": actor})).mappings().one()
    return dict(row)


async def update_integration(session: AsyncSession, integration_id: str, *,
                             actor: str, name: str | None = None,
                             base_url: str | None = None,
                             cloud_id: str | None = None,
                             enabled: bool | None = None,
                             config: dict[str, Any] | None = None,
                             blob: bytes | None = None,
                             secret_hint: str | None = None,
                             secret_kind: str | None = None,
                             secret_expires_at: datetime | None = None,
                             ) -> dict[str, Any] | None:
    """Patch. Only what the caller names is touched.

    `version` is bumped only when `config` moves, because it is the ETag the
    UI compares: bumping it on an enable/disable would make every toggle look
    like a configuration change to anyone diffing.
    """
    sets = ["updated_at = now()", "updated_by = :actor"]
    params: dict[str, Any] = {"id": integration_id, "actor": actor}
    for column, value in (("name", name), ("base_url", base_url),
                          ("cloud_id", cloud_id), ("enabled", enabled),
                          ("secret_hint", secret_hint),
                          ("secret_kind", secret_kind)):
        if value is not None:
            sets.append(f"{column} = :{column}")
            params[column] = value
    if blob is not None:
        sets.append("secret_enc = :secret_enc")
        params["secret_enc"] = blob
        # Rotating the credential always rewrites the expiry, including to
        # NULL. A stale expiry from the previous token would either alarm
        # about a credential that no longer exists or stay quiet about one
        # that is about to lapse.
        sets.append("secret_expires_at = :secret_expires_at")
        params["secret_expires_at"] = secret_expires_at
    if config is not None:
        sets.append("config = CAST(:config AS jsonb)")
        sets.append("version = integration.version + 1")
        params["config"] = json.dumps(config)

    row = (await session.execute(text(f"""
        UPDATE integration SET {', '.join(sets)}
         WHERE id = CAST(:id AS uuid)
        RETURNING {_INTEGRATION_COLS}
    """), params)).mappings().first()
    return dict(row) if row else None


async def set_webhook(session: AsyncSession, integration_id: str, *,
                      token: str, blob: bytes, webhook_id: str | None,
                      expires_at: datetime | None) -> None:
    await session.execute(text("""
        UPDATE integration
           SET webhook_token = :token, webhook_secret_enc = :secret,
               webhook_id = :webhook_id, webhook_expires_at = :expires_at,
               updated_at = now()
         WHERE id = CAST(:id AS uuid)
    """), {"id": integration_id, "token": token, "secret": blob,
           "webhook_id": webhook_id, "expires_at": expires_at})


async def delete_integration(session: AsyncSession, integration_id: str) -> bool:
    res = await session.execute(text("""
        DELETE FROM integration WHERE id = CAST(:id AS uuid) RETURNING id
    """), {"id": integration_id})
    return res.first() is not None


async def expiring_credentials(session: AsyncSession, within_days: int = 30
                               ) -> list[dict[str, Any]]:
    """Enabled integrations whose credential lapses soon, or already has.

    Read by the platform monitor. A credential that expired yesterday is the
    interesting case and it sorts first, because "expiring in -1 days" is how
    this failure actually presents: everything looks configured and nothing
    has been delivered since Tuesday.
    """
    rows = (await session.execute(text("""
        SELECT id::text, name, kind, secret_kind, secret_expires_at,
               EXTRACT(EPOCH FROM (secret_expires_at - now())) / 86400.0 AS days_left
          FROM integration
         WHERE enabled AND secret_expires_at IS NOT NULL
           AND secret_expires_at < now() + make_interval(days => :days)
         ORDER BY secret_expires_at
    """), {"days": within_days})).mappings().all()
    return [dict(r) for r in rows]


# ------------------------------------------------------- alarms, for export

#: The alarm as the exporter needs to see it.
#:
#: NOT `_ALARM_SELECT` from the alarm repository, and the difference is the
#: point: that query answers "what should the console show", so it hides
#: symptoms and hides shelved rows. The exporter has to SEE both in order to
#: decide about them, and the policy's reasons ("it is a symptom", "it is
#: shelved") are only writable if the columns arrive.
_EXPORT_SELECT = """
    SELECT a.id::text, a.device_id::text AS device_id, a.alarm_type, a.instance,
           a.severity::text AS severity, a.state::text AS state, a.message,
           a.metric_key, a.trigger_value::float8 AS trigger_value,
           a.threshold::float8 AS threshold, a.source,
           a.first_seen, a.last_seen, a.cleared_at, a.occurrence_count,
           a.acknowledged_at, a.acknowledged_by,
           a.category, a.detection, a.response_class,
           a.is_symptom, a.root_cause_alarm_id::text AS root_cause_alarm_id,
           (a.shelved_by_window IS NOT NULL) AS shelved,
           d.name AS device_name, d.device_type, d.mgmt_ip::text AS mgmt_ip,
           d.serial_number, d.asset_tag,
           dc.code AS datacenter_code, rm.name AS room_name, r.name AS rack_name,
           d.u_start
      FROM alarm a
      LEFT JOIN device d      ON d.id = a.device_id
      LEFT JOIN rack r        ON r.id = d.rack_id
      LEFT JOIN rack_row rr   ON rr.id = r.row_id
      LEFT JOIN room rm       ON rm.id = COALESCE(rr.room_id, d.room_id)
      LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
"""


async def alarms_for_export(session: AsyncSession, alarm_ids: list[str]
                            ) -> dict[str, dict[str, Any]]:
    """The wide rows for a batch of alarm ids, keyed by id.

    One query per batch rather than per alarm. The alarm actions that reach
    the exporter carry a deliberately thin dict - raise_alarm's RETURNING - so
    somebody has to widen them, and doing it here means the hot path pays one
    round trip for a whole tick's worth of alarms.
    """
    if not alarm_ids:
        return {}
    rows = (await session.execute(
        text(_EXPORT_SELECT + " WHERE a.id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": alarm_ids})).mappings().all()
    return {r["id"]: dict(r) for r in rows}


# ------------------------------------------------------------------- outbox

async def enqueue(session: AsyncSession, rows: list[dict[str, Any]]) -> int:
    """Append intents. Participates in the caller's transaction - never commits.

    That is the whole reason this table exists: the alarm and the intent to
    ticket it land together or not at all.
    """
    if not rows:
        return 0
    await session.execute(text("""
        INSERT INTO integration_outbox
               (integration_id, kind, fingerprint, alarm_id, payload)
        VALUES (CAST(:integration_id AS uuid), :kind, :fingerprint,
                CAST(:alarm_id AS uuid), CAST(:payload AS jsonb))
    """), [{"integration_id": r["integration_id"], "kind": r["kind"],
            "fingerprint": r["fingerprint"], "alarm_id": r.get("alarm_id"),
            "payload": json.dumps(r["payload"], default=str)} for r in rows])
    return len(rows)


async def claim(session: AsyncSession, *, consumer: str, limit: int
                ) -> list[dict[str, Any]]:
    """Take up to ``limit`` due rows for this worker, and nobody else's.

    SKIP LOCKED is what makes two ingest workers safe. Without it the second
    worker blocks on the first's row locks and then processes the same rows
    after they commit, which is not a duplicate post only because the state
    check happens to filter them - a correctness argument nobody should have
    to make.

    `claimed_by IS NULL` is the other half, and SKIP LOCKED does not survive
    without it. The claim commits before the HTTP call - it has to, because a
    Jira call must never run inside a transaction - and at that moment the row
    is unlocked and still `pending`, so the next worker's tick selects the very
    row somebody is mid-POST on. Both create a ticket for the same condition
    seconds apart, and the recovery search only catches it when Jira's search
    index happens to have caught up in between.

    Every terminal path already clears `claimed_by`, and
    `release_stale_claims` exists to hand back rows a worker died holding -
    both of which are only meaningful if a claimed row is off limits until
    then. This is the clause that makes that true.

    Ordered by id, which is the delivery order: a clear must never overtake
    its own raise, and `id` is the only monotonic column here (two rows can
    share `created_at` to the microsecond when one tick raises and clears).
    """
    rows = (await session.execute(text("""
        UPDATE integration_outbox SET claimed_by = :consumer, claimed_at = now(),
                                      attempts = attempts + 1
         WHERE id IN (
             SELECT id FROM integration_outbox
              WHERE state = 'pending' AND not_before <= now()
                AND claimed_by IS NULL
              ORDER BY id
              FOR UPDATE SKIP LOCKED
              LIMIT :limit)
        RETURNING id, integration_id::text AS integration_id, kind, fingerprint,
                  alarm_id::text AS alarm_id, window_id::text AS window_id,
                  payload, attempts, created_at
    """), {"consumer": consumer, "limit": limit})).mappings().all()
    return [dict(r) for r in rows]


async def mark_done(session: AsyncSession, row_id: int) -> None:
    await session.execute(text("""
        UPDATE integration_outbox
           SET state = 'done', last_error = NULL, claimed_by = NULL
         WHERE id = :id
    """), {"id": row_id})


async def mark_retry(session: AsyncSession, row_id: int, *,
                     not_before: datetime, error: str) -> None:
    await session.execute(text("""
        UPDATE integration_outbox
           SET not_before = :not_before, last_error = :error, claimed_by = NULL
         WHERE id = :id
    """), {"id": row_id, "not_before": not_before, "error": error[:2000]})


async def mark_dead(session: AsyncSession, row_id: int, *, error: str) -> None:
    await session.execute(text("""
        UPDATE integration_outbox
           SET state = 'dead', last_error = :error, claimed_by = NULL
         WHERE id = :id
    """), {"id": row_id, "error": error[:2000]})


async def release_stale_claims(session: AsyncSession, older_than_s: int = 300
                               ) -> int:
    """Hand back rows a worker claimed and then died holding.

    `attempts` was already incremented by the claim, so a crash loop still
    walks towards the dead state rather than retrying forever - which is the
    behaviour wanted: a row that reliably kills its worker is a row that needs
    looking at, not one that needs another go.
    """
    res = await session.execute(text("""
        UPDATE integration_outbox SET claimed_by = NULL
         WHERE state = 'pending' AND claimed_by IS NOT NULL
           AND claimed_at < now() - make_interval(secs => :age)
        RETURNING id
    """), {"age": older_than_s})
    return len(res.fetchall())


async def outbox_rows(session: AsyncSession, integration_id: str, *,
                      state: str | None = None, limit: int = 100,
                      offset: int = 0) -> list[dict[str, Any]]:
    where = ["integration_id = CAST(:id AS uuid)"]
    params: dict[str, Any] = {"id": integration_id, "limit": limit,
                              "offset": offset}
    if state:
        where.append("state = :state")
        params["state"] = state
    rows = (await session.execute(text(f"""
        SELECT id, kind, fingerprint, alarm_id::text AS alarm_id, state,
               attempts, created_at, not_before, last_error,
               payload -> 'device_name'  AS device_name,
               payload -> 'message'      AS message,
               payload -> 'severity'     AS severity
          FROM integration_outbox
         WHERE {' AND '.join(where)}
         ORDER BY id DESC
         LIMIT :limit OFFSET :offset
    """), params)).mappings().all()
    return [dict(r) for r in rows]


async def revive(session: AsyncSession, integration_id: str, row_id: int
                 ) -> bool:
    """Put one dead letter back in the queue, attempts reset.

    Reset rather than continued: a human pressing Retry has usually just fixed
    the thing that was wrong - created the custom field, corrected the project
    key - and making the row die again after one attempt because it had
    already used its eight would be indistinguishable from the fix not working.
    """
    res = await session.execute(text("""
        UPDATE integration_outbox
           SET state = 'pending', attempts = 0, not_before = now(),
               last_error = NULL, claimed_by = NULL
         WHERE id = :row AND integration_id = CAST(:id AS uuid) AND state = 'dead'
        RETURNING id
    """), {"row": row_id, "id": integration_id})
    return res.first() is not None


async def dead_count(session: AsyncSession) -> dict[str, int]:
    rows = (await session.execute(text("""
        SELECT integration_id::text AS integration_id, count(*) AS n
          FROM integration_outbox WHERE state = 'dead'
         GROUP BY integration_id
    """))).mappings().all()
    return {r["integration_id"]: int(r["n"]) for r in rows}


# --------------------------------------------------------------- jira links

async def links_for(session: AsyncSession, integration_id: str,
                    fingerprints: list[str]) -> dict[str, dict[str, Any]]:
    """Existing links for a batch of fingerprints, open or closed.

    Closed ones are returned too, because the reopen decision needs them: a
    query that filtered on `closed_at IS NULL` would make every recurrence
    look like a first sighting.
    """
    if not fingerprints:
        return {}
    rows = (await session.execute(text("""
        SELECT fingerprint, issue_key, issue_id, status, status_category,
               resolution, opened_at, closed_at, wont_reopen, push_count,
               last_pushed_at
          FROM jira_link
         WHERE integration_id = CAST(:id AS uuid) AND fingerprint = ANY(:fps)
    """), {"id": integration_id, "fps": fingerprints})).mappings().all()
    return {r["fingerprint"]: dict(r) for r in rows}


async def open_fingerprints(session: AsyncSession, integration_id: str,
                            fingerprints: list[str]) -> set[str]:
    """Which of these already have a ticket that is not closed.

    Used by the enqueue path to let follow-up actions past the policy: once a
    ticket exists you must be able to close it, whatever the policy has since
    been edited to say.
    """
    if not fingerprints:
        return set()
    rows = (await session.execute(text("""
        SELECT fingerprint FROM jira_link
         WHERE integration_id = CAST(:id AS uuid) AND fingerprint = ANY(:fps)
           AND closed_at IS NULL
    """), {"id": integration_id, "fps": fingerprints})).mappings().all()
    return {r["fingerprint"] for r in rows}


async def upsert_link(session: AsyncSession, *, integration_id: str,
                      fingerprint: str, issue_key: str,
                      issue_id: str | None = None,
                      alarm_id: str | None = None,
                      device_id: str | None = None,
                      alarm_type: str | None = None,
                      instance: str = "",
                      status: str | None = None,
                      status_category: str | None = None,
                      resolution: str | None = None,
                      closed_at: datetime | None = None,
                      wont_reopen: bool | None = None,
                      reopened: bool = False) -> dict[str, Any]:
    """Record or refresh the link.

    ``reopened`` clears `closed_at` and `resolution`; without it a reopen would
    leave the row looking closed and the next occurrence would try to reopen
    again, forever.
    """
    row = (await session.execute(text("""
        INSERT INTO jira_link (fingerprint, integration_id, issue_key, issue_id,
                               alarm_id, device_id, alarm_type, instance,
                               status, status_category,
                               resolution, closed_at, wont_reopen,
                               last_pushed_at, push_count)
        VALUES (:fingerprint, CAST(:integration_id AS uuid), :issue_key, :issue_id,
                CAST(:alarm_id AS uuid), CAST(:device_id AS uuid),
                :alarm_type, :instance, :status,
                :status_category, :resolution, :closed_at,
                COALESCE(:wont_reopen, false), now(), 1)
        ON CONFLICT (fingerprint) DO UPDATE SET
            integration_id  = EXCLUDED.integration_id,
            issue_key       = EXCLUDED.issue_key,
            issue_id        = COALESCE(EXCLUDED.issue_id, jira_link.issue_id),
            alarm_id        = COALESCE(EXCLUDED.alarm_id, jira_link.alarm_id),
            device_id       = COALESCE(EXCLUDED.device_id, jira_link.device_id),
            -- The condition's identity, which the inbound path resolves the
            -- CURRENT alarm through. COALESCE rather than overwrite: it never
            -- changes for a given fingerprint, and a push that happens not to
            -- carry it must not erase it.
            alarm_type      = COALESCE(EXCLUDED.alarm_type, jira_link.alarm_type),
            instance        = COALESCE(NULLIF(EXCLUDED.instance, ''),
                                       jira_link.instance),
            status          = COALESCE(EXCLUDED.status, jira_link.status),
            status_category = COALESCE(EXCLUDED.status_category,
                                       jira_link.status_category),
            resolution      = CASE WHEN :reopened THEN NULL
                                   ELSE COALESCE(EXCLUDED.resolution,
                                                 jira_link.resolution) END,
            closed_at       = CASE WHEN :reopened THEN NULL
                                   ELSE COALESCE(EXCLUDED.closed_at,
                                                 jira_link.closed_at) END,
            opened_at       = CASE WHEN :reopened THEN now()
                                   ELSE jira_link.opened_at END,
            wont_reopen     = COALESCE(:wont_reopen, jira_link.wont_reopen),
            last_pushed_at  = now(),
            push_count      = jira_link.push_count + 1
        RETURNING fingerprint, issue_key, issue_id, alarm_id::text AS alarm_id,
                  device_id::text AS device_id, alarm_type, instance,
                  status, status_category, resolution, opened_at, closed_at,
                  wont_reopen, push_count
    """), {"fingerprint": fingerprint, "integration_id": integration_id,
           "issue_key": issue_key, "issue_id": issue_id, "alarm_id": alarm_id,
           "device_id": device_id, "alarm_type": alarm_type,
           "instance": instance, "status": status,
           "status_category": status_category, "resolution": resolution,
           "closed_at": closed_at, "wont_reopen": wont_reopen,
           "reopened": reopened})).mappings().one()
    return dict(row)


async def link_for_alarm(session: AsyncSession, alarm_id: str
                         ) -> dict[str, Any] | None:
    """The ticket for one alarm, for the console's chip.

    Looked up by fingerprint through the alarm rather than by `alarm_id`,
    because the link belongs to the CONDITION: an alarm cleared yesterday and
    raised again this morning is a new row with the same ticket, and the chip
    should show it.
    """
    row = (await session.execute(text("""
        SELECT l.fingerprint, l.issue_key, l.status, l.status_category,
               l.resolution, l.opened_at, l.closed_at, i.base_url, i.name
          FROM alarm a
          JOIN jira_link l ON l.alarm_id = a.id
          JOIN integration i ON i.id = l.integration_id
         WHERE a.id = CAST(:id AS uuid)
         ORDER BY l.last_pushed_at DESC NULLS LAST
         LIMIT 1
    """), {"id": alarm_id})).mappings().first()
    return dict(row) if row else None


async def links_for_alarm_ids(session: AsyncSession, alarm_ids: list[str]
                              ) -> dict[str, dict[str, Any]]:
    if not alarm_ids:
        return {}
    rows = (await session.execute(text("""
        SELECT l.alarm_id::text AS alarm_id, l.issue_key, l.status_category,
               i.base_url
          FROM jira_link l
          JOIN integration i ON i.id = l.integration_id
         WHERE l.alarm_id = ANY(CAST(:ids AS uuid[]))
    """), {"ids": alarm_ids})).mappings().all()
    return {r["alarm_id"]: dict(r) for r in rows}


async def open_alarm_ids(session: AsyncSession, alarm_ids: list[str]) -> set[str]:
    """Which of these alarms are still open.

    Read by the dispatcher before it acts on a CREATE that has been waiting -
    held by the storm breaker, or stuck behind a long backoff. The payload is
    frozen by design, so without this check a condition that flapped an hour
    ago still opens a ticket for a fault that is over, and the storm breaker
    becomes a delayed flood rather than a brake.

    Deliberately not applied to fresh rows: a condition that raises and clears
    inside one dispatcher pass is a real flap, and the alarm engine's own
    dwell is where that is supposed to be damped.
    """
    if not alarm_ids:
        return set()
    rows = (await session.execute(text("""
        SELECT id::text FROM alarm
         WHERE id = ANY(CAST(:ids AS uuid[])) AND state <> 'CLEARED'
    """), {"ids": alarm_ids})).mappings().all()
    return {r["id"] for r in rows}


async def recent_links(session: AsyncSession, integration_id: str, *,
                       limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """Conditions that have a ticket, newest first, with the alarm beside them.

    LEFT JOIN on the alarm: a link outlives the alarm row it was opened for -
    the condition cleared, the row aged out - and dropping those would hide
    exactly the tickets that are still open with nothing behind them.
    """
    rows = (await session.execute(text("""
        SELECT l.fingerprint, l.issue_key, l.status, l.status_category,
               l.resolution, l.opened_at, l.closed_at, l.push_count,
               l.wont_reopen,
               a.alarm_type, a.severity::text AS severity, a.state::text AS state,
               d.name AS device_name
          FROM jira_link l
          LEFT JOIN alarm a  ON a.id = l.alarm_id
          LEFT JOIN device d ON d.id = l.device_id
         WHERE l.integration_id = CAST(:id AS uuid)
         ORDER BY l.opened_at DESC
         LIMIT :limit OFFSET :offset
    """), {"id": integration_id, "limit": limit,
           "offset": offset})).mappings().all()
    return [dict(r) for r in rows]


async def recent_alarms_for_preview(session: AsyncSession, *, days: int = 7,
                                    limit: int = 5000) -> list[dict[str, Any]]:
    """Alarms raised recently, in the shape the policy reads.

    Every row, including symptoms and shelved ones, because the preview's job
    is to show what each clause EXCLUDES as well as what gets through - and a
    query that pre-filtered them would make `exclude_symptoms` look like it
    does nothing.

    Capped rather than paged: this is a rehearsal, and a sample of five
    thousand answers "roughly how many tickets a day" exactly as well as the
    whole population would, at a cost the settings page can afford.
    """
    rows = (await session.execute(
        text(_EXPORT_SELECT + """
         WHERE a.first_seen > now() - make_interval(days => :days)
         ORDER BY a.first_seen DESC
         LIMIT :limit
        """), {"days": days, "limit": limit})).mappings().all()
    return [dict(r) for r in rows]


# ------------------------------------------------------------ inbound links

async def link_by_issue(session: AsyncSession, integration_id: str,
                        issue_key: str) -> dict[str, Any] | None:
    """The link a webhook is about.

    On the critical path of a request that must answer inside 30 seconds, so
    it reads the index `ix_jira_link_issue` and nothing else.
    """
    row = (await session.execute(text("""
        SELECT fingerprint, issue_key, issue_id, alarm_id::text AS alarm_id,
               device_id::text AS device_id, alarm_type, instance,
               status, status_category, resolution, opened_at, closed_at,
               wont_reopen
          FROM jira_link
         WHERE integration_id = CAST(:id AS uuid) AND issue_key = :key
    """), {"id": integration_id, "key": issue_key})).mappings().first()
    return dict(row) if row else None


async def record_inbound(session: AsyncSession, fingerprint: str, *,
                         status: str | None, status_category: str | None,
                         resolution: str | None, closed: bool,
                         reopened: bool = False,
                         wont_reopen: bool = False) -> None:
    """Write back what Jira last said about one ticket.

    `closed_at` is set on a close and NULLed on a reopen, and it is OURS: it
    records when we last saw the issue leave the open state, which is what the
    reopen window is measured from. Jira's own resolution date can disagree
    after a manual reopen-and-reclose, and when they disagree the window
    should follow what this platform actually observed.

    `wont_reopen` only ever goes true. A human declining a ticket is a
    decision, and a later automation touching the same issue must not quietly
    withdraw it.
    """
    await session.execute(text("""
        UPDATE jira_link
           SET status          = COALESCE(:status, status),
               status_category = COALESCE(:category, status_category),
               resolution      = CASE WHEN :reopened THEN NULL
                                      ELSE COALESCE(:resolution, resolution) END,
               closed_at       = CASE WHEN :closed   THEN COALESCE(closed_at, now())
                                      WHEN :reopened THEN NULL
                                      ELSE closed_at END,
               opened_at       = CASE WHEN :reopened THEN now() ELSE opened_at END,
               wont_reopen     = wont_reopen OR :wont_reopen,
               last_inbound_at = now()
         WHERE fingerprint = :fingerprint
    """), {"fingerprint": fingerprint, "status": status,
           "category": status_category, "resolution": resolution,
           "closed": closed, "reopened": reopened,
           "wont_reopen": wont_reopen})


async def drop_link(session: AsyncSession, fingerprint: str) -> bool:
    """Forget a ticket, so the next occurrence opens a new one.

    Used when the issue was deleted in Jira. Keeping the row would make every
    later action comment into a void and report success.
    """
    res = await session.execute(text("""
        DELETE FROM jira_link WHERE fingerprint = :fingerprint
        RETURNING fingerprint
    """), {"fingerprint": fingerprint})
    return res.first() is not None


# ------------------------------------------------------------------- inbox

async def enqueue_inbound(session: AsyncSession, *, integration_id: str,
                          event: str, issue_key: str | None,
                          payload: dict[str, Any], dedup_sha: str) -> bool:
    """Record one webhook delivery. False when it is a redelivery.

    ON CONFLICT DO NOTHING rather than a check-then-insert: Jira retries on
    anything that is not a 2xx, two workers can be handed the same retry at
    the same moment, and only the unique index can settle that.
    """
    res = await session.execute(text("""
        INSERT INTO integration_inbox (integration_id, event, issue_key,
                                       payload, dedup_sha)
        VALUES (CAST(:id AS uuid), :event, :issue_key,
                CAST(:payload AS jsonb), :sha)
        ON CONFLICT (dedup_sha) DO NOTHING
        RETURNING id
    """), {"id": integration_id, "event": event, "issue_key": issue_key,
           "payload": json.dumps(payload, default=str), "sha": dedup_sha})
    return res.first() is not None


async def claim_inbound(session: AsyncSession, *, consumer: str, limit: int
                        ) -> list[dict[str, Any]]:
    """Take due inbound rows for this worker.

    Ordered by id - oldest first - because an issue's events are only
    meaningful in order: a reopen followed by a close is not a close followed
    by a reopen, and delivering them the wrong way round leaves the link
    saying the opposite of what Jira shows.
    """
    rows = (await session.execute(text("""
        UPDATE integration_inbox SET claimed_by = :consumer, claimed_at = now(),
                                     attempts = attempts + 1
         WHERE id IN (
             SELECT id FROM integration_inbox
              WHERE state = 'pending'
              ORDER BY id
              FOR UPDATE SKIP LOCKED
              LIMIT :limit)
        RETURNING id, integration_id::text AS integration_id, event, issue_key,
                  payload, attempts, received_at
    """), {"consumer": consumer, "limit": limit})).mappings().all()
    return [dict(r) for r in rows]


async def mark_inbound(session: AsyncSession, row_id: int, *, state: str,
                       error: str | None = None) -> None:
    await session.execute(text("""
        UPDATE integration_inbox
           SET state = :state, last_error = :error, claimed_by = NULL
         WHERE id = :id
    """), {"id": row_id, "state": state,
           "error": error[:2000] if error else None})


async def release_stale_inbound(session: AsyncSession, older_than_s: int = 300
                                ) -> int:
    res = await session.execute(text("""
        UPDATE integration_inbox SET claimed_by = NULL
         WHERE state = 'pending' AND claimed_by IS NOT NULL
           AND claimed_at < now() - make_interval(secs => :age)
        RETURNING id
    """), {"age": older_than_s})
    return len(res.fetchall())


async def inbound_rows(session: AsyncSession, integration_id: str, *,
                       state: str | None = None, limit: int = 100,
                       offset: int = 0) -> list[dict[str, Any]]:
    where = ["integration_id = CAST(:id AS uuid)"]
    params: dict[str, Any] = {"id": integration_id, "limit": limit,
                              "offset": offset}
    if state:
        where.append("state = :state")
        params["state"] = state
    rows = (await session.execute(text(f"""
        SELECT id, event, issue_key, state, attempts, received_at, last_error
          FROM integration_inbox
         WHERE {' AND '.join(where)}
         ORDER BY id DESC
         LIMIT :limit OFFSET :offset
    """), params)).mappings().all()
    return [dict(r) for r in rows]


async def integration_by_webhook_token(session: AsyncSession, token: str
                                       ) -> dict[str, Any] | None:
    """The webhook endpoint's only lookup, before any body is parsed.

    Returns the webhook ciphertext under a neutral name, for the same reason
    `secrets_of` does: this module stays the only one that names the column.
    """
    row = (await session.execute(text("""
        SELECT id::text, name, enabled, config,
               webhook_secret_enc AS webhook_blob
          FROM integration
         WHERE webhook_token = :token
    """), {"token": token})).mappings().first()
    return dict(row) if row else None


async def webhooks_needing_refresh(session: AsyncSession, within_days: int = 7
                                   ) -> list[dict[str, Any]]:
    """Registrations about to lapse.

    Jira Cloud's DYNAMIC webhooks expire 30 days after registration and have
    to be extended. This is the single most likely way this feature dies
    quietly in production: nothing fails and nothing errors, the tickets
    simply stop answering back one month after somebody set it up.
    """
    rows = (await session.execute(text("""
        SELECT id::text, name, webhook_id, webhook_expires_at,
               EXTRACT(EPOCH FROM (webhook_expires_at - now())) / 86400.0
                   AS days_left
          FROM integration
         WHERE enabled AND webhook_id IS NOT NULL
           AND webhook_expires_at IS NOT NULL
           AND webhook_expires_at < now() + make_interval(days => :days)
         ORDER BY webhook_expires_at
    """), {"days": within_days})).mappings().all()
    return [dict(r) for r in rows]


async def touch_webhook_expiry(session: AsyncSession, integration_id: str,
                               expires_at) -> None:
    """Push a refreshed registration's deadline out, and nothing else.

    Separate from `set_webhook` on purpose: a refresh must not disturb the
    token or the secret, and a function that could would eventually be called
    by a sweep that had none to hand.
    """
    await session.execute(text("""
        UPDATE integration SET webhook_expires_at = :expires_at
         WHERE id = CAST(:id AS uuid)
    """), {"id": integration_id, "expires_at": expires_at})


# ----------------------------------------------------------- window outbox

#: The fingerprint a window's messages carry.
#:
#: Prefixed and not a hash, unlike a condition's. A window is already unique -
#: it has a primary key - and the fingerprint's whole job for an alarm is to
#: collapse many rows onto one identity, which a window does not need. The
#: prefix keeps the two populations apart in a column they share, and makes a
#: window's row obvious to anybody reading the table.
def window_fingerprint(window_id: str) -> str:
    return f"window:{window_id}"


async def enqueue_window(session: AsyncSession, *, integration_id: str,
                         window_id: str, kind: str,
                         payload: dict[str, Any]) -> None:
    """Record an intent to tell Jira something about a window.

    Same table, same dispatcher, same retry and the same storm brake as an
    alarm's - because the failure modes are identical and a second delivery
    path would only be a second place to get them wrong.
    """
    await session.execute(text("""
        INSERT INTO integration_outbox
               (integration_id, kind, fingerprint, window_id, payload)
        VALUES (CAST(:integration_id AS uuid), :kind, :fingerprint,
                CAST(:window_id AS uuid), CAST(:payload AS jsonb))
    """), {"integration_id": integration_id, "kind": kind,
           "fingerprint": window_fingerprint(window_id),
           "window_id": window_id,
           "payload": json.dumps(payload, default=str)})


async def enabled_integration(session: AsyncSession) -> dict[str, Any] | None:
    """The one integration a change request should be opened against.

    A window is not a condition: it has one change request, not one per
    integration, because a change advisory board is a place rather than a
    broadcast. The first enabled integration wins, and the caller may name a
    different one.

    A PAGING integration is passed over when a ticketing one exists. An
    Operations alert cannot hold a change request, and a board is not
    something to wake up.
    """
    rows = await active(session)
    ticketing = [r for r in rows if r["kind"] != "jsm_ops"]
    return (ticketing or rows)[0] if rows else None


# ------------------------------------------------------------ assets export

async def assets_state(session: AsyncSession, integration_id: str
                       ) -> dict[str, Any]:
    """The sync's own memory. An absent row is a sync that has never run."""
    row = (await session.execute(text("""
        SELECT integration_id::text AS integration_id, workspace_id, schema_id,
               type_map, import_id, last_cursor, last_run_at, last_run_status,
               last_error, objects_pushed, runs,
               (import_token_enc IS NOT NULL) AS import_token_set
          FROM assets_sync_state WHERE integration_id = CAST(:id AS uuid)
    """), {"id": integration_id})).mappings().first()
    if row is None:
        return {"integration_id": integration_id, "workspace_id": None,
                "schema_id": None, "type_map": {}, "import_id": None,
                "last_cursor": None, "last_run_at": None,
                "last_run_status": None, "last_error": None,
                "objects_pushed": 0, "runs": 0, "import_token_set": False}
    return dict(row)


async def set_assets_config(session: AsyncSession, integration_id: str, *,
                            schema_id: str | None = None,
                            import_id: str | None = None,
                            import_token_blob: bytes | None = None) -> None:
    """What the operator configured. Upserted, because the row is created by
    whichever of configuring and running happens first."""
    await session.execute(text("""
        INSERT INTO assets_sync_state (integration_id, schema_id, import_id,
                                       import_token_enc)
        VALUES (CAST(:id AS uuid), :schema_id, :import_id, :blob)
        ON CONFLICT (integration_id) DO UPDATE SET
            schema_id        = COALESCE(EXCLUDED.schema_id,
                                        assets_sync_state.schema_id),
            import_id        = COALESCE(EXCLUDED.import_id,
                                        assets_sync_state.import_id),
            import_token_enc = COALESCE(EXCLUDED.import_token_enc,
                                        assets_sync_state.import_token_enc)
    """), {"id": integration_id, "schema_id": schema_id,
           "import_id": import_id, "blob": import_token_blob})


async def assets_secret(session: AsyncSession, integration_id: str
                        ) -> bytes | None:
    """The import configuration's ciphertext, under a neutral name.

    Same discipline as `secrets_of`: this module stays the only one in the
    application that names an encrypted column.
    """
    row = (await session.execute(text("""
        SELECT import_token_enc AS blob FROM assets_sync_state
         WHERE integration_id = CAST(:id AS uuid)
    """), {"id": integration_id})).mappings().first()
    return bytes(row["blob"]) if row and row["blob"] else None


async def cache_type_map(session: AsyncSession, integration_id: str, *,
                         workspace_id: str, type_map: dict[str, Any]) -> None:
    await session.execute(text("""
        INSERT INTO assets_sync_state (integration_id, workspace_id, type_map)
        VALUES (CAST(:id AS uuid), :workspace_id, CAST(:type_map AS jsonb))
        ON CONFLICT (integration_id) DO UPDATE SET
            workspace_id = EXCLUDED.workspace_id,
            type_map     = EXCLUDED.type_map
    """), {"id": integration_id, "workspace_id": workspace_id,
           "type_map": json.dumps(type_map)})


async def finish_assets_run(session: AsyncSession, integration_id: str, *,
                            status: str, pushed: int,
                            cursor: Any = None,
                            error: str | None = None) -> None:
    """Record the outcome, and advance the cursor ONLY on success.

    A run that dies half way leaves the cursor where it was, so the next one
    re-sends the devices it had already pushed. Re-sending is free - the
    import matches on the object key and updates - while advancing early
    loses a machine until somebody touches it again.
    """
    await session.execute(text("""
        INSERT INTO assets_sync_state (integration_id, last_run_at,
                                       last_run_status, last_error,
                                       objects_pushed, runs, last_cursor)
        VALUES (CAST(:id AS uuid), now(), :status, :error, :pushed, 1, :cursor)
        ON CONFLICT (integration_id) DO UPDATE SET
            last_run_at     = now(),
            last_run_status = EXCLUDED.last_run_status,
            last_error      = EXCLUDED.last_error,
            objects_pushed  = EXCLUDED.objects_pushed,
            runs            = assets_sync_state.runs + 1,
            last_cursor     = CASE WHEN :status = 'ok' THEN :cursor
                                   ELSE assets_sync_state.last_cursor END
    """), {"id": integration_id, "status": status, "pushed": pushed,
           "error": (error or "")[:2000] or None, "cursor": cursor})


async def inventory_for_assets(session: AsyncSession, *, since: Any = None
                               ) -> dict[str, Any]:
    """Everything the export pushes, and the cursor it reached.

    The PARENTS are always sent whole. There are a few dozen datacentres,
    rooms, racks, vendors and models between them, and sending only the
    changed ones would leave a device whose rack was created since the last
    run pointing at an object that does not exist - which Assets accepts
    silently, leaving the tree with no branches.

    Only DEVICES are incremental, because they are the population that is
    large and the only one that changes often.
    """
    params: dict[str, Any] = {"since": since}
    device_where = "WHERE d.updated_at > :since" if since else ""

    datacenters = (await session.execute(text("""
        SELECT id::text, code, name FROM datacenter ORDER BY code
    """))).mappings().all()
    rooms = (await session.execute(text("""
        SELECT r.id::text, r.name, r.datacenter_id::text AS datacenter_key
          FROM room r ORDER BY r.name
    """))).mappings().all()
    racks = (await session.execute(text("""
        SELECT rk.id::text, rk.name, rk.u_height,
               -- Through the ROW, and only through the row. A rack has no
               -- room_id of its own; the obvious COALESCE against one is a
               -- column that does not exist.
               rr.room_id::text AS room_key
          FROM rack rk
          LEFT JOIN rack_row rr ON rr.id = rk.row_id
         ORDER BY rk.name
    """))).mappings().all()
    vendors = (await session.execute(text("""
        SELECT id::text, name FROM vendor ORDER BY name
    """))).mappings().all()
    models = (await session.execute(text("""
        SELECT m.id::text, m.name, m.vendor_id::text AS vendor_key, m.u_height
          FROM model m ORDER BY m.name
    """))).mappings().all()

    devices = (await session.execute(text(f"""
        SELECT d.id::text, d.name, d.device_type, d.serial_number, d.asset_tag,
               d.model_id::text AS model_key,
               d.rack_id::text AS rack_key,
               COALESCE(rr.room_id, d.room_id)::text AS room_key,
               d.u_start, d.lifecycle::text AS lifecycle,
               d.commissioned_at, d.warranty_expires, d.purchase_order,
               d.updated_at
          FROM device d
          LEFT JOIN rack rk ON rk.id = d.rack_id
          LEFT JOIN rack_row rr ON rr.id = rk.row_id
        {device_where}
         ORDER BY d.updated_at
    """), params)).mappings().all()

    # The high-water mark is the newest row SENT, not now(): a device updated
    # while the run was in flight must be picked up next time rather than
    # skipped because the clock moved past it.
    cursor = devices[-1]["updated_at"] if devices else since

    return {
        "inventory": {
            "datacenter": [dict(r) for r in datacenters],
            "room": [dict(r) for r in rooms],
            "rack": [dict(r) for r in racks],
            "vendor": [dict(r) for r in vendors],
            "model": [dict(r) for r in models],
            "device": [dict(r) for r in devices],
        },
        "cursor": cursor,
        "devices_changed": len(devices),
    }
