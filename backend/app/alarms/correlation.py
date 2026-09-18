"""Dependency suppression: one root cause, N symptoms.

When an OOB switch dies, every device whose management interface lands on it
stops answering. The devices are fine - only the path used to watch them is
gone. Presenting that as 60 equal alarms buries the one that matters, so the
symptoms are marked and folded under the root.

Suppression is a display and notification decision, never a data decision. A
symptom keeps its row, its severity and its history; it gains ``is_symptom``
and a pointer to the alarm that explains it, and it is released the moment the
root clears.

The dangerous half of this is knowing when NOT to suppress. Losing an A feed
while the B feed is healthy leaves the load running, so a downstream alarm at
that moment is NOT explained by the feed failure - it is a real, separate
fault, and hiding it under the power alarm is how a single-feed condition goes
unnoticed until the other side fails too. That check is why redundancy_side
exists.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.layers import UPSTREAM_COL
from app.core.logging import get_logger
from app.repositories.alarms import _SEV_RANK

log = get_logger("alarms.correlation")

# Only alarms that mean "I cannot see it" are suppressible. A high temperature
# or a failed PSU on a device behind a dead switch is still a real condition
# about that device and must never be folded away.
#
# telemetry_stale is deliberately NOT in here, though it looks like it belongs.
# It is raised only for endpoints whose poll is currently SUCCEEDING - that is
# the definition of reachable-but-silent - so an upstream "cannot see it" root
# cannot explain it. Suppressing it anyway produced exactly the wrong answer in
# testing: an endpoint polling happily 287 seconds ago was folded under an
# unreachable OOB switch, hiding the one condition the staleness sweep exists
# to surface.
SUPPRESSIBLE_TYPES = frozenset({"endpoint_unreachable"})

# Link-down is suppressible too, but NOT by the upstream walk: a port going
# down on a spine is not explained by anything upstream OF THE SPINE. It is
# explained when the device on the far end of the cable has lost power - see
# correlate(). A link whose far end is still powered stays a root, because then
# the cable, the optic or the far end's port is the fault.
LINK_TYPES = frozenset({"link_down"})

# A port flapping is the other thing a switch says as it boots: its links come
# up and bounce while the ASIC and the far ends settle. Folded under a power
# fault only inside the hold-down - the device's own power just returned, or
# the device across the cable is dark or just back. Anywhere else a flap is a
# bad optic or cable and stays a root.
FLAP_TYPES = frozenset({"link_flap"})

# What counts as a root on an upstream device, per layer.
#
# endpoint_unreachable is a root everywhere: a switch that cannot be seen
# explains the devices seen through it, and a feeder whose management card has
# gone dark has usually lost its own input.
#
# The power layer adds the events that DE-ENERGISE a feeder's output. A rack
# PDU's breaker trip and a switchgear breaker trip open the circuit; everything
# corded to it is off, however healthy its controller still looks. Missing them
# is how one PDU trip showed up as three unrelated kinds of root - the trip,
# four spine link-downs, and eighteen unreachable servers once the poll landed.
#
# Deliberately NOT here: ups_on_battery (the load is still fed), overloads and
# voltage excursions (degraded, not dead). A root has to mean "nothing is
# coming out of this", or the redundancy veto below is fed a lie.
_UNREACHABLE = ("endpoint_unreachable",)
ROOT_TYPES_BY_LAYER: dict[str, tuple[str, ...]] = {
    "management": _UNREACHABLE,
    "fieldbus": _UNREACHABLE,
    "power": (*_UNREACHABLE, "breaker_tripped", "switchgear_breaker_trip"),
}

# A breaker alarm that NAMES a breaker (one bank of a multi-bank rack PDU)
# opens only the outlets on that bank, and the model does not map outlets to
# banks. Only a whole-device trip (empty instance) is taken as de-energising
# everything behind it; a named one stays a root of its own and explains
# nothing. endpoint_unreachable's instance is an endpoint id, not a part, so
# it is exempt.
_ANY_INSTANCE = _UNREACHABLE


def root_types(layer: str) -> tuple[str, ...]:
    return ROOT_TYPES_BY_LAYER.get(layer, _UNREACHABLE)


# A device that boots when its power comes back is not a second incident. Every
# switch and PDU sends coldStart and every BMC announces itself, and after rack
# R2-02's two PDUs were reset nineteen of them stood on the console as roots.
RESTART_TYPES = frozenset({"device_restarted"})

# How recently a power fault must have been open for a restart to be its
# consequence. A BMC is up within a minute of power; a host OS and its SNMP
# agent can take several. Ten minutes covers a slow POST without claiming a
# reboot next morning.
RESTORE_WINDOW_S = 600

# The hold-down after a power root clears. Its symptoms are NOT released the
# instant it clears: a server is still booting for another half-minute, a leaf
# that came up first still sees its server ports down, and releasing them made
# thirty-six momentary roots out of one restoration. Anything still open this
# long after the power came back is a genuine fault and is released then, by
# the sweep. Restarts are never released - they ARE the restoration.
RESTORE_HOLD_S = 180

# Which end of a connection is upstream is NOT uniform across layers, and
# assuming it is produces a correlation engine that silently explains nothing -
# the traversal walks away from the cause instead of towards it. The map lives
# in app.core.layers because impact analysis needs the same fact.
_UPSTREAM_COL = UPSTREAM_COL

# Cheapest and most common explanation first. Power is last because it is the
# only one that can be vetoed by redundancy.
LAYER_ORDER = ("management", "fieldbus", "power")

# Two hops covers device -> access switch -> distribution switch, and
# load -> rack PDU -> RPP. Past that the "cause" is too far away to assert.
MAX_HOPS = 2


def has_surviving_feed(side_status: dict[str, bool]) -> bool:
    """True when at least one distribution path into the load is still healthy.

    ``side_status`` maps a redundancy side ('A', 'B', or '?' for a feed whose
    path could not be determined) to whether every feeder on that side is
    alarming.

    A load with a healthy side is still powered, so nothing downstream of it is
    explained by the failure - which is exactly the case the exit criterion for
    this phase turns on.
    """
    return any(not compromised for compromised in side_status.values())


async def _upstream_root(session: AsyncSession, device_id: str,
                         layer: str) -> dict[str, Any] | None:
    """The nearest active root alarm upstream of this device on one layer."""
    up_col, down_col = _UPSTREAM_COL[layer]
    # up_col/down_col come from a fixed dict, never from a request.
    sql = f"""
        WITH RECURSIVE up AS (
            SELECT CAST(:device AS uuid) AS dev, 0 AS hop
            UNION
            SELECT c.{up_col}, u.hop + 1
              FROM up u
              JOIN connection c ON c.{down_col} = u.dev
               AND c.layer = CAST(:layer AS layer_t)
               AND c.admin_state = 'enabled'
             WHERE u.hop < :max_hops
        )
        SELECT a.id::text        AS id,
               a.device_id::text AS device_id,
               a.alarm_type, a.severity::text AS severity,
               d.name            AS device_name,
               u.hop
          FROM up u
          JOIN alarm a  ON a.device_id = u.dev
          JOIN device d ON d.id = u.dev
         WHERE u.hop > 0
           AND a.state <> 'CLEARED'
           AND a.alarm_type = ANY(:root_types)
           AND (a.instance = '' OR a.alarm_type = ANY(:any_instance))
         -- Nearest first, then oldest: the upstream failure that started it.
         ORDER BY u.hop, a.first_seen
         LIMIT 1
    """
    row = (await session.execute(text(sql), {
        "device": device_id, "layer": layer, "max_hops": MAX_HOPS,
        "root_types": list(root_types(layer)),
        "any_instance": list(_ANY_INSTANCE),
    })).mappings().first()
    return dict(row) if row else None


async def feed_side_status(session: AsyncSession,
                           device_id: str) -> dict[str, bool]:
    """Per power path into this device, is every feeder on it alarming?

    Grouped by redundancy_side, so a dual-fed load yields two entries and a
    single-corded one yields a single entry. Feeds whose side could not be
    derived are grouped under '?' rather than silently merged with A: an
    unknown path is not evidence of redundancy.
    """
    rows = (await session.execute(text("""
        SELECT COALESCE(c.redundancy_side, '?')      AS side,
               bool_or(root.id IS NOT NULL)          AS compromised
          FROM connection c
          LEFT JOIN alarm root
                 ON root.device_id = c.a_device_id
                AND root.state <> 'CLEARED'
                AND root.alarm_type = ANY(:root_types)
                AND (root.instance = '' OR root.alarm_type = ANY(:any_instance))
         WHERE c.layer = CAST('power' AS layer_t)
           AND c.admin_state = 'enabled'
           AND c.b_device_id = CAST(:device AS uuid)
         GROUP BY 1
    """), {"device": device_id, "root_types": list(root_types("power")),
          "any_instance": list(_ANY_INSTANCE)})).all()
    return {side: bool(compromised) for side, compromised in rows}


async def mark_symptom(session: AsyncSession, *, alarm_id: str,
                       root_alarm_id: str) -> None:
    await session.execute(text("""
        UPDATE alarm
           SET is_symptom = true, root_cause_alarm_id = CAST(:root AS uuid)
         WHERE id = CAST(:id AS uuid)
    """), {"id": alarm_id, "root": root_alarm_id})


async def power_dead_root(session: AsyncSession,
                          device_id: str) -> dict[str, Any] | None:
    """The power root that has taken this device dark, if one has.

    Both halves are required: an upstream de-energising root, AND no surviving
    feed. A dual-corded load that lost its A strip is still running, so the A
    strip explains nothing about it.
    """
    root = await _upstream_root(session, device_id, "power")
    if not root:
        return None
    if has_surviving_feed(await feed_side_status(session, device_id)):
        return None
    return root


async def restored_root(session: AsyncSession,
                        device_id: str) -> dict[str, Any] | None:
    """The power fault this device is still coming back from, if any.

    Every feed side had a de-energising root that cleared within the hold-down
    - so the device was dark and its power returned moments ago. What it says
    while it boots (silent endpoints, links not yet up) belongs to that fault.
    """
    rows = (await session.execute(_RECENT_POWER_ROOTS, {
        "device": device_id, "max_hops": MAX_HOPS,
        "root_types": list(root_types("power")),
        "any_instance": list(_ANY_INSTANCE), "window_s": RESTORE_HOLD_S,
    })).mappings().all()
    sides = {r["side"] for r in rows}
    hit = [r for r in rows if r["id"] and r["cleared_at"] is not None]
    if not sides or {r["side"] for r in hit} != sides:
        return None
    root = max(hit, key=lambda r: r["cleared_at"])
    return {"id": root["id"], "device_name": root["device_name"],
            "alarm_type": root["alarm_type"], "hop": 0}


async def _correlate_link(session: AsyncSession, *, alarm_id: str,
                          device_id: str, instance: str) -> dict[str, Any] | None:
    """A port down because the device on the far end lost power.

    The spine's port is fine and so is the cable; the leaf at the other end is
    dark. That is one incident - the power event - and the spine's report is
    its symptom. If the far end still has power the link-down stays a root:
    then it IS about the cable, the optic or the far port.
    """
    # Local import: link_correlation knows which interface a cable lands on,
    # and it has no reason to import this module back.
    from app.alarms import link_correlation

    link = await link_correlation.find_link(
        session, device_id=device_id, instance=instance)
    if not link or not link["peer_device_id"]:
        return None
    root = (await power_dead_root(session, link["peer_device_id"])
            or await restored_root(session, link["peer_device_id"]))
    if not root:
        return None
    await mark_symptom(session, alarm_id=alarm_id, root_alarm_id=root["id"])
    log.info("link-down suppressed; far end lost power", alarm_id=alarm_id,
             port=instance, peer=link["peer_device_id"], root_alarm=root["id"],
             root_device=root["device_name"])
    return {**root, "layer": "power", "via_peer": link["peer_device_id"]}


_RECENT_POWER_ROOTS = text("""
    WITH RECURSIVE up AS (
        -- Each direct feed carries its side; everything above it inherits it.
        SELECT c.a_device_id AS dev, COALESCE(c.redundancy_side, '?') AS side,
               1 AS hop
          FROM connection c
         WHERE c.layer = CAST('power' AS layer_t)
           AND c.admin_state = 'enabled'
           AND c.b_device_id = CAST(:device AS uuid)
        UNION
        SELECT c.a_device_id, u.side, u.hop + 1
          FROM up u
          JOIN connection c ON c.b_device_id = u.dev
           AND c.layer = CAST('power' AS layer_t)
           AND c.admin_state = 'enabled'
         WHERE u.hop < :max_hops
    )
    SELECT u.side, a.id::text AS id, d.name AS device_name, a.alarm_type,
           a.severity::text AS severity, a.cleared_at
      FROM up u
      LEFT JOIN alarm a ON a.device_id = u.dev
            AND a.alarm_type = ANY(:root_types)
            AND (a.instance = '' OR a.alarm_type = ANY(:any_instance))
            AND (a.state <> 'CLEARED'
                 OR a.cleared_at > now() - make_interval(secs => :window_s))
      LEFT JOIN device d ON d.id = a.device_id
