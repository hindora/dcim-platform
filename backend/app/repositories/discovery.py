"""Discovery runs and candidate staging."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def create_run(session: AsyncSession, *, method: str,
                     scope: dict[str, Any],
                     schedule_id: str | None = None,
                     schedule_label: str | None = None,
                     collector_id: str | None = None,
                     range_ids: list[str] | None = None) -> dict[str, Any]:
    # `trigger` and the label are the run's OWN record of who asked: schedule_id is
    # nulled when the schedule is deleted, and was the only trace (0081).
    # `collector_id` is who must do it (0082): NULL means any collector may.
    row = (await session.execute(text("""
        INSERT INTO discovery_run (method, scope, status, schedule_id, trigger,
                                   schedule_label, collector_id, range_ids)
        VALUES (:method, CAST(:scope AS jsonb), 'pending', CAST(:schedule AS uuid),
                :trigger, :label, :collector, CAST(:ranges AS uuid[]))
        RETURNING id::text, method, scope, status, started_at, trigger, collector_id
    """), {"method": method, "scope": json.dumps(scope),
           "schedule": schedule_id,
           "trigger": "schedule" if schedule_id else "manual",
           "label": schedule_label, "collector": collector_id,
           "ranges": list(range_ids or [])})).mappings().first()
    return dict(row)


async def run_in_flight(session: AsyncSession,
                        lanes: list[str | None] | None = None) -> bool:
    """Whether a sweep is queued or running - on these collectors, or anywhere.

    Per collector, because that is where one-at-a-time matters: a collector sweeps
    its runs one after another, and the agents on ITS network answer through the
    same listeners. A long sweep on DC1's collector used to hold back every
    schedule, DC2's included, for no reason at all.

    `None` in `lanes` is the shared lane - runs assigned to no collector, which
    whichever collector asks first takes.
    """
    if lanes is None:
        return bool((await session.execute(text("""
            SELECT EXISTS (SELECT 1 FROM discovery_run
                            WHERE status IN ('pending', 'running'))
        """))).scalar_one())
    ids = [lane for lane in lanes if lane]
    return bool((await session.execute(text("""
        SELECT EXISTS (SELECT 1 FROM discovery_run
                        WHERE status IN ('pending', 'running')
                          AND (collector_id = ANY(CAST(:ids AS text[]))
                               OR (CAST(:shared AS boolean) AND collector_id IS NULL)))
    """), {"ids": ids, "shared": None in lanes})).scalar_one())


async def unfinished_runs(session: AsyncSession) -> list[dict[str, Any]]:
    """Every queued or running sweep, with how recently its collector was heard,
    locked so two API processes do not both time the same one out."""
    rows = (await session.execute(text("""
        SELECT r.id::text, r.status, r.scope, r.collector_id,
               extract(epoch FROM (clock_timestamp() - r.started_at)) AS queued_s,
               extract(epoch FROM (clock_timestamp() - r.claimed_at)) AS running_s,
               ci.id IS NOT NULL AS collector_registered,
               extract(epoch FROM (clock_timestamp() - ci.last_heartbeat))
                   AS collector_age_s,
               (SELECT min(extract(epoch FROM (clock_timestamp() - last_heartbeat)))
                  FROM collector_instance) AS freshest_collector_s
          FROM discovery_run r
          LEFT JOIN collector_instance ci ON ci.id = r.collector_id
         WHERE r.status IN ('pending', 'running')
         ORDER BY r.started_at
         FOR UPDATE OF r SKIP LOCKED
    """))).mappings().all()
    return [dict(r) for r in rows]


async def fail_run(session: AsyncSession, run_id: str, error: str) -> bool:
    """Close an unfinished run as failed. A late report is then refused by the
    results endpoint, which only takes one for a run still running."""
    return bool((await session.execute(text("""
        UPDATE discovery_run SET status = 'failed', finished_at = now(), error = :e
         WHERE id = CAST(:id AS uuid) AND status IN ('pending', 'running')
        RETURNING id
    """), {"id": run_id, "e": error})).first())


async def list_runs(session: AsyncSession, limit: int = 25) -> list[dict[str, Any]]:
    """Recent sweeps, each with what it actually concluded.

    `found` alone is not a result. "105 answered" says nothing about whether that
    is good news; "105 answered, 105 expected, 0 new, 1 moved" is the audit. The
    counts are per run rather than over open candidates, because the question is
    what THAT sweep saw - a later promotion should not rewrite the history of the
    run that found it.
    """
    rows = (await session.execute(text("""
        SELECT r.id::text, r.method, r.scope, r.status, r.found, r.promoted,
               r.started_at, r.claimed_at, r.finished_at, r.error,
               -- What the run concluded AS IT FINISHED. The live join below is
               -- only for runs recorded before the snapshot existed: it counts by
               -- each candidate's CURRENT run_id, which the next sweep of the same
               -- range moves - so an old run's counts shrank to zero the moment
               -- its range was swept again.
               COALESCE(r.known, c.known, 0)             AS known,
               COALESCE(r.unknown, c.unknown, 0)         AS unknown,
               COALESCE(r.moved, c.moved, 0)             AS moved,
               COALESCE(r.with_serial, c.with_serial, 0) AS with_serial,
               r.appeared, r.gone, r.changed,
               r.schedule_id::text AS schedule_id, r.trigger,
               -- The schedule's name now, else what it was called when it fired.
               COALESCE(sch.name, r.schedule_label) AS schedule_name,
               -- Who must do it, and whether they are about. A run waiting on a
               -- collector nobody has heard from in an hour is not "queued", it
               -- is stuck, and the page has to be able to say so.
               r.collector_id,
               ci.id IS NOT NULL AS collector_registered,
               extract(epoch FROM (clock_timestamp() - ci.last_heartbeat))
                   AS collector_age_s,
               array(SELECT x::text FROM unnest(r.range_ids) x) AS range_ids
          FROM discovery_run r
          LEFT JOIN discovery_schedule sch ON sch.id = r.schedule_id
          LEFT JOIN collector_instance ci ON ci.id = r.collector_id
          LEFT JOIN (
            SELECT dc.run_id,
                   count(*) FILTER (WHERE dc.matched_device_id IS NOT NULL) AS known,
                   count(*) FILTER (WHERE dc.matched_device_id IS NULL)     AS unknown,
                   -- Matched to a device, at an address that device is not
                   -- recorded at: the box has MOVED and nobody wrote it down.
                   -- Only knowable because the serial matched; on address alone
                   -- this row would have counted as unknown.
                   count(*) FILTER (
                       WHERE dc.matched_device_id IS NOT NULL
                         AND d.mgmt_ip IS NOT NULL
                         AND host(d.mgmt_ip) <> host(dc.address))          AS moved,
                   count(dc.serial)                                        AS with_serial
              FROM discovery_candidate dc
              LEFT JOIN device d ON d.id = dc.matched_device_id
             GROUP BY dc.run_id
          ) c ON c.run_id = r.id
         ORDER BY r.started_at DESC LIMIT :limit
    """), {"limit": limit})).mappings().all()
    return [dict(r) for r in rows]


async def claim_pending(session: AsyncSession, collector_id: str | None = None,
                        supports_exclude: bool = False) -> dict[str, Any] | None:
    """Hand the oldest pending run THIS collector may do to it, exactly once.

    A run assigned to a collector goes to that collector only: the range is on a
    network only it reaches, and a sweep from anywhere else hears nothing and
    reports a site's worth of devices missing. A run assigned to nobody goes to
    whoever asks first.

    A run with exclusions goes only to a collector that says it honours them. One
    that predates exclusions would probe exactly the addresses somebody listed
    because probing them is harmful.

    SKIP LOCKED so two collectors cannot claim the same run: the second skips it
    rather than blocking, which is what you want when the work is a network sweep
    that must not be done twice.
    """
    row = (await session.execute(text("""
        UPDATE discovery_run SET status = 'running', claimed_at = now()
         WHERE id = (SELECT id FROM discovery_run
                      WHERE status = 'pending'
                        AND (collector_id IS NULL
                             OR collector_id = CAST(:collector AS text))
                        AND (CAST(:excl AS boolean)
                             OR jsonb_array_length(
                                  COALESCE(scope -> 'exclude', '[]'::jsonb)) = 0)
                      ORDER BY started_at
                      FOR UPDATE SKIP LOCKED
                      LIMIT 1)
        RETURNING id::text, method, scope, collector_id
    """), {"collector": collector_id, "excl": supports_exclude})).mappings().first()
    return dict(row) if row else None


async def record_skipped_run(session: AsyncSession, *, scope: dict[str, Any],
                             schedule_id: str | None, schedule_label: str | None,
                             collector_id: str | None, range_ids: list[str],
                             reason: str) -> str:
    """A scheduled sweep that did not happen, and why, as a run in the history.

    Recorded rather than silently skipped: "the 02:00 sweep did nothing because
    of the Q4 freeze" is exactly what somebody reading the history the next
    morning needs to see.
    """
    return (await session.execute(text("""
        INSERT INTO discovery_run (method, scope, status, schedule_id, trigger,
                                   schedule_label, collector_id, range_ids,
                                   finished_at, error)
        VALUES ('sweep', CAST(:scope AS jsonb), 'skipped', CAST(:schedule AS uuid),
                'schedule', :label, :collector, CAST(:ranges AS uuid[]), now(), :reason)
        RETURNING id::text
    """), {"scope": json.dumps(scope), "schedule": schedule_id, "label": schedule_label,
           "collector": collector_id, "ranges": range_ids,
           "reason": reason})).scalar_one()


async def lock_run_status(session: AsyncSession, run_id: str) -> str | None:
    """The run's status, with its row locked until the transaction ends.

    Taken before results are recorded, so a cancel and the collector's report
    cannot interleave: whichever commits first decides, and the other sees it.
    """
    return (await session.execute(text("""
        SELECT status FROM discovery_run WHERE id = CAST(:id AS uuid) FOR UPDATE
    """), {"id": run_id})).scalar()


async def cancel_run(session: AsyncSession, run_id: str, reason: str,
                     running_reason: str) -> dict[str, Any] | None:
    """Cancel a run that has not finished. Returns what it WAS, or None.

    Only pending and running runs: a finished run is history, and cancelling it
    would rewrite what a sweep that happened concluded.
    """
    row = (await session.execute(text("""
        WITH prev AS (
            SELECT id, status FROM discovery_run
             WHERE id = CAST(:id AS uuid) FOR UPDATE
        )
        UPDATE discovery_run r
           SET status = 'cancelled', finished_at = now(),
               error = CASE WHEN prev.status = 'running' THEN :running_reason
                            ELSE :reason END
          FROM prev
         WHERE r.id = prev.id AND prev.status IN ('pending', 'running')
        RETURNING r.id::text AS id, prev.status AS was, r.collector_id
    """), {"id": run_id, "reason": reason,
           "running_reason": running_reason})).mappings().first()
    return dict(row) if row else None


async def finish_run(session: AsyncSession, run_id: str, *, found: int,
                     status: str = "done", error: str | None = None,
                     counts: dict[str, int] | None = None) -> None:
    """Close a run, with what it concluded written onto it.

    The counts are a snapshot on purpose. Computed live they were a function of
    each candidate's current run_id, which later sweeps move, so history changed
    every time a range was re-swept.
    """
    c = counts or {}
    await session.execute(text("""
        UPDATE discovery_run
           SET status = :status, finished_at = now(), found = :found, error = :error,
               known = :known, unknown = :unknown, moved = :moved,
               with_serial = :with_serial, appeared = :appeared, gone = :gone,
               changed = :changed
         WHERE id = CAST(:id AS uuid)
    """), {"id": run_id, "status": status, "found": found, "error": error,
           **{k: c.get(k) for k in ("known", "unknown", "moved", "with_serial",
                                    "appeared", "gone", "changed")}})


async def match_addresses(session: AsyncSession,
                          addresses: list[str]) -> dict[str, dict[str, Any]]:
    """Which of these addresses inventory already knows.

    Checked against BOTH the device's management address and the addresses its
    endpoints are polled on. A device is frequently managed on one address and
    polled on another; matching on only one of them reports half the fleet as
    unmanaged, which is the fastest way to make an audit useless.
    """
    if not addresses:
        return {}
    rows = (await session.execute(text("""
        SELECT host(d.mgmt_ip) AS addr, d.id::text AS device_id, d.name
          FROM device d
         WHERE d.mgmt_ip IS NOT NULL
           AND host(d.mgmt_ip) = ANY(:addrs)
           AND d.lifecycle <> 'decommissioned'
        UNION
        SELECT host(e.address) AS addr, d.id::text AS device_id, d.name
          FROM device_endpoint e
          JOIN device d ON d.id = e.device_id
         WHERE e.address IS NOT NULL
           AND host(e.address) = ANY(:addrs)
           AND d.lifecycle <> 'decommissioned'
    """), {"addrs": addresses})).mappings().all()
    return {r["addr"]: {"device_id": r["device_id"], "name": r["name"]}
            for r in rows}


async def match_serials(session: AsyncSession,
                        serials: list[str]) -> dict[str, dict[str, Any]]:
    """Which of these serials inventory already knows, and at what address.

    The serial is the STRONGER key and the only one that survives a device being
    re-addressed. Matching on the address alone reported a moved machine as brand
    new, and promoting it created a second record for one physical box - the exact
    failure an audit exists to catch, produced by the audit.

    `known_address` comes back so the caller can say WHY it matched: "this is
    SW07, last known at 10.51.11.9" is an actionable sentence, and "this is SW07"
    on a responder at a different address is a confusing one.
    """
    serials = [s for s in serials if s]
    if not serials:
        return {}
    rows = (await session.execute(text("""
        SELECT d.serial_number AS serial, d.id::text AS device_id, d.name,
               host(d.mgmt_ip) AS known_address
          FROM device d
         WHERE d.serial_number = ANY(:serials)
           AND d.lifecycle <> 'decommissioned'
    """), {"serials": serials})).mappings().all()
    return {r["serial"]: dict(r) for r in rows}


async def upsert_candidate(session: AsyncSession, *, run_id: str, address: str,
                           protocol: str, identity: dict[str, Any],
                           matched_device_id: str | None,
                           serial: str | None = None,
                           suggested_device_type: str | None = None,
                           suggested_vendor: str | None = None
                           ) -> dict[str, Any] | None:
    """Record one responder.

    Conflicts on the open-candidate index, so rediscovering the same unmanaged
    address updates last_seen instead of growing a new row every sweep - and that
    index spans 'new' and 'gone', so a responder that comes back is resurrected in
    place rather than inserted beside the row it already has.

    Everything a probe READ is refreshed, including the suggestions derived from it.
    A sweep is the freshest thing anybody has about an address, so a row must never
    keep a guess that its own identity no longer supports.
    """
    row = (await session.execute(text("""
        INSERT INTO discovery_candidate
               (run_id, address, protocol, identity, matched_device_id, serial,
                suggested_device_type, suggested_vendor, status)
        VALUES (CAST(:run_id AS uuid), CAST(:address AS inet),
                CAST(:protocol AS protocol_t), CAST(:identity AS jsonb),
                CAST(:matched AS uuid), :serial, :dtype, :vendor, 'new')
        ON CONFLICT (address, protocol) WHERE status IN ('new', 'gone')
        DO UPDATE SET last_seen = now(),
                      -- Answering again undoes having gone quiet. Without this the
                      -- row would stay 'gone' while its last_seen advanced, which
                      -- reads as a device that is silent and being seen at once.
                      status = 'new',
                      run_id = EXCLUDED.run_id,
                      identity = EXCLUDED.identity,
                      serial = EXCLUDED.serial,
                      matched_device_id = EXCLUDED.matched_device_id,
                      -- Derived FROM the identity this statement is replacing, so
                      -- keeping the old one guarantees the row contradicts itself.
                      -- It did: responders whose sysDescr had been corrected still
                      -- read as the type their first sweep guessed, while every
                      -- address being seen for the first time classified correctly
                      -- in the same run.
                      suggested_device_type = EXCLUDED.suggested_device_type,
                      suggested_vendor = EXCLUDED.suggested_vendor
        -- xmax is zero on a row this statement INSERTED and non-zero on one it
        -- updated: the cheapest honest way to count what appeared this run.
        RETURNING id::text, (xmax = 0) AS inserted
    """), {"run_id": run_id, "address": address, "protocol": protocol,
           "identity": json.dumps(identity), "matched": matched_device_id,
           "serial": serial,
           "dtype": suggested_device_type, "vendor": suggested_vendor})
    ).mappings().first()
    return dict(row) if row else None


#: The addresses a sweep of `cidr` actually PROBES. The sweeper skips a CIDR's network
#: and broadcast address below /31 (collector Hosts()), so containment alone - which
#: includes both - calls a device on .31 "asked" by a /27 sweep that never sent it a
#: packet. That read as missing, and as gone.
def _probed(addr: str, cidr: str) -> str:
    return (f"({addr} <<= CAST({cidr} AS inet)"
            f" AND (masklen(CAST({cidr} AS inet)) >= 31"
            # host() on both sides: network() and broadcast() keep the /27 mask
            # and a stored address is /32, so comparing the inets would compare
            # the masks too and never exclude anything.
            f" OR (host({addr}) <> host(network(CAST({cidr} AS inet)))"
            f" AND host({addr}) <> host(broadcast(CAST({cidr} AS inet))))))")


async def mark_gone(session: AsyncSession, run_id: str) -> int:
    """Mark the candidates this run covered but did not see.

    The difference that makes this safe is between "not asked" and "asked and
    silent". A run records the subnets it swept, so a candidate whose address falls
    inside that scope and which this run did not touch has genuinely stopped
    answering; one outside the scope was never asked and must be left alone, or a
    sweep of the electrical plane would mark the whole IT plane gone.

    Identified by last_seen rather than by run_id: the upsert stamps last_seen =
    now() on every responder it records, so anything still older than this run's
    start was not among them. Comparing run_id would miss a candidate that this run
    saw and a later statement touched.

    Marked, not deleted. That a machine used to answer at an address is how somebody
    later works out what used to be there, and it is the only trace of a device that
    was removed without being decommissioned.
    """
    run = (await session.execute(text("""
        SELECT scope, COALESCE(claimed_at, started_at) AS started_at
          FROM discovery_run WHERE id = CAST(:id AS uuid)
    """), {"id": run_id})).mappings().first()
    if run is None:
        return 0
    subnets = list((run["scope"] or {}).get("subnets") or [])
    # Excluded addresses were never asked, so they cannot have gone quiet.
    exclude = list((run["scope"] or {}).get("exclude") or [])
    if not subnets:
        # A run with no recorded scope cannot say what it covered, and guessing
        # would mark devices gone on no evidence.
        return 0

    rows = (await session.execute(text(f"""
        UPDATE discovery_candidate
           SET status = 'gone'
         WHERE status = 'new'
           AND last_seen < :started
           AND address IS NOT NULL
           AND EXISTS (
                 SELECT 1 FROM unnest(CAST(:subnets AS text[])) AS s(cidr)
                  WHERE {_probed("address", "s.cidr")})
           AND NOT EXISTS (
                 SELECT 1 FROM unnest(CAST(:exclude AS text[])) AS x(cidr)
                  WHERE address <<= CAST(x.cidr AS inet))
        RETURNING host(address) AS address, protocol::text AS protocol
    """), {"started": run["started_at"], "subnets": subnets,
           "exclude": exclude})).mappings().all()
    return len(rows)


async def list_candidates(session: AsyncSession, *, run_id: str | None = None,
                          status: str | None = None,
                          unmatched_only: bool = False,
                          limit: int = 200) -> list[dict[str, Any]]:
    where = ["1=1"]
    params: dict[str, Any] = {"limit": limit}
    if run_id:
        where.append("c.run_id = CAST(:run_id AS uuid)")
        params["run_id"] = run_id
    if status:
        where.append("c.status = :status")
        params["status"] = status
    if unmatched_only:
        where.append("c.matched_device_id IS NULL")

    rows = (await session.execute(text(f"""
        SELECT c.id::text, c.run_id::text AS run_id, host(c.address) AS address,
               c.protocol::text AS protocol, c.identity, c.serial,
               c.suggested_device_type, c.suggested_vendor, c.suggested_model,
               c.matched_device_id::text AS matched_device_id,
               d.name AS matched_device_name,
               -- Why it matched. A responder recognised by its SERIAL at an
               -- address inventory did not expect has MOVED, and saying so is the
               -- difference between a useful audit line and a confusing one.
               host(d.mgmt_ip) AS matched_device_address,
               (c.serial IS NOT NULL AND d.serial_number = c.serial)
                   AS matched_on_serial,
               c.status, c.first_seen, c.last_seen,
               -- What this responder said differently from the last time, not yet
               -- reviewed. Kept per field so "serial changed" - a replaced box -
               -- is distinguishable from a firmware string moving.
               COALESCE((
                 SELECT json_agg(json_build_object(
                          'id', ch.id::text, 'field', ch.field,
                          'old', ch.old_value, 'new', ch.new_value,
                          'detected_at', ch.detected_at)
                          ORDER BY ch.detected_at)
                   FROM discovery_identity_change ch
                  WHERE ch.candidate_id = c.id AND ch.acknowledged_at IS NULL
               ), '[]'::json) AS changes
          FROM discovery_candidate c
          LEFT JOIN device d ON d.id = c.matched_device_id
         WHERE {' AND '.join(where)}
         ORDER BY (c.matched_device_id IS NULL) DESC, c.address
         LIMIT :limit
    """), params)).mappings().all()
    return [dict(r) for r in rows]


async def set_candidate_status(session: AsyncSession, candidate_id: str,
                               status: str) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        UPDATE discovery_candidate SET status = :status
         WHERE id = CAST(:id AS uuid)
        RETURNING id::text, host(address) AS address, protocol::text AS protocol,
                  identity, status
    """), {"id": candidate_id, "status": status})).mappings().first()
    return dict(row) if row else None


