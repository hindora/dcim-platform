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

    groups = _merge_by_scope(graph, groups, set((await session.execute(text(
        "SELECT root_key FROM anomaly_incident WHERE state = 'open'"))).scalars().all()))

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
    for rdev, g in groups.items():
        own = ([r for r in g["roots"].values() if r["device_id"] == rdev]
               or list(g["roots"].values()))
        root = min(own, key=lambda r: (r["first_seen"], r["id"]))
        rnode = graph.nodes.get(rdev)
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
        title, summary = device_summary(rnode.name if rnode else rdev, root["alarm_type"],
                                        g["members"], len(linked), air)
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
    by_room: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in hot:
        node = graph.nodes.get(a["device_id"])
        if node and node.room_id and node.tier in ("zone", "server"):
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
    if opened or resolved:
        log.info("incidents refreshed", opened=opened, updated=updated, resolved=len(resolved))
    return {"opened": opened, "updated": updated, "resolved": len(resolved)}


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
                    open_keys: set[str]) -> dict[str, dict[str, Any]]:
    """One incident per plant, not per chiller.

    all-chillers (2026-11-04): the lead chiller stopped, the sequencer called
    the next, which was held off, then the next - each "unit stopped" alarm
    raised, then cleared as the unit fell back to standby. Rooted per device,
    one plant failure read as three incidents. Roots of the same tier and
    scope are one event; the incident already open keeps its key, so the
    console does not see it renamed every minute.
    """
    buckets: dict[Any, list[str]] = {}
    for rdev, g in groups.items():
        buckets.setdefault(_scope(graph, rdev, g) or ("device", rdev), []).append(rdev)
    out: dict[str, dict[str, Any]] = {}
    for devs in buckets.values():
        if len(devs) == 1:
            out[devs[0]] = groups[devs[0]]
            continue
        anchor = next((d for d in devs if f"device:{d}" in open_keys), None) or min(
            devs, key=lambda d: min(r["first_seen"] for r in groups[d]["roots"].values()))
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
    i.datacenter_id::text AS datacenter_id, i.room_id::text AS room_id, r.name AS room_name,
    i.title, i.summary, i.member_count, i.evidence, i.opened_at, i.last_activity_at,
    i.resolved_at, cardinality(i.member_alarm_ids) AS alarm_count
"""


async def list_incidents(session: AsyncSession, *, states: list[str], device_id: str | None,
                         limit: int) -> list[dict[str, Any]]:
    where = ["i.state = ANY(:states)"]
    p: dict[str, Any] = {"states": states, "limit": limit}
    if device_id:
        # Root or member: the 3D panel asks "is this machine part of one?"
        where.append("""(i.root_device_id = CAST(:dev AS uuid) OR EXISTS (
            SELECT 1 FROM alarm a WHERE a.id = ANY(i.member_alarm_ids)
               AND a.device_id = CAST(:dev AS uuid)))""")
        p["dev"] = device_id
    rows = (await session.execute(text(f"""
        SELECT {_INCIDENT_COLS}
          FROM anomaly_incident i
          LEFT JOIN device d ON d.id = i.root_device_id
          LEFT JOIN room r ON r.id = i.room_id
         WHERE {' AND '.join(where)}
         ORDER BY (i.state = 'open') DESC, i.last_activity_at DESC
         LIMIT :limit
    """), p)).mappings().all()
    return [dict(r) for r in rows]


async def get_incident(session: AsyncSession, incident_id: str) -> dict[str, Any] | None:
    row = (await session.execute(text(f"""
        SELECT {_INCIDENT_COLS}, i.member_alarm_ids
          FROM anomaly_incident i
          LEFT JOIN device d ON d.id = i.root_device_id
          LEFT JOIN room r ON r.id = i.room_id
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