""")


async def _correlate_restart(session: AsyncSession, *, alarm_id: str,
                             device_id: str) -> dict[str, Any] | None:
    """A boot that followed its power coming back, folded under that fault.

    Only when EVERY feed side had a de-energising fault open inside the window.
    A dual-corded server that reboots while one side was down and the other
    healthy was never unpowered: that reboot is its own fault - a PSU that did
    not carry the load, a firmware watchdog - and hiding it under the one strip
    that tripped is the veto's whole reason for existing.

    Folded under the fault that cleared LAST, since that is the restoration
    that actually brought the device back.
    """
    rows = (await session.execute(_RECENT_POWER_ROOTS, {
        "device": device_id, "max_hops": MAX_HOPS,
        "root_types": list(root_types("power")),
        "any_instance": list(_ANY_INSTANCE), "window_s": RESTORE_WINDOW_S,
    })).mappings().all()
    sides = {r["side"] for r in rows}
    hit = [r for r in rows if r["id"]]
    if not sides or {r["side"] for r in hit} != sides:
        return None
    # Still-open roots sort last: an open fault did not restore anything, but
    # if one is all there is, it is still the explanation.
    root = max(hit, key=lambda r: (r["cleared_at"] is not None,
                                   r["cleared_at"] or 0))
    await mark_symptom(session, alarm_id=alarm_id, root_alarm_id=root["id"])
    log.info("restart folded under the power fault it followed",
             alarm_id=alarm_id, device_id=device_id, root_alarm=root["id"],
             root_device=root["device_name"])
    return {"id": root["id"], "device_name": root["device_name"],
            "alarm_type": root["alarm_type"], "layer": "power"}


async def _correlate_flap(session: AsyncSession, *, alarm_id: str,
                          device_id: str, instance: str) -> dict[str, Any] | None:
    """A flap that is the boot of this device, or of the one across the cable."""
    root = await restored_root(session, device_id)
    if not root and instance:
        from app.alarms import link_correlation
        link = await link_correlation.find_link(
            session, device_id=device_id, instance=instance)
        if link and link["peer_device_id"]:
            root = (await power_dead_root(session, link["peer_device_id"])
                    or await restored_root(session, link["peer_device_id"]))
    if not root:
        return None
    await mark_symptom(session, alarm_id=alarm_id, root_alarm_id=root["id"])
    log.info("link flap folded under a power incident", alarm_id=alarm_id,
             device_id=device_id, root_alarm=root["id"])
    return {**root, "layer": "power"}


async def correlate(session: AsyncSession, *, alarm_id: str, device_id: str,
                    alarm_type: str, instance: str = "") -> dict[str, Any] | None:
    """Fold a new alarm under an upstream root, if one explains it.

    Returns the root alarm when suppressed, otherwise None.
    """
    if alarm_type in RESTART_TYPES:
        return await _correlate_restart(session, alarm_id=alarm_id,
                                        device_id=device_id)
    if alarm_type in FLAP_TYPES:
        return await _correlate_flap(session, alarm_id=alarm_id,
                                     device_id=device_id, instance=instance)
    if alarm_type in LINK_TYPES:
        return await _correlate_link(session, alarm_id=alarm_id,
                                     device_id=device_id, instance=instance)
    if alarm_type not in SUPPRESSIBLE_TYPES:
        return None

    # Still booting from a power fault that has just cleared: its silence is
    # part of that fault, not a new one. Checked first - if the device were
    # still dark, power_dead_root below would find the open root anyway.
    restored = await restored_root(session, device_id)
    if restored:
        await mark_symptom(session, alarm_id=alarm_id,
                           root_alarm_id=restored["id"])
        log.info("alarm held under a just-cleared power root",
                 alarm_id=alarm_id, root_alarm=restored["id"],
                 root_device=restored["device_name"])
        return {**restored, "layer": "power"}

    for layer in LAYER_ORDER:
        root = await _upstream_root(session, device_id, layer)
        if not root:
            continue

        if layer == "power":
            sides = await feed_side_status(session, device_id)
            if has_surviving_feed(sides):
                # Still fed from another path, so the feed failure does not
                # explain this. Leave it visible: it is a genuine fault AND
                # the load is now running without redundancy.
                log.info("power root not applied; load still fed",
                         alarm_id=alarm_id, device_id=device_id,
                         root=root["device_name"], sides=sides)
                continue

        await mark_symptom(session, alarm_id=alarm_id, root_alarm_id=root["id"])
        log.info("alarm suppressed under root", alarm_id=alarm_id, layer=layer,
                 root_alarm=root["id"], root_device=root["device_name"],
                 hops=root["hop"])
        return {**root, "layer": layer}
    return None


# ------------------------------------------------------- late roots
#
# correlate() runs when an alarm is raised, so it can only fold a symptom under
# a root that already exists. A power event does not arrive in that order. When
# both PDUs under a leaf tripped, the four spine linkDown traps and the two
# breaker traps landed within 0.2 s of each other, the link-downs first - and
# with two ingest workers a root one worker has raised is not even visible to
# the other until it commits. Every link-down stayed a root.
#
# So a power root looks back when it is raised: every load it has taken dark
# (both halves of power_dead_root, the redundancy veto included) has its open
# unreachable alarms and the link-downs facing it folded under the root. This
# is what Netcool and Smarts do with a late parent - re-correlate the children
# that arrived first.

_DOWNSTREAM = text("""
    WITH RECURSIVE down AS (
        SELECT CAST(:device AS uuid) AS dev, 0 AS hop
        UNION
        SELECT c.b_device_id, d.hop + 1
          FROM down d
          JOIN connection c ON c.a_device_id = d.dev
           AND c.layer = CAST('power' AS layer_t)
           AND c.admin_state = 'enabled'
         WHERE d.hop < :max_hops
    )
    SELECT DISTINCT dev::text AS id FROM down WHERE hop > 0