async def get_candidate(session: AsyncSession,
                        candidate_id: str) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        SELECT id::text, run_id::text AS run_id, host(address) AS address,
               protocol::text AS protocol, identity, suggested_device_type,
               suggested_vendor, matched_device_id::text AS matched_device_id,
               status
          FROM discovery_candidate WHERE id = CAST(:id AS uuid)
    """), {"id": candidate_id})).mappings().first()
    return dict(row) if row else None


async def resolve_catalog(session: AsyncSession, *, vendor: str | None,
                          model: str | None) -> tuple[str | None, str | None]:
    """Match a swept vendor and model to rows that already exist.

    MATCH ONLY - nothing is created. The importer creates vendors and models
    because its input is an authoritative export; discovery's input is a regex over
    a sysDescr and a string a BMC volunteered. Creating from that would fill the
    catalog with "Dell Inc.", "DELL" and "Dell" as three vendors, and every
    composition chart downstream would then have to cope with it.

    So an unrecognised vendor resolves to nothing and the operator sets it on the
    asset afterwards - which is a smaller job than merging duplicate catalog rows.
    """
    vendor_id = None
    if vendor:
        # Exact first, then a UNIQUE substring. Exact alone resolved almost
        # nothing, which the first live run showed plainly: the catalog says
        # "Dell Technologies", "Cisco Systems" and "APC by Schneider Electric"
        # while a sysDescr regex yields "Dell", "Cisco" and "Schneider Electric".
        #
        # Ambiguity is refused rather than guessed. If a short name matches two
        # vendors there is no way to know which, and attaching the wrong one is
        # worse than attaching none - a wrong vendor is a fact somebody will read
        # and believe, where a missing one is a gap they can fill.
        rows = (await session.execute(text("""
            SELECT id::text, name FROM vendor
             WHERE lower(name) = lower(:name)
                OR position(lower(:name) in lower(name)) > 0
             ORDER BY (lower(name) = lower(:name)) DESC, length(name)
             LIMIT 3
        """), {"name": vendor.strip()})).mappings().all()
        if len(rows) == 1 or (rows and rows[0]["name"].lower() == vendor.strip().lower()):
            vendor_id = rows[0]["id"]
    model_id = None
    if model and vendor_id:
        # Scoped to the vendor: "R7525" from two manufacturers is two models, and
        # a name-only match would attach the wrong one.
        rows = (await session.execute(text("""
            SELECT id::text, name FROM model
             WHERE vendor_id = CAST(:vid AS uuid)
               AND (lower(name) = lower(:name)
                    OR position(lower(:name) in lower(name)) > 0)
             ORDER BY (lower(name) = lower(:name)) DESC, length(name)
             LIMIT 3
        """), {"vid": vendor_id, "name": model.strip()})).mappings().all()
        if len(rows) == 1 or (rows and rows[0]["name"].lower() == model.strip().lower()):
            model_id = rows[0]["id"]
    return vendor_id, model_id


async def attachable_devices(session: AsyncSession, *,
                             device_type: str | None = None,
                             limit: int = 100) -> list[dict[str, Any]]:
    """Records a responder could BE: hardware that was requested and is expected.

    `planned` and `in_stock` only. Those are the states that mean "we are waiting
    for this box", and they are the ones holding a rack unit and a power budget
    that the arriving hardware should inherit rather than duplicate.

    Anything already `installed` or `in_service` is either this device - in which
    case the candidate matched it and promotion is refused - or a different one.
    """
    where = ["d.lifecycle::text IN ('planned', 'in_stock')"]
    params: dict[str, Any] = {"limit": limit}
    if device_type:
        where.append("d.device_type = :dtype")
        params["dtype"] = device_type
    rows = (await session.execute(text(f"""
        SELECT d.id::text, d.name, d.device_type, d.lifecycle::text AS lifecycle,
               rk.name AS rack_name, d.u_start, dc.code AS datacenter_code,
               rm.name AS room_name
          FROM device d
          LEFT JOIN rack rk     ON rk.id = d.rack_id
          LEFT JOIN rack_row rr ON rr.id = rk.row_id
          LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
          LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
         WHERE {" AND ".join(where)}
         ORDER BY d.name
         LIMIT :limit
    """), params)).mappings().all()
    return [dict(r) for r in rows]



# ------------------------------------------------------------ identity changes

#: A change here means the BOX changed: a different chassis serial, platform,
#: board UUID or model at the same address is hardware that was swapped, and
#: nobody recorded it. That needs somebody.
HARDWARE_FIELDS = frozenset({"serial", "sysObjectID", "uuid", "model", "vendor"})

async def prior_identities(session: AsyncSession, addresses: list[str]
                           ) -> dict[tuple[str, str], dict[str, Any]]:
    """What each open responder said LAST time, read before this run overwrites it.

    One query for the whole run rather than one per responder: a /24 is 254
    lookups otherwise, on the one path where a sweep's results are recorded.
    """
    if not addresses:
        return {}
    rows = (await session.execute(text("""
        SELECT id::text, host(address) AS address, protocol::text AS protocol,
               identity, serial, status
          FROM discovery_candidate
         WHERE status IN ('new', 'gone')
           AND host(address) = ANY(:addrs)
    """), {"addrs": addresses})).mappings().all()
    return {(r["address"], r["protocol"]): dict(r) for r in rows}


async def record_changes(session: AsyncSession, run_id: str,
                         changes: list[dict[str, Any]]) -> None:
    for ch in changes:
        await session.execute(text("""
            INSERT INTO discovery_identity_change
                   (candidate_id, run_id, field, old_value, new_value)
            VALUES (CAST(:cid AS uuid), CAST(:run AS uuid), :field, :old, :new)
        """), {"cid": ch["candidate_id"], "run": run_id, "field": ch["field"],
               "old": ch["old"], "new": ch["new"]})


async def acknowledge_changes(session: AsyncSession, candidate_ids: list[str],
                              actor: str | None) -> int:
    """Mark reviewed. Never deleted: when a serial changed is a date somebody may
    need later, long after they have agreed the change was expected."""
    rows = (await session.execute(text("""
        UPDATE discovery_identity_change
           SET acknowledged_at = now(), acknowledged_by = :actor
         WHERE candidate_id = ANY(CAST(:ids AS uuid[])) AND acknowledged_at IS NULL
        RETURNING id
    """), {"ids": candidate_ids, "actor": actor})).all()
    return len(rows)


# ----------------------------------------------------------- missing devices

#: Lifecycles in which a device is racked, powered and expected to answer. A
#: planned or in-stock device is not on the wire yet, and a decommissioned one is
#: not supposed to be - neither is "missing" for being silent.
EXPECTED_ON_WIRE = ("installed", "in_service", "maintenance")

#: What a sweep speaks. A device reached only over BACnet, Modbus or gNMI - a
#: chiller, a pump, a valve behind a gateway - cannot answer a sweep however
#: healthy it is, and calling it missing would fill the page with false alarms.
SWEPT_PROTOCOLS = ("snmp", "redfish")


async def missing_devices(session: AsyncSession) -> list[dict[str, Any]]:
    """Inventory that should have answered the last sweep of its range, and did not.

    The mirror of "not in inventory", and the half every reconciliation needs: a
    device on record whose address a sweep COVERED - so it was asked - and which
    that sweep did not hear from, on any of the addresses it is polled on.

    Judged against the LATEST finished run covering each address, so a device
    swept last week and fine today is not missing because of last week. And the
    polling state is carried beside it, because the two disagreeing is the
    diagnosis: silent to the sweep but ONLINE to the poller means the sweep's
    credentials or an ACL, not a dead box.
    """
    rows = (await session.execute(text(f"""
        WITH addr AS (
            -- Every address a device answers a SWEEPABLE protocol on.
            SELECT e.device_id, e.address, e.protocol::text AS protocol
              FROM device_endpoint e
             WHERE e.enabled AND e.admin_state = 'enabled'
               AND e.protocol::text = ANY(:protocols) AND e.address IS NOT NULL
        ), covered AS (
            -- The newest finished run whose scope contains one of them.
            SELECT DISTINCT ON (a.device_id)
                   a.device_id, host(a.address) AS address, r.id AS run_id,
                   COALESCE(r.claimed_at, r.started_at) AS began, r.finished_at,
                   s.cidr AS subnet
              FROM addr a
              JOIN discovery_run r ON r.status = 'done'
              JOIN LATERAL jsonb_array_elements_text(r.scope -> 'subnets') AS s(cidr)
                   ON {_probed("a.address", "s.cidr")}
             -- An excluded address was never asked, so silence there is not a
             -- finding - the device is simply outside what that run audited.
             WHERE NOT EXISTS (
                     SELECT 1 FROM jsonb_array_elements_text(
                              COALESCE(r.scope -> 'exclude', '[]'::jsonb)) AS x(cidr)
                      WHERE a.address <<= CAST(x.cidr AS inet))
             ORDER BY a.device_id, r.finished_at DESC
        ), polled AS (
            SELECT e.device_id,
                   bool_or(st.status::text = 'ONLINE') AS any_online,
                   (array_agg(st.status::text ORDER BY st.status::text))[1] AS a_state
              FROM device_endpoint e
              JOIN endpoint_state st ON st.endpoint_id = e.id
             WHERE e.enabled AND e.admin_state = 'enabled'
             GROUP BY e.device_id
        )
        SELECT d.id::text AS device_id, d.name, d.device_type,
               d.lifecycle::text AS lifecycle, d.serial_number AS serial, cv.address,
               cv.run_id::text AS run_id, cv.finished_at AS swept_at, cv.subnet,
               p.any_online, p.a_state AS polling_state,
               rk.name AS rack_name, rm.name AS room_name
          FROM covered cv
          JOIN device d ON d.id = cv.device_id
          LEFT JOIN polled p ON p.device_id = d.id
          LEFT JOIN rack rk ON rk.id = d.rack_id
          LEFT JOIN rack_row rr ON rr.id = rk.row_id
          LEFT JOIN room rm ON rm.id = COALESCE(rr.room_id, d.room_id)
         WHERE d.lifecycle::text = ANY(:lifecycles)
           AND NOT EXISTS (
                 -- Answered that run, or a later one, on ANY of its addresses -
                 -- or was matched by serial somewhere else, which is "moved",
                 -- not "missing".
                 SELECT 1 FROM discovery_candidate c
                  WHERE c.last_seen >= cv.began
                    AND (c.matched_device_id = d.id
                         OR c.address IN (SELECT a2.address FROM addr a2
                                           WHERE a2.device_id = d.id)))
         ORDER BY d.name
    """), {"protocols": list(SWEPT_PROTOCOLS),
           "lifecycles": list(EXPECTED_ON_WIRE)})).mappings().all()
    return [dict(r) for r in rows]


async def attention_counts(session: AsyncSession) -> dict[str, int]:
    """The two findings the nav badge needs beyond unrecorded and moved.

    Missing counts what the page counts: a device in maintenance is expected to go
    quiet, so it is listed and not badged. Replaced is a hardware field that
    changed on a responder still answering, by address - machines, like the rest
    of the badge.
    """
    missing = [m for m in await missing_devices(session)
               if m["lifecycle"] != "maintenance"]
    replaced = (await session.execute(text("""
        SELECT count(DISTINCT c.address)
          FROM discovery_candidate c
         WHERE c.status = 'new'
           AND EXISTS (SELECT 1 FROM discovery_identity_change ch
                        WHERE ch.candidate_id = c.id
                          AND ch.acknowledged_at IS NULL
                          AND ch.field = ANY(:hardware))
    """), {"hardware": sorted(HARDWARE_FIELDS)})).scalar_one()
    return {"missing": len(missing), "replaced": int(replaced or 0)}


# ---------------------------------------------------------------- schedules

async def list_schedules(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT s.id::text, s.name, s.interval_hours, s.enabled,
               to_char(s.run_at, 'HH24:MI') AS run_at, s.days, s.timezone,
               array(SELECT x::text FROM unnest(s.range_ids) x) AS range_ids,
               -- What it sweeps, by name and CIDR, in one read. Resolved live, so
               -- editing a range changes what its schedules say they cover.
               COALESCE((SELECT array_agg(g.name ORDER BY g.cidr)
                           FROM discovery_range g WHERE g.id = ANY(s.range_ids)),
                        '{}') AS range_names,
               COALESCE((SELECT array_agg(g.cidr::text ORDER BY g.cidr)
                           FROM discovery_range g WHERE g.id = ANY(s.range_ids)),
                        '{}') AS subnets,
               s.next_run_at, s.last_run_id::text AS last_run_id,
               s.created_by, s.created_at,
               r.status AS last_status, r.finished_at AS last_finished_at,
               r.found AS last_found, r.unknown AS last_unknown,
               r.moved AS last_moved, r.gone AS last_gone
          FROM discovery_schedule s
          LEFT JOIN discovery_run r ON r.id = s.last_run_id
         ORDER BY s.next_run_at
    """))).mappings().all()
    return [dict(r) for r in rows]


