"""Incidents: the cooling chain's groups, kept as rows (docs/29 section 4.6).

`app.alarms.cooling_correlation` decides which alarm explains which and marks
the alarm rows. This turns those marks into `anomaly_incident` rows a person
can open: one per root device, with its members, the findings on the same
chain, and a summary written from the facts rather than generated.

Two kinds:
- **device**: a cooling machine (or its feeder) with heat downstream of it.
  The root alarm is the machine's own; the members are what it explains.
- **room**: heat on three or more devices in one room with no cooling alarm
  behind any of it. Nothing is folded - there is no root alarm to fold under -
  but the console shows one "Hall A is warm" instead of a scatter of probes,
  and it is exactly the case equipment alarms miss: a containment door, a
  floor tile, a CRAH running but under-delivering.

Rebuilt from the alarm table each pass, so it never drifts from what the
console shows. Idempotent: the open-root unique index makes a second worker's
pass an update of the same row.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.alarms import cooling_correlation as cc
from app.core.logging import get_logger
from app.integrations.config import SEVERITY_RANK

log = get_logger("analytics.correlator")

#: A room incident needs this many hot devices.
ROOM_MIN_DEVICES = 3
#: ...whose alarms started within this long of each other.
ROOM_WINDOW_S = 1800

_TIER_WORDS = {"power": "feeder", "plant": "plant", "unit": "cooling unit",
               "zone": "rack and room sensors", "server": "IT equipment"}


def _label(alarm_type: str) -> str:
    return alarm_type.replace("_", " ")


def _worst(sevs: list[str]) -> str:
    return min(sevs, key=lambda s: SEVERITY_RANK.get(s, 9)) if sevs else "INFO"


def device_summary(root_name: str, root_type: str, members: list[dict[str, Any]],
                   findings: int, air_by_room: bool) -> tuple[str, str]:
    """Title and summary of a device-rooted incident, from the facts alone."""
    by_tier: dict[str, int] = defaultdict(int)
    for m in members:
        by_tier[m["tier_of_member"]] += 1
    folded = sum(1 for m in members if m["is_symptom"])
    shown = len(members) - folded
    parts = [f"{n} {_TIER_WORDS.get(t, t)}" for t, n in
             sorted(by_tier.items(), key=lambda kv: -cc.TIER_RANK.get(kv[0], 0))]
    title = f"{root_name}: {_label(root_type)}"
    summary = (f"{len(members)} downstream alarm{'s' if len(members) != 1 else ''} "
               f"({', '.join(parts)}) follow{'s' if len(members) == 1 else ''} "
               f"{root_name}'s {_label(root_type)}.")
    if folded:
        summary += f" {folded} folded under it."
    if shown:
        summary += (f" {shown} more severe than the cause kept visible, one of each kind; "
                    "the rest of the same kind are folded with it.")
    if findings:
        summary += f" {findings} anomaly finding{'s' if findings != 1 else ''} on the same chain."
    if air_by_room:
        summary += (" Racks are tied to the unit by room, not by a measured zone: "
                    "every unit in the hall is taken to serve every rack in it.")
    return title, summary


async def refresh_incidents(session: AsyncSession, graph: cc.CoolingGraph | None = None,
                            now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    graph = graph or await cc.load_graph(session)

    members = [dict(r) for r in (await session.execute(text("""
        SELECT s.id::text AS id, s.device_id::text AS device_id, s.alarm_type,
               s.severity::text AS severity, s.first_seen, s.is_symptom,
               s.attributes->'l5'->>'root' AS root
          FROM alarm s
         WHERE s.state <> 'CLEARED' AND s.attributes ? 'l5'
    """))).mappings().all()]
    root_ids = sorted({m["root"] for m in members})
    roots = {r["id"]: dict(r) for r in (await session.execute(text("""
        SELECT id::text AS id, device_id::text AS device_id, alarm_type,
               severity::text AS severity, state::text AS state, first_seen
          FROM alarm WHERE id = ANY(CAST(:ids AS uuid[]))
    """), {"ids": root_ids})).mappings().all()}

    # One incident per root DEVICE: a CRAH that raised both "stopped" and
    # "high temp" is one machine with one story.
    groups: dict[str, dict[str, Any]] = {}
    for m in members:
        root = roots.get(m["root"])
        if root is None:
            continue
        rdev = root["device_id"]
        g = groups.setdefault(rdev, {"roots": {}, "members": []})
        g["roots"][root["id"]] = root
        node = graph.nodes.get(m["device_id"])
        m["tier_of_member"] = (node.tier if node else None) or "server"
        g["members"].append(m)

    open_rows = (await session.execute(text("""
        SELECT root_key, tier, datacenter_id::text AS datacenter_id, room_id::text AS room_id,
               title
          FROM anomaly_incident WHERE state = 'open' AND root_key LIKE 'device:%'
    """))).mappings().all()
    open_titles = {r["root_key"]: r["title"] for r in open_rows}
    groups = _merge_by_scope(graph, groups, [dict(r) for r in open_rows])

    # Open, visible cooling alarms on units: candidate peers of a unit root.
    unit_alarms = [dict(r) for r in (await session.execute(text(cc._OPEN_VISIBLE)))
                   .mappings().all()
                   if (graph.nodes.get(r["device_id"]) is not None
                       and graph.nodes[r["device_id"]].tier == "unit"
                       and cc.root_eligible(r["alarm_type"], r["category"], "unit"))]

    findings = [dict(r) for r in (await session.execute(text("""
        SELECT id::text AS id, device_id::text AS device_id, first_seen
          FROM anomaly_finding
         WHERE state IN ('open', 'acknowledged', 'confirmed')
    """))).mappings().all()]

    seen_keys: set[str] = set()
    opened = updated = 0
    actions: list[tuple[str, dict[str, Any]]] = []
    common_on: set[str] = set()
    room_names = {r["id"]: r["name"] for r in (await session.execute(text(
        "SELECT id::text AS id, name FROM room"))).mappings().all()}
    for rdev, g in groups.items():
        # The incident's identity is the anchor device (its key stays while
        # the event lasts); its ROOT is the earliest alarm still open among
        # the machines merged into it - the one a reader should see first.
        open_roots = [r for r in g["roots"].values() if r["state"] != "CLEARED"]
        own = ([r for r in open_roots if r["device_id"] == rdev]
               or open_roots or list(g["roots"].values()))
        root = min(own, key=lambda r: (r["first_seen"], r["id"]))
        rnode = graph.nodes.get(rdev)
        # The TITLE names the anchor - the machine the common alarm stands on
        # and the incident's identity - with the anchor's own condition. It
        # used to name the root, which is re-picked every pass as the
        # earliest alarm still open: all-chillers run 5 (2026-11-06) opened
        # as CHL1 and, as the chillers recovered one by one, was titled for
        # whichever was last to clear, with the common alarm still on CHL1.
        # One event, one name. The root in the evidence stays the earliest
        # open alarm, which is what a reader should look at first.
        own_any = [r for r in g["roots"].values() if r["device_id"] == rdev]
        named = min(own_any, key=lambda r: (r["first_seen"], r["id"])) if own_any else None
        name_node = rnode or graph.nodes.get(root["device_id"])
        anc_tier = None
        for m in g["members"]:
            anc_tier = graph.ancestors(m["device_id"]).get(rdev) or anc_tier
        tier = anc_tier or (rnode.tier if rnode else None) or "unit"
        member_devs = {m["device_id"] for m in g["members"]}
        t0 = root["first_seen"].timestamp() - cc.LEAD_S
        linked = [f for f in findings
                  if f["first_seen"].timestamp() >= t0
                  and (f["device_id"] == rdev or f["device_id"] in member_devs
                       or rdev in graph.ancestors(f["device_id"]))]
        air = any(m["tier_of_member"] in ("zone", "server") for m in g["members"]) \
            and tier in ("unit", "plant", "power")
        title, summary = device_summary(name_node.name if name_node else rdev,
                                        (named or root)["alarm_type"], g["members"],
                                        len(linked), air)
        if named is None and f"device:{rdev}" in open_titles:
            # The anchor no longer carries a root of its own (every condition
            # that pointed at it has cleared or moved): keep the name the
            # incident already has rather than borrowing another machine's.
            title = open_titles[f"device:{rdev}"].split(" and ", 1)[0]
        # The other air units of the same room that went down with it. Each
        # is its own equipment condition and stays visible, but seven CRAHs
        # stopping together are one event, and usually one cause (their
        # feeder, their controls) that the incident should put side by side.
        peers = [p for p in unit_alarms
                 if p["device_id"] != rdev and rnode is not None
                 and graph.nodes.get(p["device_id"]) is not None
                 and graph.nodes[p["device_id"]].room_id == rnode.room_id
                 and graph.nodes[p["device_id"]].tier == "unit"
                 and abs(p["first_seen"].timestamp() - root["first_seen"].timestamp())
                 <= cc.LEAD_S]
        merged = [r for r in g["roots"].values() if r["device_id"] != rdev]
        if merged and tier in ("plant", "power"):
            n = len({r["device_id"] for r in merged}) + 1
            title += f" and {n - 1} more plant machine{'s' if n > 2 else ''}"
            summary = (f"{n} plant machines at the site stopped or faulted in sequence: the "
                       "standby units were called and did not hold. Look at what they share "
                       "(chilled-water flow, condenser water, power, controls) first. " + summary)
        peers += [{"id": r["id"], "device_id": r["device_id"]} for r in merged
                  if r["id"] not in {p["id"] for p in peers}]
        if peers and tier == "unit":
            n_units = len({p["device_id"] for p in peers}) + 1
            title += f" and {n_units - 1} more cooling unit{'s' if n_units > 2 else ''}"
            summary = (f"{n_units} cooling units in the room stopped or faulted within "
                       f"{cc.LEAD_S // 60} min of each other; look for what they share "
                       "(feeder, controls, chilled water) first. " + summary)
        evidence = {
            "v_l5": 1, "kind": "device",
            "root": {"alarm_id": root["id"], "alarm_type": root["alarm_type"],
                     "severity": root["severity"], "state": root["state"],
                     "first_seen": root["first_seen"].isoformat()},
            "other_root_alarms": [r["id"] for r in g["roots"].values() if r["id"] != root["id"]],
            "members_by_tier": {t: sum(1 for m in g["members"] if m["tier_of_member"] == t)
                                for t in {m["tier_of_member"] for m in g["members"]}},
            "folded": sum(1 for m in g["members"] if m["is_symptom"]),
            "worst": _worst([m["severity"] for m in g["members"]] + [root["severity"]]),
            "air_side": "room" if air else None,
            "finding_ids": [f["id"] for f in linked],
            "peer_alarm_ids": [p["id"] for p in peers],
            "by_room": _by_room(graph, g["members"], room_names),
        }
        key = f"device:{rdev}"
        seen_keys.add(key)
        alarm_ids = list(dict.fromkeys(
            list(g["roots"]) + [p["id"] for p in peers] + [m["id"] for m in g["members"]]))
        inc_id, created = await _upsert(
            session, key=key, tier=tier, root_device=rdev, root_alarm=root["id"],
            dc=rnode.datacenter_id if rnode else None, room=rnode.room_id if rnode else None,
            title=title, summary=summary, alarm_ids=alarm_ids,
            count=len(g["members"]) + len(linked), evidence=evidence,
            opened_at=root["first_seen"], now=now)
        opened += created
        updated += not created
        # ISA-18.2 flood suppression: the consequences are folded, and in their
        # place ONE common alarm carries the cascade at its worst member's
        # severity - the alarm an email rule and a ticket follow. Only for a
        # cascade (three devices or more): one hot rack under a CRAH alarm is
        # already said by the CRAH alarm.
        member_devs_all = {m["device_id"] for m in g["members"]}
        if len(member_devs_all) >= CASCADE_MIN_DEVICES:
            act = await _raise_common(session, rdev=rdev, tier=tier, title=title,
                                      severity=evidence["worst"], incident_id=inc_id,
                                      members=len(g["members"]), now=now)
            if act:
                actions.append(act)
            common_on.add(rdev)
        if linked:
            await session.execute(text("""
                UPDATE anomaly_finding SET incident_id = CAST(:inc AS uuid)
                 WHERE id = ANY(CAST(:ids AS uuid[]))
                   AND incident_id IS DISTINCT FROM CAST(:inc AS uuid)
            """), {"inc": inc_id, "ids": [f["id"] for f in linked]})

    # --- room: heat with nothing behind it ------------------------------------
    hot = [dict(r) for r in (await session.execute(text("""
        SELECT a.id::text AS id, a.device_id::text AS device_id, a.alarm_type,
               a.severity::text AS severity, a.first_seen
          FROM alarm a
         WHERE a.state <> 'CLEARED' AND NOT a.is_symptom
           AND a.shelved_by_window IS NULL
           AND NOT (coalesce(a.attributes, '{}'::jsonb) ? 'l5')
           AND a.alarm_type = ANY(:types)
    """), {"types": sorted(cc.THERMAL_SYMPTOMS)})).mappings().all()]
    # A recovering tail is not "no cooling alarm". Heat that appears within
    # an hour of a cooling root above it clearing is that outage's: a 2U box
    # whose CPU crosses its limit twenty minutes after the chillers restart
    # is still hot from the outage (all-chillers, 2026-11-05: three such
    # alarms in the network room opened "3 devices hot, no cooling alarm",
    # pointing the operator at airflow). Those alarms do not count toward a
    # room incident.
    tail_roots = await cc.recent_roots(session, hold=cc.LAG_S)

    def _in_tail(a: dict[str, Any]) -> bool:
        anc = graph.ancestors(a["device_id"])
        return any(r["device_id"] in anc
                   and cc.root_eligible(r["alarm_type"], r.get("category"),
                                        anc[r["device_id"]], r.get("instance") or "")
                   for r in tail_roots)

    by_room: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in hot:
        node = graph.nodes.get(a["device_id"])
        if node and node.room_id and node.tier in ("zone", "server") and not _in_tail(a):
            by_room[node.room_id].append(a)
    for room_id, alarms in by_room.items():
        alarms.sort(key=lambda a: a["first_seen"])
        latest = alarms[-1]["first_seen"].timestamp()
        recent = [a for a in alarms if latest - a["first_seen"].timestamp() <= ROOM_WINDOW_S]
        devs = {a["device_id"] for a in recent}
        if len(devs) < ROOM_MIN_DEVICES:
            continue
        # Not before a cooling root has had its chance. A rack probe trips
        # before the CRAH's own alarm finishes its dwell; calling that minute
        # "no cooling alarm" pointed the second hall-crahs run at airflow
        # while seven CRAHs were stopped.
        if now.timestamp() - recent[0]["first_seen"].timestamp() < cc.LEAD_S:
            continue
        room_name = await session.scalar(text(
            "SELECT name FROM room WHERE id = CAST(:r AS uuid)"), {"r": room_id}) or "room"
        any_node = graph.nodes[recent[0]["device_id"]]
        key = f"room:{room_id}"
        seen_keys.add(key)
        title = f"{room_name}: {len(devs)} devices hot, no cooling alarm"
        summary = (f"{len(recent)} heat alarms on {len(devs)} devices in {room_name} within "
                   f"{ROOM_WINDOW_S // 60} min, and no cooling unit or plant alarm to explain "
                   "them. Look for airflow rather than a failed machine: containment, "
                   "blanking, floor tiles, a unit running but under-delivering.")
        evidence = {"v_l5": 1, "kind": "room", "worst": _worst([a["severity"] for a in recent]),
                    "devices": sorted(devs)}
        _, created = await _upsert(
            session, key=key, tier="room", root_device=None, root_alarm=None,
            dc=any_node.datacenter_id, room=room_id, title=title, summary=summary,
            alarm_ids=[a["id"] for a in recent], count=len(recent), evidence=evidence,
            opened_at=recent[0]["first_seen"], now=now)
        opened += created
        updated += not created

    resolved = (await session.execute(text("""
        UPDATE anomaly_incident
           SET state = 'resolved', resolved_at = CAST(:now AS timestamptz),
               last_activity_at = CAST(:now AS timestamptz)
         WHERE state = 'open' AND NOT (root_key = ANY(:keys))
        RETURNING id
    """), {"now": now, "keys": sorted(seen_keys)})).all()
    stale = (await session.execute(text("""
        SELECT device_id::text FROM alarm
         WHERE alarm_type = 'cascade_incident' AND state <> 'CLEARED'
    """))).scalars().all()
    from app.repositories import alarms as alarm_repo
    for dev in stale:
        if dev in common_on:
            continue
        for row in await alarm_repo.clear_alarms(session, device_id=dev,
                                                 alarm_types=["cascade_incident"],
                                                 instance="", at=now, by="correlation"):
            await alarm_repo.record_history(session, alarm_id=row["id"], device_id=dev,
                                            action="cleared", severity=row["severity"],
                                            actor="correlation")
            actions.append(("alarm_cleared", row))
    if opened or resolved:
        log.info("incidents refreshed", opened=opened, updated=updated, resolved=len(resolved))
    return {"opened": opened, "updated": updated, "resolved": len(resolved),
            "actions": actions}


#: A cascade, for the common alarm: consequences on this many devices.
CASCADE_MIN_DEVICES = 3

#: The category of the common alarm, by the tier of its cause.
_COMMON_CATEGORY = {"power": "power", "plant": "cooling", "loop": "cooling", "unit": "cooling"}


def _by_room(graph: cc.CoolingGraph, members: list[dict[str, Any]],
             names: dict[str, str]) -> list[dict[str, Any]]:
    """The cascade by room: who is affected where, and how badly. Each room
    has its own response (its own load to shed, its own doors to open)."""
    rooms: dict[str, dict[str, Any]] = {}
    for m in members:
        node = graph.nodes.get(m["device_id"])
        rid = (node.room_id if node else None) or ""
        r = rooms.setdefault(rid, {"room_id": rid or None, "room_name": names.get(rid),
                                   "alarms": 0, "devices": set(), "severities": []})
        r["alarms"] += 1
        r["devices"].add(m["device_id"])
        r["severities"].append(m["severity"])
    out = [{"room_id": r["room_id"], "room_name": r["room_name"], "alarms": r["alarms"],
            "devices": len(r["devices"]), "worst": _worst(r["severities"])}
           for r in rooms.values()]
    return sorted(out, key=lambda r: (SEVERITY_RANK.get(r["worst"], 9), -r["alarms"]))


async def _raise_common(session: AsyncSession, *, rdev: str, tier: str, title: str,
                        severity: str, incident_id: str, members: int,
                        now: datetime) -> tuple[str, dict[str, Any]] | None:
    from app.repositories import alarms as alarm_repo
    alarm = await alarm_repo.raise_alarm(
        session, device_id=rdev, alarm_type="cascade_incident", instance="",
        severity=severity, source="correlation", observed_at=now,
        message=f"Cooling incident: {title} ({members} alarms downstream)",
        category=_COMMON_CATEGORY.get(tier, "cooling"), response_class="alarm",
        attributes={"incident_id": incident_id})
    if alarm is None or alarm["change"] == "touched":
        return None
    await alarm_repo.record_history(session, alarm_id=alarm["id"], device_id=rdev,
                                    action="raised" if alarm["change"] == "created"
                                    else alarm["change"],
                                    severity=alarm["severity"], actor="correlation",
                                    detail={"incident_id": incident_id})
    return ("alarm_created" if alarm["change"] == "created" else "alarm_updated", alarm)


def _scope(graph: cc.CoolingGraph, rdev: str, g: dict[str, Any]) -> tuple[str, str] | None:
    """What a root's incident is ABOUT, beyond the one machine: the site's
    plant for a chiller, tower, pump or feeder; the room for a CRAH or CDU."""
    node = graph.nodes.get(rdev)
    tier = None
    for m in g["members"]:
        tier = graph.ancestors(m["device_id"]).get(rdev) or tier
    if node is None or tier is None:
        return None
    if tier in ("plant", "power") and node.datacenter_id:
        return ("site", node.datacenter_id)
    if tier == "unit" and node.room_id:
        return ("room", node.room_id)
    return None


def _merge_by_scope(graph: cc.CoolingGraph, groups: dict[str, dict[str, Any]],
                    open_incidents: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """One incident per plant, not per chiller - and one identity for as long
    as the event lasts.

    all-chillers (2026-11-04): the lead chiller stopped, the sequencer called
    the next, which was held off, then the next - each "unit stopped" alarm
    raised, then cleared as the unit fell back to standby. Rooted per device,
    one plant failure read as three incidents. Roots of the same tier and
    scope are one event.

    all-chillers (2026-11-05): with the roots merged, the lead's alarm still
    cleared first, its incident resolved for want of a root, and the next
    chiller's opened as a new one - one event, two identities, two common
    alarms. The incident already open in the same scope keeps its key, even
    when the device it was keyed on no longer carries a root: the key is the
    event's name, not the machine's.
    """
    open_by_scope: dict[tuple[str, str], str] = {}
    for inc in open_incidents:
        dev = inc["root_key"].split(":", 1)[1]
        if inc["tier"] in ("plant", "power", "loop") and inc.get("datacenter_id"):
            open_by_scope.setdefault(("site", inc["datacenter_id"]), dev)
        elif inc["tier"] == "unit" and inc.get("room_id"):
            open_by_scope.setdefault(("room", inc["room_id"]), dev)
    buckets: dict[Any, list[str]] = {}
    for rdev, g in groups.items():
        buckets.setdefault(_scope(graph, rdev, g) or ("device", rdev), []).append(rdev)
    out: dict[str, dict[str, Any]] = {}
    for scope, devs in buckets.items():
        anchor = (open_by_scope.get(scope) if scope[0] != "device" else None)
        if anchor is None:
            anchor = min(devs, key=lambda d: min(r["first_seen"]
                                                 for r in groups[d]["roots"].values()))
        merged = {"roots": {}, "members": []}
        for d in devs:
            merged["roots"].update(groups[d]["roots"])
            merged["members"] += groups[d]["members"]
        out[anchor] = merged
    return out


async def _upsert(session: AsyncSession, *, key: str, tier: str, root_device: str | None,
                  root_alarm: str | None, dc: str | None, room: str | None, title: str,
                  summary: str, alarm_ids: list[str], count: int, evidence: dict[str, Any],
                  opened_at: datetime, now: datetime) -> tuple[str, bool]:
    row = (await session.execute(text("""
        INSERT INTO anomaly_incident (root_key, tier, root_device_id, root_alarm_id,
                                      datacenter_id, room_id, title, summary,
                                      member_alarm_ids, member_count, evidence,
                                      opened_at, last_activity_at)
        VALUES (:key, :tier, CAST(:rdev AS uuid), CAST(:ralarm AS uuid), CAST(:dc AS uuid),
                CAST(:room AS uuid), :title, :summary, CAST(:ids AS uuid[]), :count,
                CAST(:ev AS jsonb), :opened, :now)
        ON CONFLICT (root_key) WHERE state = 'open' DO UPDATE
           SET tier = EXCLUDED.tier, root_alarm_id = EXCLUDED.root_alarm_id,
               title = EXCLUDED.title, summary = EXCLUDED.summary,
               member_alarm_ids = EXCLUDED.member_alarm_ids,
               member_count = EXCLUDED.member_count, evidence = EXCLUDED.evidence,
               last_activity_at = CASE
                   WHEN anomaly_incident.member_alarm_ids = EXCLUDED.member_alarm_ids
                   THEN anomaly_incident.last_activity_at ELSE EXCLUDED.last_activity_at END
        RETURNING id::text, (xmax = 0) AS created
    """), {"key": key, "tier": tier, "rdev": root_device, "ralarm": root_alarm, "dc": dc,
           "room": room, "title": title, "summary": summary, "ids": alarm_ids,
           "count": count, "ev": json.dumps(evidence), "opened": opened_at,
           "now": now})).first()
    return row[0], bool(row[1])


# --- reads ----------------------------------------------------------------------

_INCIDENT_COLS = """
    i.id::text AS id, i.state, i.root_key, i.tier, i.root_device_id::text AS root_device_id,
    d.name AS root_device_name, i.root_alarm_id::text AS root_alarm_id,
    i.datacenter_id::text AS datacenter_id, dc.code AS site_code, dc.name AS site_name,
    i.room_id::text AS room_id, r.name AS room_name,
    i.title, i.summary, i.member_count, i.evidence, i.opened_at, i.last_activity_at,
    i.resolved_at, cardinality(i.member_alarm_ids) AS alarm_count