""")

# Open, not already explained, and of a kind a dead device produces: its own
# endpoints going silent, and the ports on its neighbours that face it.
_ORPHANS_OF = text("""
    SELECT a.id::text AS id, a.device_id::text AS device_id,
           a.alarm_type, a.severity::text AS severity
      FROM alarm a
     WHERE a.device_id = CAST(:load AS uuid)
       AND a.alarm_type = ANY(:suppressible)
       AND a.state <> 'CLEARED' AND NOT a.is_symptom
    UNION ALL
    SELECT a.id::text, a.device_id::text, a.alarm_type, a.severity::text
      FROM alarm a
      JOIN interface i ON i.device_id = a.device_id AND i.name = a.instance
      JOIN connection c ON c.layer::text = ANY(:port_layers)
                       AND (c.a_termination_id = i.id OR c.b_termination_id = i.id)
     WHERE a.alarm_type = ANY(:link_types)
       AND a.state <> 'CLEARED' AND NOT a.is_symptom
       AND CASE WHEN c.a_termination_id = i.id THEN c.b_device_id
                ELSE c.a_device_id END = CAST(:load AS uuid)
""")


async def adopt_orphans(session: AsyncSession, *, alarm_type: str,
                        device_id: str, instance: str) -> list[dict[str, Any]]:
    """Fold alarms raised BEFORE this power root under it, where it explains
    them. Returns the adopted alarms."""
    if alarm_type not in root_types("power"):
        return []
    if instance and alarm_type not in _ANY_INSTANCE:
        # A named bank does not take the whole strip down - see _ANY_INSTANCE.
        return []
    # Local import, as in _correlate_link.
    from app.alarms import link_correlation

    loads = (await session.execute(_DOWNSTREAM, {
        "device": device_id, "max_hops": MAX_HOPS})).scalars().all()
    adopted: list[dict[str, Any]] = []
    for load in loads:
        root = await power_dead_root(session, load)
        if not root:
            continue                      # still fed on a surviving side
        rows = (await session.execute(_ORPHANS_OF, {
            "load": load, "suppressible": list(SUPPRESSIBLE_TYPES),
            "link_types": list(LINK_TYPES),
            "port_layers": list(link_correlation.PORT_LAYERS),
        })).mappings().all()
        for row in rows:
            await mark_symptom(session, alarm_id=row["id"], root_alarm_id=root["id"])
            adopted.append({**dict(row), "root": root["id"],
                            "root_device": root["device_name"]})
    if adopted:
        log.info("late root adopted earlier alarms", root_device=device_id,
                 alarm_type=alarm_type, count=len(adopted))
    return adopted


async def open_deenergising_roots(session: AsyncSession) -> list[dict[str, Any]]:
    """Open whole-device power roots that are not visibility failures.

    The sweep's worklist. endpoint_unreachable is left out on purpose: there
    can be hundreds open at once, and adoption at raise time covers it - the
    race the sweep exists for is two breaker trips landing on two workers in
    the same instant, each seeing the other side as still fed.
    """
    types = [t for t in root_types("power") if t not in _UNREACHABLE]
    rows = (await session.execute(text("""
        SELECT id::text, device_id::text, alarm_type, instance
          FROM alarm
         WHERE state <> 'CLEARED' AND NOT is_symptom
           AND alarm_type = ANY(:types) AND instance = ''
    """), {"types": types})).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- bands
#
# A warning rule and a critical rule on ONE measurement are two views of one
# condition, not two conditions. A CPU at 93 C crosses `cpu_temp_high` (>80)
# and `cpu_temp_critical` (>90) together, and the console showed both: two
# rows, two severities, two acknowledgements and two clears for one hot CPU.
#
# Measured on this fleet: three injected faults produced five alarms, and the
# WARNING for the temperature arrived a minute AFTER its CRITICAL because the
# two rules carry different dwells - so the list read as though the situation
# had improved while nothing had changed.
#
# ISA-18.2's position is one alarm per measurement point with a severity that
# escalates. This platform keeps the separate rules - they hold different
# thresholds, dwells and response classes, and both are genuinely true - and
# folds the lower under the higher, which is what the suppression machinery
# above already does for dependency roots. The record keeps both; the console
# shows the one that matters.

_BAND_ROOT = text("""
    WITH me AS (
        SELECT metric_key, operator, threshold
          FROM alarm_rule
         WHERE alarm_type = :alarm_type AND metric_key IS NOT NULL
         LIMIT 1
    ), higher AS (
        -- A band is HIGHER when its threshold is further along the direction
        -- the rule fires in. Severity is not the test: it is a label chosen by
        -- whoever wrote the rule, and two rules can share one.
        SELECT r.alarm_type, r.threshold
          FROM alarm_rule r, me
         WHERE r.enabled
           AND r.metric_key = me.metric_key
           AND r.alarm_type <> :alarm_type
           AND r.threshold IS NOT NULL AND me.threshold IS NOT NULL
           AND ((me.operator = '>' AND r.threshold > me.threshold)
             OR (me.operator = '<' AND r.threshold < me.threshold))
    )
    SELECT a.id::text AS id, a.alarm_type, a.severity::text AS severity
      FROM alarm a
      JOIN higher h ON h.alarm_type = a.alarm_type
     WHERE a.device_id = CAST(:device_id AS uuid)
       AND a.instance IS NOT DISTINCT FROM :instance
       AND a.state <> 'CLEARED'
       AND NOT a.is_symptom
     ORDER BY CASE (SELECT operator FROM me) WHEN '>' THEN -h.threshold
                                             ELSE h.threshold END
     LIMIT 1
