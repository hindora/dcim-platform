"""One cable, one row - without losing which end could still see.

A link that fails is one fault, and both switches on it say so. The console
showed two rows: `LF1 link_down` and `SP2 link_down`, unrelated as far as the
list was concerned, acknowledged twice and cleared twice.

The obvious repair is to merge them. This does not merge them, and the reason
matters. The two reports are not duplicates of one fact - they are two
independent observations, and the interesting cases are exactly the ones where
they disagree:

  * both ends down - the cable, the patch, or a shared optic.
  * one end down, the far end up - unidirectional: a broken Rx fibre, a failed
    laser, a wavelength mismatch. The far end still transmits happily and will
    keep forwarding into a hole.
  * one end down, the far end silent - the far end has no power. The silence
    IS the diagnosis, and it was on this fleet: LF1 reported a port down and
    SRV05 said nothing, because SRV05 had been de-energised.

Collapsing to a single synthesised "link down" throws all three into one
bucket. So both alarms are kept exactly as raised, and the later one is folded
under the earlier as a symptom - the same mechanism dependency roots and
severity bands already use. The console shows one row, the row names both
ends, and the record still holds two independent observations and the order
they arrived in.

This is what topology-aware managers do - Netcool, Smarts, and the
parent/child suppression in NPM: correlate to one incident, keep the evidence.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger("alarms.link")

LINK_TYPES = ("link_down",)

# Only layers where a port is a port. Power and cooling connections terminate
# on outlets and pipe stubs, which do not report link state.
PORT_LAYERS = ("production", "management")


_PEER_END = text("""
    WITH me AS (
        SELECT id FROM interface
         WHERE device_id = CAST(:device_id AS uuid) AND name = :instance
    )
    SELECT c.id::text AS connection_id,
           c.layer::text AS layer,
           CASE WHEN c.a_termination_id = me.id THEN c.b_device_id
                ELSE c.a_device_id END::text AS peer_device_id,
           CASE WHEN c.a_termination_id = me.id THEN c.b_termination_id
                ELSE c.a_termination_id END::text AS peer_iface_id
      FROM connection c, me
     WHERE c.layer::text = ANY(:layers)
       AND (c.a_termination_id = me.id OR c.b_termination_id = me.id)
     LIMIT 1
""")

_PEER_ALARM = text("""
    SELECT a.id::text AS id, a.first_seen, a.severity::text AS severity,
           a.is_symptom, d.name AS device_name, i.name AS port
      FROM alarm a
      JOIN device d ON d.id = a.device_id
      JOIN interface i ON i.id = CAST(:peer_iface AS uuid)
     WHERE a.device_id = CAST(:peer_device AS uuid)
       AND a.alarm_type = ANY(:types)
       AND a.instance = i.name
       AND a.state <> 'CLEARED'
       AND a.id <> CAST(:alarm_id AS uuid)
     ORDER BY a.first_seen
     LIMIT 1
""")

_ME = text("""
    SELECT a.first_seen, d.name AS device_name
      FROM alarm a JOIN device d ON d.id = a.device_id
     WHERE a.id = CAST(:alarm_id AS uuid)
""")

# oper_state is the link's own state, not a copy of an alarm. It stays DOWN
# while any end still reports the port down, so a link is not called up
# because the end that died cannot participate in its own recovery.
_REFRESH_STATE = text("""
    UPDATE connection c
       SET oper_state = CASE WHEN EXISTS (
               SELECT 1
                 FROM interface i
                 JOIN alarm a ON a.device_id = i.device_id
                             AND a.instance = i.name
                WHERE i.id IN (c.a_termination_id, c.b_termination_id)
                  AND a.alarm_type = ANY(:types)
                  AND a.state <> 'CLEARED'
           ) THEN 'down' ELSE 'up' END
     WHERE c.id = CAST(:connection_id AS uuid)
 RETURNING oper_state
""")

_NAME_BOTH_ENDS = text("""
    UPDATE alarm SET message = :message WHERE id = CAST(:id AS uuid)
""")


def _both_ends(a_dev: str, a_port: str, b_dev: str, b_port: str) -> str:
    return f"Link down: {a_dev} {a_port} <-> {b_dev} {b_port}"


async def find_link(session: AsyncSession, *, device_id: str,
                    instance: str) -> dict[str, Any] | None:
    """The connection this port terminates, if the model knows one."""
    if not instance:
        # An alarm that does not name a port cannot be matched to a cable. The
        # mapping now takes the port from the trap's ifDescr; alarms raised
        # before it did are not pairable, and guessing which of a switch's 48
        # ports was meant would be worse than leaving them apart.
        return None
    row = (await session.execute(_PEER_END, {
        "device_id": device_id, "instance": instance,
        "layers": list(PORT_LAYERS)})).mappings().first()
    return dict(row) if row else None


async def refresh_link_state(session: AsyncSession, *, device_id: str,
                             instance: str) -> str | None:
    """Drive the connection's oper_state from what its ends currently report."""
    link = await find_link(session, device_id=device_id, instance=instance)
    if not link:
        return None
    return await session.scalar(_REFRESH_STATE, {
        "connection_id": link["connection_id"], "types": list(LINK_TYPES)})


async def pair_ends(session: AsyncSession, *, alarm_id: str, device_id: str,
                    instance: str) -> dict[str, Any] | None:
    """Fold the second end of a failed link under the first.

    Returns the root alarm when THIS alarm was folded under the far end.
    Returns None when this end is the root - including when it is the only end
    reporting, which is a finding in itself and stays visible on its own.
    """
    link = await find_link(session, device_id=device_id, instance=instance)
    if not link or not link["peer_iface_id"]:
        return None

    peer = (await session.execute(_PEER_ALARM, {
        "peer_device": link["peer_device_id"],
        "peer_iface": link["peer_iface_id"],
        "types": list(LINK_TYPES), "alarm_id": alarm_id})).mappings().first()
    if not peer:
        return None

    me = (await session.execute(_ME, {"alarm_id": alarm_id})).mappings().first()
    if not me:
        return None

    # The end that saw it first is the root. Not severity, and not which end
    # happens to be upstream: on a cable the two ends are peers, and the
    # earlier observation is the one an operator should be reading.
    peer_first = peer["first_seen"] <= me["first_seen"]
    root_id = peer["id"] if peer_first else alarm_id
    symptom_id = alarm_id if peer_first else peer["id"]

    if peer_first:
        a_dev, a_port = peer["device_name"], peer["port"]
        b_dev, b_port = me["device_name"], instance
    else:
        a_dev, a_port = me["device_name"], instance
        b_dev, b_port = peer["device_name"], peer["port"]

    await session.execute(text("""
        UPDATE alarm
           SET is_symptom = true, root_cause_alarm_id = CAST(:root AS uuid)
         WHERE id = CAST(:id AS uuid)
    """), {"id": symptom_id, "root": root_id})
    await session.execute(_NAME_BOTH_ENDS, {
        "id": root_id, "message": _both_ends(a_dev, a_port, b_dev, b_port)})

    log.info("link ends paired", connection=link["connection_id"],
             root_alarm=root_id, symptom_alarm=symptom_id,
             link=f"{a_dev} {a_port} <-> {b_dev} {b_port}")

    if peer_first:
        return {"id": peer["id"], "device_name": peer["device_name"],
                "port": peer["port"], "connection_id": link["connection_id"]}
    return None
