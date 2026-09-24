"""Devices whose physical state has moved ahead of their record.

Not discovery. Discovery answers "something is answering that appears nowhere in
inventory" (migration 0012); everything here is a device the DCIM already knows
about, already has a reserved rack unit for, and is already polling. What has
changed is the HARDWARE, and the record has not caught up.

The two signals arrive separately, which is the whole reason this is possible:

  the management plane answers      somebody racked it, cabled the BMC into the
  while the record says planned     OOB switch and powered it on. A BMC runs on
  or in_stock                       standby power and comes up long before any
                                    OS, so this is the earliest physical evidence
                                    the DCIM can have that a box has landed.

  the OS agent answers while        it has been imaged and is reporting. Paired
  the record says installed         with enough time in `installed` for the soak
                                    to have run, it is a candidate for acceptance.

  anything answers while the        a discrepancy. Either the box was never
  record says decommissioned        pulled, or it was re-racked without anybody
  or retired                        recording it. No transition is proposed -
                                    somebody has to go and look.

WHY THIS PROPOSES AND NEVER APPLIES. An OS agent answering is not evidence that
anybody ACCEPTED the machine: acceptance is a cut-over with a change record and a
workload owner, and a poller cannot know that happened. Advancing automatically
would also un-shelve a commissioning machine's alarms at exactly the moment its
soak was most likely to be failing, and it would make the DCIM's record derived
from the wire - inverting the ownership the importer fix was about. Same rule
migration 0012 states for discovery: it guesses, inventory is a record of fact.

So every row carries a PROPOSED state that an operator confirms, and the
confirmation goes through the ordinary transition endpoint, which means it is
still checked against TRANSITIONS and still writes its event and its audit row.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: Default hours a machine must have been `installed` before acceptance is
#: offered. A burn-in is 24-48h of thermal and power soak; 48 is the
#: conservative end, and the caller may lower it.
DEFAULT_SOAK_HOURS = 48

#: What each signal proposes. Every one of these is a legal move in
#: `repositories.lifecycle.TRANSITIONS` - a queue that offered a transition the
#: matrix refuses would put a 409 behind a button, and the test suite asserts it.
PROPOSED = {
    "racked": "installed",
    "ready": "in_service",
    "discrepancy": None,
}

_QUERY = """
WITH ep AS (
    -- One row per endpoint with its live comm state. Disabled endpoints are
    -- excluded for the same reason the collector excludes them: nobody is
    -- polling them, so their silence means nothing.
    SELECT e.device_id,
           e.role::text     AS role,
           e.protocol::text AS protocol,
           host(e.address)  AS address,
           s.status::text   AS status,
           s.last_success
      FROM device_endpoint e
      JOIN endpoint_state s ON s.endpoint_id = e.id
     WHERE e.enabled AND e.admin_state = 'enabled'
), agg AS (
    SELECT device_id,
           bool_or(status = 'ONLINE' AND role = 'bmc')       AS bmc_up,
           bool_or(status = 'ONLINE' AND role = 'os_agent')  AS os_up,
           bool_or(status = 'ONLINE')                        AS any_up,
           max(last_success)                                 AS last_success,
           -- The evidence an operator reads: which endpoint is talking.
           (array_agg(role || ':' || protocol ORDER BY role)
            FILTER (WHERE status = 'ONLINE'))[1:4]           AS live_roles
      FROM ep
     GROUP BY device_id
), staged AS (
    -- When the device last changed lifecycle state. The soak clock starts when
    -- it was racked, which is a business event, so it is read from the event log
    -- rather than from any telemetry timestamp. Falls back to updated_at for a
    -- device whose history predates migration 0045.
    SELECT DISTINCT ON (device_id) device_id, ts
      FROM device_lifecycle_event
     ORDER BY device_id, ts DESC, id
)
SELECT d.id::text                        AS device_id,
       d.name,
       d.device_type,
       d.lifecycle::text                 AS lifecycle,
       d.serial_number,
       d.asset_tag,
       host(d.mgmt_ip)                   AS mgmt_ip,
       dc.code                           AS datacenter_code,
       rm.name                           AS room_name,
       rk.name                           AS rack_name,
       d.u_start,
       a.bmc_up, a.os_up, a.any_up,
       a.last_success,
       a.live_roles,
       COALESCE(st.ts, d.updated_at)     AS state_since,
       round(EXTRACT(EPOCH FROM (now() - COALESCE(st.ts, d.updated_at)))
             / 3600.0, 1)                AS hours_in_state,
       CASE
         -- Racked: the management plane is answering for something the record
         -- says has not been installed. Checked FIRST because it is the earliest
         -- and least ambiguous signal.
         WHEN d.lifecycle::text IN ('planned', 'in_stock') AND a.any_up
              THEN 'racked'
         -- Ready to accept: imaged and reporting, and soaked long enough. A
         -- server needs its OS agent specifically; a switch or a piece of plant
         -- has no separate OS to deploy, so any live endpoint counts.
         WHEN d.lifecycle::text = 'installed'
              AND (a.os_up OR (d.device_type <> 'server' AND a.any_up))
              AND EXTRACT(EPOCH FROM (now() - COALESCE(st.ts, d.updated_at)))
                  >= :soak_seconds
              THEN 'ready'
         -- Answering while the record says it is gone.
         WHEN d.lifecycle::text IN ('decommissioned', 'retired') AND a.any_up
              THEN 'discrepancy'
       END                               AS signal
  FROM device d
  JOIN agg a ON a.device_id = d.id
  LEFT JOIN staged st ON st.device_id = d.id
  LEFT JOIN rack rk    ON rk.id = d.rack_id
  LEFT JOIN rack_row rr ON rr.id = rk.row_id
  LEFT JOIN room rm    ON rm.id = COALESCE(rr.room_id, d.room_id)
  LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