""")

_LOWER_BANDS = text("""
    WITH me AS (
        SELECT metric_key, operator, threshold
          FROM alarm_rule
         WHERE alarm_type = :alarm_type AND metric_key IS NOT NULL
         LIMIT 1
    ), lower AS (
        SELECT r.alarm_type
          FROM alarm_rule r, me
         WHERE r.enabled
           AND r.metric_key = me.metric_key
           AND r.alarm_type <> :alarm_type
           AND r.threshold IS NOT NULL AND me.threshold IS NOT NULL
           AND ((me.operator = '>' AND r.threshold < me.threshold)
             OR (me.operator = '<' AND r.threshold > me.threshold))
    )
    SELECT a.id::text AS id, a.alarm_type, a.severity::text AS severity
      FROM alarm a
      JOIN lower l ON l.alarm_type = a.alarm_type
     WHERE a.device_id = CAST(:device_id AS uuid)
       AND a.instance IS NOT DISTINCT FROM :instance
       AND a.state <> 'CLEARED'
       AND a.id <> CAST(:alarm_id AS uuid)
""")


async def collapse_bands(session: AsyncSession, *, alarm_id: str,
                         device_id: str, alarm_type: str,
                         instance: str) -> dict[str, Any] | None:
    """Fold this alarm and its siblings into one visible band.

    Both directions, because either can happen first and neither order is
    unusual: a value that jumps straight past both thresholds raises the
    critical first, while a value that climbs raises the warning first. The
    dwells differ too, so the arrival order does not even follow the reading.

    Returns the higher-band alarm when THIS one was folded under it; otherwise
    folds any open lower bands under this one and returns None.
    """
    args = {"alarm_type": alarm_type, "device_id": device_id,
            "instance": instance or ""}

    root = (await session.execute(_BAND_ROOT, args)).mappings().first()
    if root:
        await mark_symptom(session, alarm_id=alarm_id, root_alarm_id=root["id"])
        log.info("band folded under a higher one", alarm_id=alarm_id,
                 alarm_type=alarm_type, root_alarm=root["id"],
                 root_type=root["alarm_type"])
        return dict(root)

    lower = (await session.execute(
        _LOWER_BANDS, {**args, "alarm_id": alarm_id})).mappings().all()
    for row in lower:
        await mark_symptom(session, alarm_id=row["id"], root_alarm_id=alarm_id)
        log.info("lower band folded under this one", alarm_id=row["id"],
                 alarm_type=row["alarm_type"], root_alarm=alarm_id,
                 root_type=alarm_type)
    return None


#: The same condition, reported without saying which part.
#:
#: A trap says "this device is hot" and carries no instance; the rule watching
#: the same reading says "CPU Temp is hot" and carries the sensor's name. On an
#: instance-scoped metric those cannot share an alarm key - and must not, since
#: a dual-socket server's two CPU sensors are two faults - so they arrive as two
#: alarms for one fan.
#:
#: The qualified one is the root: it names the part, which is what somebody
#: acting on it needs. The unqualified one becomes its symptom, still there,
#: still linked, no longer a second line on the console.
_QUALIFIED_SIBLING = text("""
    SELECT id::text, alarm_type, instance, severity::text AS severity
      FROM alarm
     WHERE device_id = CAST(:device_id AS uuid)
       AND alarm_type = CAST(:alarm_type AS text)
       AND instance <> ''
       AND state <> 'CLEARED'
       AND NOT is_symptom
     -- Worst first: _SEV_RANK numbers CRITICAL 0, so ASC is most severe.
     -- Imported rather than rewritten, because a severity ranked one way here
     -- and another way in the alarm list is how a console starts disagreeing
     -- with itself.
     ORDER BY {rank} ASC, first_seen
     LIMIT 1