"""


#: How the list can be ordered. Open incidents lead in every order: a
#: reader paging the archive still sees what is wrong now first.
INCIDENT_ORDERS = ("recent", "longest", "biggest")
_ORDER_SQL = {
    "recent": "i.last_activity_at DESC",
    "longest": "(COALESCE(i.resolved_at, now()) - i.opened_at) DESC",
    "biggest": "cardinality(i.member_alarm_ids) DESC",
}


async def list_incidents(session: AsyncSession, *, states: list[str], device_id: str | None,
                         limit: int, offset: int = 0, datacenter: str | None = None,
                         tier: str | None = None, q: str | None = None,
                         resolved_since: datetime | None = None,
                         order: str = "recent",
                         with_total: bool = False) -> list[dict[str, Any]]:
    """Incidents, open first, then by `order`; a page of `limit` from
    `offset`. The order is total (the id breaks every tie), so walking the
    list in pages sees every row once. `with_total` rides the window count
    on each row as `total`, for a pager; the caller lifts it off."""
    where = ["i.state = ANY(:states)"]
    p: dict[str, Any] = {"states": states, "limit": limit, "offset": offset}
    if device_id:
        # Root or member: the 3D panel asks "is this machine part of one?"
        where.append("""(i.root_device_id = CAST(:dev AS uuid) OR EXISTS (
            SELECT 1 FROM alarm a WHERE a.id = ANY(i.member_alarm_ids)
               AND a.device_id = CAST(:dev AS uuid)))""")
        p["dev"] = device_id
    if datacenter:
        # A code (DC1) or an id, as the alarm list takes it.
        where.append("(dc.code = :dc OR i.datacenter_id::text = :dc)")
        p["dc"] = datacenter
    if tier:
        where.append("i.tier = :tier")
        p["tier"] = tier
    if q:
        # Words over what the row shows: the title, the root device, the room.
        where.append("(i.title ILIKE :q OR d.name ILIKE :q OR r.name ILIKE :q)")
        p["q"] = f"%{q}%"
    if resolved_since is not None:
        where.append("i.resolved_at >= :since")
        p["since"] = resolved_since
    total = ", count(*) OVER () AS total" if with_total else ""
    rows = (await session.execute(text(f"""
        SELECT {_INCIDENT_COLS}{total}
          FROM anomaly_incident i
          LEFT JOIN device d ON d.id = i.root_device_id
          LEFT JOIN room r ON r.id = i.room_id
          LEFT JOIN datacenter dc ON dc.id = i.datacenter_id
         WHERE {' AND '.join(where)}
         ORDER BY (i.state = 'open') DESC, {_ORDER_SQL.get(order, _ORDER_SQL["recent"])}, i.id
         LIMIT :limit OFFSET :offset
    """), p)).mappings().all()
    return [dict(r) for r in rows]


async def get_incident(session: AsyncSession, incident_id: str) -> dict[str, Any] | None:
    row = (await session.execute(text(f"""
        SELECT {_INCIDENT_COLS}, i.member_alarm_ids
          FROM anomaly_incident i
          LEFT JOIN device d ON d.id = i.root_device_id
          LEFT JOIN room r ON r.id = i.room_id
          LEFT JOIN datacenter dc ON dc.id = i.datacenter_id
         WHERE i.id = CAST(:id AS uuid)
    """), {"id": incident_id})).mappings().first()
    if row is None:
        return None
    out = dict(row)
    ids = out.pop("member_alarm_ids") or []
    alarms = (await session.execute(text("""
        SELECT a.id::text AS id, a.device_id::text AS device_id, d.name AS device_name,
               d.device_type::text AS device_type, a.alarm_type, a.instance,
               a.severity::text AS severity, a.state::text AS state, a.message,
               a.first_seen, a.cleared_at, a.is_symptom,
               a.root_cause_alarm_id::text AS root_cause_alarm_id
          FROM alarm a JOIN device d ON d.id = a.device_id
         WHERE a.id = ANY(CAST(:ids AS uuid[]))
         ORDER BY a.first_seen
    """), {"ids": [str(i) for i in ids]})).mappings().all()
    out["alarms"] = []
    for a in alarms:
        a = dict(a)
        peers = set((out.get("evidence") or {}).get("peer_alarm_ids") or [])
        a["role"] = ("root" if a["id"] == out["root_alarm_id"]
                     else "peer" if a["id"] in peers
                     else "folded" if a["is_symptom"] else "member")
        a["tier"] = cc.device_tier(a["device_type"])
        out["alarms"].append(a)
    out["findings"] = [dict(f) for f in (await session.execute(text("""
        SELECT f.id::text AS id, f.detector_id, f.device_id::text AS device_id,
               d.name AS device_name, f.state, f.summary, f.first_seen, f.last_seen
          FROM anomaly_finding f JOIN device d ON d.id = f.device_id
         WHERE f.incident_id = CAST(:id AS uuid)
         ORDER BY f.first_seen
    """), {"id": incident_id})).mappings().all()]
    return out