async def create_schedule(session: AsyncSession, *, name: str, range_ids: list[str],
                          interval_hours: int, first_run_at: Any | None,
                          actor: str | None, run_at: str | None = None,
                          days: list[int] | None = None,
                          timezone: str | None = None) -> dict[str, Any]:
    row = (await session.execute(text("""
        INSERT INTO discovery_schedule (name, range_ids, interval_hours, next_run_at,
                                        created_by, run_at, days, timezone)
        VALUES (:name, CAST(:ranges AS uuid[]), :interval,
                COALESCE(CAST(:first AS timestamptz), now()), :actor,
                CAST(CAST(:run_at AS text) AS time), CAST(:days AS smallint[]), :tz)
        RETURNING id::text, name, interval_hours, enabled, next_run_at,
                  to_char(run_at, 'HH24:MI') AS run_at, days, timezone
    """), {"name": name, "ranges": range_ids, "interval": interval_hours,
           "first": first_run_at, "actor": actor, "run_at": run_at, "days": days,
           "tz": timezone})).mappings().first()
    return dict(row)


async def get_schedule(session: AsyncSession, schedule_id: str) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        SELECT id::text, name, interval_hours, enabled, next_run_at,
               to_char(run_at, 'HH24:MI') AS run_at, days, timezone,
               array(SELECT x::text FROM unnest(range_ids) x) AS range_ids
          FROM discovery_schedule WHERE id = CAST(:id AS uuid)
    """), {"id": schedule_id})).mappings().first()
    return dict(row) if row else None


async def update_schedule(session: AsyncSession, schedule_id: str,
                          fields: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {"name": "name = :name",
               "enabled": "enabled = :enabled",
               "interval_hours": "interval_hours = :interval_hours",
               "range_ids": "range_ids = CAST(:range_ids AS uuid[])",
               "run_at": "run_at = CAST(CAST(:run_at AS text) AS time)",
               "days": "days = CAST(:days AS smallint[])",
               "timezone": "timezone = :timezone",
               "next_run_at": "next_run_at = CAST(CAST(:next_run_at AS text) AS timestamptz)"}
    sets = [allowed[k] for k in fields if k in allowed]
    if not sets:
        return None
    row = (await session.execute(text(f"""
        UPDATE discovery_schedule SET {", ".join(sets)}, updated_at = now()
         WHERE id = CAST(:id AS uuid)
        RETURNING id::text, name, interval_hours, enabled, next_run_at,
                  to_char(run_at, 'HH24:MI') AS run_at, days, timezone
    """), {"id": schedule_id, **fields})).mappings().first()
    return dict(row) if row else None


async def delete_schedule(session: AsyncSession, schedule_id: str) -> bool:
    return bool((await session.execute(text("""
        DELETE FROM discovery_schedule WHERE id = CAST(:id AS uuid) RETURNING id
    """), {"id": schedule_id})).first())


async def claim_due_schedules(session: AsyncSession,
                              limit: int = 25) -> list[dict[str, Any]]:
    """The due schedules, oldest first, locked so two API processes cannot both
    fire one.

    Several, not one: the oldest may be waiting on a busy collector, and a
    schedule for a different collector should not wait behind it. SKIP LOCKED for
    the same reason collectors use it to claim runs - the second process moves on
    rather than queueing a duplicate sweep.
    """
    rows = (await session.execute(text("""
        SELECT id::text, name, interval_hours,
               to_char(run_at, 'HH24:MI') AS run_at, days, timezone,
               array(SELECT x::text FROM unnest(range_ids) x) AS range_ids
          FROM discovery_schedule
         WHERE enabled AND next_run_at <= now()
         ORDER BY next_run_at
         FOR UPDATE SKIP LOCKED
         LIMIT :limit
    """), {"limit": limit})).mappings().all()
    return [dict(r) for r in rows]


async def advance_schedule(session: AsyncSession, schedule_id: str,
                           run_id: str | None, next_run_at: Any) -> None:
    """Move a schedule on to its next run. The caller computes when
    (schedule_time.next_after) - from NOW, so a missed slot runs late once."""
    await session.execute(text("""
        UPDATE discovery_schedule
           SET next_run_at = CAST(:next AS timestamptz),
               last_run_id = COALESCE(CAST(:run AS uuid), last_run_id),
               updated_at = now()
         WHERE id = CAST(:id AS uuid)
    """), {"id": schedule_id, "run": run_id, "next": next_run_at})
