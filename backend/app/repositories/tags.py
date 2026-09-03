"""Tags: a controlled vocabulary over devices, racks and rooms.

`attributes` JSONB already exists on all three and is the right place for a
one-off value. It is the wrong place for something you want to filter and count
by, because there is no list of valid keys, no way to rename one, and nothing
stops `env: Prod`, `env: prod` and `environment: production` all appearing.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: The polymorphic target types. Validated here rather than by a foreign key,
#: the same way connection terminations are - and, like them, the list is short
#: and closed on purpose. An unchecked object_type is a row pointing at nothing.
OBJECT_TYPES = ("device", "rack", "room")


class UnknownObjectTypeError(ValueError):
    def __init__(self, object_type: str):
        super().__init__(
            f"'{object_type}' cannot be tagged; "
            f"expected one of {', '.join(OBJECT_TYPES)}")


async def list_tags(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT t.id::text, t.key, t.value, t.colour, t.description,
               count(a.tag_id) AS usage_count
        FROM tag t
        LEFT JOIN tag_assignment a ON a.tag_id = t.id
        GROUP BY t.id, t.key, t.value, t.colour, t.description
        ORDER BY t.key, t.value
    """))).mappings().all()
    return [dict(r) for r in rows]


async def create_tag(session: AsyncSession, *, key: str, value: str,
                     colour: str | None = None,
                     description: str | None = None) -> str:
    return (await session.execute(text("""
        INSERT INTO tag (key, value, colour, description)
        VALUES (:key, :value, :colour, :description)
        RETURNING id::text
    """), {"key": key, "value": value, "colour": colour,
           "description": description})).scalar_one()


async def update_tag(session: AsyncSession, tag_id: str,
                     changes: dict[str, Any]) -> None:
    if not changes:
        return
    sets = ", ".join(f"{k} = :{k}" for k in changes)
    await session.execute(
        text(f"UPDATE tag SET {sets} WHERE id = CAST(:id AS uuid)"),
        {**changes, "id": tag_id})


async def delete_tag(session: AsyncSession, tag_id: str) -> None:
    """Deleting a tag detaches it. The cascade does that, and nothing else -
    an object is never removed because a label was."""
    await session.execute(text("DELETE FROM tag WHERE id = CAST(:id AS uuid)"),
                          {"id": tag_id})


async def tags_for(session: AsyncSession, object_type: str,
                   object_id: str) -> list[dict[str, Any]]:
    if object_type not in OBJECT_TYPES:
        raise UnknownObjectTypeError(object_type)
    rows = (await session.execute(text("""
        SELECT t.id::text, t.key, t.value, t.colour
        FROM tag_assignment a JOIN tag t ON t.id = a.tag_id
        WHERE a.object_type = :object_type AND a.object_id = CAST(:object_id AS uuid)
        ORDER BY t.key, t.value
    """), {"object_type": object_type, "object_id": object_id})).mappings().all()
    return [dict(r) for r in rows]


async def assign(session: AsyncSession, *, object_type: str, object_id: str,
                 tag_ids: list[str], actor: str) -> int:
    if object_type not in OBJECT_TYPES:
        raise UnknownObjectTypeError(object_type)
    if not tag_ids:
        return 0
    await session.execute(text("""
        INSERT INTO tag_assignment (tag_id, object_type, object_id, assigned_by)
        SELECT CAST(t AS uuid), :object_type, CAST(:object_id AS uuid), :actor
        FROM unnest(CAST(:ids AS text[])) AS t
        ON CONFLICT DO NOTHING
    """), {"object_type": object_type, "object_id": object_id,
           "ids": tag_ids, "actor": actor})
    return len(tag_ids)


async def unassign(session: AsyncSession, *, object_type: str, object_id: str,
                   tag_id: str) -> None:
    if object_type not in OBJECT_TYPES:
        raise UnknownObjectTypeError(object_type)
    await session.execute(text("""
        DELETE FROM tag_assignment
        WHERE tag_id = CAST(:tag_id AS uuid)
          AND object_type = :object_type
          AND object_id = CAST(:object_id AS uuid)
    """), {"tag_id": tag_id, "object_type": object_type, "object_id": object_id})


async def tags_for_devices(session: AsyncSession,
                           device_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Tags for many devices at once.

    The asset table renders a tag column over 200 rows; asking per row is 200
    round trips behind one screen.
    """
    if not device_ids:
        return {}
    rows = (await session.execute(text("""
        SELECT a.object_id::text AS device_id, t.id::text, t.key, t.value, t.colour
        FROM tag_assignment a JOIN tag t ON t.id = a.tag_id
        WHERE a.object_type = 'device'
          AND a.object_id = ANY(CAST(:ids AS uuid[]))
        ORDER BY t.key, t.value
    """), {"ids": device_ids})).mappings().all()
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        out.setdefault(item.pop("device_id"), []).append(item)
    return out
