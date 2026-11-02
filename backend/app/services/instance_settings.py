"""Instance-wide settings an administrator changes from the UI.

Today that is one thing: the organisation's name, shown on the Home headline,
the login card and the browser tab.

Where the name comes from, in order:

1. ``settings``    - saved by an admin in Settings > Organisation (this table);
2. ``environment`` - DCIM_ORG_NAME in the server's environment, the original
                     source and still the way to seed a fresh install;
3. ``default``     - the product name, so the heading is never empty.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PRODUCT_NAME = "DCIM Platform"
MAX_ORG_NAME = 80

Source = Literal["settings", "environment", "default"]


class OrgNameError(ValueError):
    """A name that cannot be saved, with a sentence a form can show."""


def clean_org_name(raw: str | None) -> str | None:
    """Normalise a submitted name, or refuse it.

    Runs of whitespace collapse to one space and the ends are trimmed, so a
    pasted name with a stray newline still reads as one line in a heading.
    Blank means "not set here" and returns None. Control characters are
    refused rather than stripped: they never belong in a name, and silently
    removing them would save something other than what was typed.
    """
    if raw is None:
        return None
    name = " ".join(raw.split())
    if not name:
        return None
    if any(unicodedata.category(c) == "Cc" for c in name):
        raise OrgNameError("The name contains a control character. Type it again without it.")
    if len(name) > MAX_ORG_NAME:
        raise OrgNameError(
            f"The name is {len(name)} characters; the limit is {MAX_ORG_NAME}, "
            "so it fits the Home heading and a browser tab.")
    return name


def resolve(stored: str | None, env_value: str | None) -> tuple[str, Source]:
    """The name to show, and which of the three sources it came from."""
    if stored:
        return stored, "settings"
    env = (env_value or "").strip()
    if env:
        return env, "environment"
    return PRODUCT_NAME, "default"


@dataclass(frozen=True)
class StoredOrg:
    org_name: str | None
    updated_at: datetime | None
    updated_by: str | None


async def load(session: AsyncSession) -> StoredOrg:
    row = (await session.execute(text(
        "SELECT org_name, updated_at, updated_by FROM instance_setting WHERE id = 1"
    ))).mappings().first()
    if row is None:
        return StoredOrg(None, None, None)
    return StoredOrg(row["org_name"], row["updated_at"], row["updated_by"])


async def save(session: AsyncSession, org_name: str | None, actor: str) -> None:
    """Upsert the single row. The caller owns the transaction (and the audit)."""
    await session.execute(text("""
        INSERT INTO instance_setting (id, org_name, updated_at, updated_by)
        VALUES (1, :name, now(), :actor)
        ON CONFLICT (id) DO UPDATE
           SET org_name = EXCLUDED.org_name,
               updated_at = EXCLUDED.updated_at,
               updated_by = EXCLUDED.updated_by
    """), {"name": org_name, "actor": actor})