"""


async def ready_queue(session: AsyncSession, *,
                      soak_hours: int = DEFAULT_SOAK_HOURS,
                      limit: int = 500) -> list[dict[str, Any]]:
    """Every device whose wire has moved ahead of its record.

    Ordered by signal so the earliest step in the path comes first - a rack full
    of boxes that just landed is more urgent than one machine waiting on a
    signature - and within a signal by how long it has been waiting.
    """
    rows = (await session.execute(text(f"""
        SELECT * FROM ({_QUERY}) q
         WHERE q.signal IS NOT NULL
         ORDER BY CASE q.signal WHEN 'discrepancy' THEN 0
                                WHEN 'racked' THEN 1
                                ELSE 2 END,
                  q.hours_in_state DESC NULLS LAST, q.name
         LIMIT :limit
    """), {"soak_seconds": soak_hours * 3600, "limit": limit})).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["proposed_state"] = PROPOSED.get(d["signal"])
        d["hours_in_state"] = (float(d["hours_in_state"])
                               if d["hours_in_state"] is not None else None)
        d["live_roles"] = list(d["live_roles"] or [])
        out.append(d)
    return out


async def counts(session: AsyncSession, *,
                 soak_hours: int = DEFAULT_SOAK_HOURS) -> dict[str, int]:
    """One row per signal, for the navigation badge.

    Separate from `ready_queue` rather than derived from it: the badge is fetched
    with every asset page and must not pull 500 rows to print a number.
    """
    rows = (await session.execute(text(f"""
        SELECT q.signal, count(*) AS n FROM ({_QUERY}) q
         WHERE q.signal IS NOT NULL
         GROUP BY q.signal
    """), {"soak_seconds": soak_hours * 3600})).mappings().all()
    out = dict.fromkeys(PROPOSED, 0)
    for r in rows:
        out[r["signal"]] = int(r["n"])
    out["total"] = sum(out[k] for k in PROPOSED)
    return out