""".format(rank=_SEV_RANK.format(col="severity")))

#: The reverse: an unqualified alarm already open when a qualified one arrives.
_UNQUALIFIED_SIBLINGS = text("""
    SELECT id::text, alarm_type, severity::text AS severity
      FROM alarm
     WHERE device_id = CAST(:device_id AS uuid)
       AND alarm_type = CAST(:alarm_type AS text)
       AND instance = ''
       AND id <> CAST(:alarm_id AS uuid)
       AND state <> 'CLEARED'
       AND NOT is_symptom
""")


async def collapse_unqualified(session: AsyncSession, *, alarm_id: str,
                               device_id: str, alarm_type: str,
                               instance: str) -> dict[str, Any] | None:
    """Fold a part-less alarm under the one that names the part.

    Both directions, for the same reason bands need both: the trap usually
    arrives first, having been sent the moment the device noticed, but a poll
    that lands mid-climb can beat it.

    Only within one canonical alarm type. Two different conditions on one
    device are two conditions, however close together they appear.
    """
    if instance:
        # This alarm names a part. Anything unqualified of the same type is the
        # same condition, seen with less detail.
        rows = (await session.execute(_UNQUALIFIED_SIBLINGS, {
            "device_id": device_id, "alarm_type": alarm_type,
            "alarm_id": alarm_id})).mappings().all()
        for row in rows:
            await mark_symptom(session, alarm_id=row["id"],
                               root_alarm_id=alarm_id)
            log.info("unqualified alarm folded under a named instance",
                     alarm_id=row["id"], alarm_type=alarm_type,
                     root_alarm=alarm_id, instance=instance)
        return None

    root = (await session.execute(_QUALIFIED_SIBLING, {
        "device_id": device_id, "alarm_type": alarm_type})).mappings().first()
    if root:
        await mark_symptom(session, alarm_id=alarm_id, root_alarm_id=root["id"])
        log.info("unqualified alarm folded under a named instance",
                 alarm_id=alarm_id, alarm_type=alarm_type,
                 root_alarm=root["id"], instance=root["instance"])
        return dict(root)
    return None


async def release_after_hold(session: AsyncSession) -> list[dict[str, Any]]:
    """Release what a cleared power root still holds, once the hold is over.

    Still open three minutes after the power came back is not a boot: it is a
    server that did not come up, a link that stayed down. Those become roots
    of their own. Restarts stay folded - they are the restoration itself, and
    they age out on their own.
    """
    rows = (await session.execute(text("""
        UPDATE alarm s
           SET is_symptom = false, root_cause_alarm_id = NULL
          FROM alarm r
         WHERE s.root_cause_alarm_id = r.id
           AND s.state <> 'CLEARED'
           AND NOT (s.alarm_type = ANY(:keep))
           AND r.state = 'CLEARED'
           AND r.alarm_type = ANY(:held)
           AND r.cleared_at < now() - make_interval(secs => :hold_s)
        RETURNING s.id::text AS id, s.device_id::text AS device_id,
                  s.alarm_type, s.severity::text AS severity,
                  r.id::text AS root
    """), {"keep": list(RESTART_TYPES),
          "held": [t for t in root_types("power") if t not in _UNREACHABLE],
          "hold_s": RESTORE_HOLD_S})).mappings().all()
    out = [dict(r) for r in rows]
    if out:
        log.info("held symptoms released after restoration", count=len(out))
    return out


async def release_symptoms(session: AsyncSession,
                           root_alarm_id: str) -> list[dict[str, Any]]:
    """Un-suppress everything a now-cleared root was explaining.

    Without this a symptom stays hidden after its cause is fixed, and an
    operator is left with a device that is still broken and an alarm list that
    says nothing is wrong.
    """
    # A de-energising power root is held down rather than released: what it
    # explained is still booting. The sweep releases what outlives the hold.
    held = (await session.execute(text("""
        SELECT 1 FROM alarm
         WHERE id = CAST(:root AS uuid)
           AND alarm_type = ANY(:held) AND instance = ''
    """), {"root": root_alarm_id,
          "held": [t for t in root_types("power") if t not in _UNREACHABLE]})).first()
    if held:
        return []
    rows = (await session.execute(text("""
        UPDATE alarm
           SET is_symptom = false, root_cause_alarm_id = NULL
         WHERE root_cause_alarm_id = CAST(:root AS uuid)
           AND state <> 'CLEARED'
        RETURNING id::text AS id, device_id::text AS device_id, alarm_type,
                  severity::text AS severity
    """), {"root": root_alarm_id})).mappings().all()
    out = [dict(r) for r in rows]
    if out:
        log.info("symptoms released", root_alarm=root_alarm_id, count=len(out))
    return out
