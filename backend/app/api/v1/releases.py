"""Collector releases, upgrades and rollouts - the operator side (docs/26 Phase 7).

Mounted under /collectors BEFORE the collectors router, so /collectors/releases
is not read as a collector called "releases".
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.repositories import commands as repo
from app.services import rollout as rollout_svc

router = APIRouter(prefix="/collectors", tags=["collector releases"])
log = get_logger("api.releases")

_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


def verify_signature(sha256_hex: str, signature_b64: str, pubkey_b64: str) -> bool:
    """Ed25519 over the 32 raw digest bytes - the same check the collector
    makes before it runs anything."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64))
        key.verify(base64.b64decode(signature_b64), bytes.fromhex(sha256_hex))
        return True
    except (InvalidSignature, ValueError):
        return False


@router.get("/releases", summary="Signed collector releases")
async def list_releases(session: AsyncSession = Depends(get_session),
                        _: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {"releases": await repo.releases(session)}


@router.post("/releases", status_code=status.HTTP_201_CREATED,
             summary="Publish a signed collector build")
async def publish_release(
    request: Request,
    version: str = Form(...),
    signature: str = Form(...),
    key_id: str = Form(...),
    notes: str = Form(""),
    artifact: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """The platform stores and serves the build; it cannot sign one. The
    signature is made offline (scripts/publish_release.py) and checked here
    only if the platform has the key - the collector checks it regardless."""
    if not _VERSION.match(version):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not a valid version string")
    data = await artifact.read()
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty artefact")
    digest = hashlib.sha256(data).hexdigest()
    trusted = settings.release_trusted_keys.get(key_id)
    if settings.release_trusted_keys and trusted is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"key {key_id!r} is not one this platform trusts")
    if trusted is not None and not verify_signature(digest, signature, trusted):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "signature does not verify against that key")
    rel_path = os.path.join(version, "collector")
    full = os.path.join(settings.release_dir, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)
    if not await repo.add_release(session, {
            "version": version, "sha256": digest, "signature": signature, "key_id": key_id,
            "size_bytes": len(data), "path": rel_path, "notes": notes or None,
            "actor": principal.username}):
        raise HTTPException(status.HTTP_409_CONFLICT, f"release {version} already exists")
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal), action="release.publish",
                       target_type="collector_release", target_id=version, ip=ip,
                       user_agent=agent, after={"sha256": digest, "key_id": key_id,
                                                "size_bytes": len(data),
                                                "verified_by_platform": trusted is not None})
    await session.commit()
    log.info("release published", version=version, sha256=digest, actor=principal.username)
    return {"version": version, "sha256": digest, "size_bytes": len(data),
            "verified_by_platform": trusted is not None}


class UpgradeBody(BaseModel):
    model_config = {"extra": "forbid"}
    version: str = Field(min_length=1, max_length=64)


@router.post("/{collector_id}/upgrade", summary="Upgrade one collector now")
async def upgrade_one(collector_id: str, body: UpgradeBody, request: Request,
                      session: AsyncSession = Depends(get_session),
                      principal: Principal = Depends(require_role("admin"))) -> dict[str, Any]:
    """Outside any rollout - the operator's own call, e.g. a single-member
    pool on a quiet night. A rollout is the safe path for a pool."""
    rel = await repo.get_release(session, body.version)
    if rel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such release")
    known = await session.scalar(text(
        "SELECT state FROM collector_instance WHERE id = :id"), {"id": collector_id})
    if known is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such collector")
    if known == "decommissioned":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "a decommissioned collector cannot be upgraded")
    if await repo.open_for(session, collector_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "it already has a command in progress")
    cid = await repo.create(session, collector_id, "upgrade",
                            rollout_svc.upgrade_payload(rel), principal.username)
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal), action="collector.upgrade",
                       target_type="collector", target_id=collector_id, ip=ip,
                       user_agent=agent, after={"version": body.version, "command_id": cid})
    await session.commit()
    return {"command_id": cid, "collector_id": collector_id, "version": body.version}


@router.get("/commands", summary="Recent collector commands")
async def list_commands(session: AsyncSession = Depends(get_session),
                        _: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {"commands": await repo.recent(session)}


class RolloutBody(BaseModel):
    model_config = {"extra": "forbid"}
    version: str = Field(min_length=1, max_length=64)
    pool_ids: list[str] = Field(min_length=1)


@router.post("/rollouts", status_code=status.HTTP_201_CREATED,
             summary="Roll a release across pools, one member per pool at a time")
async def start_rollout(body: RolloutBody, request: Request,
                        session: AsyncSession = Depends(get_session),
                        principal: Principal = Depends(require_role("admin"))) -> dict[str, Any]:
    if await repo.get_release(session, body.version) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such release")
    known = set((await session.execute(text(
        "SELECT id::text FROM collector_pool WHERE id::text = ANY(:ids)"),
        {"ids": body.pool_ids})).scalars().all())
    unknown = sorted(set(body.pool_ids) - known)
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"no such pool: {unknown}")
    for r in await repo.rollouts(session, running_only=True):
        overlap = set(r["pool_ids"]) & set(body.pool_ids)
        if overlap:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"rollout {r['id']} is already running over those pools")
    rid = await repo.create_rollout(session, body.version, body.pool_ids, principal.username)
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal), action="rollout.start",
                       target_type="collector_rollout", target_id=rid, ip=ip, user_agent=agent,
                       after={"version": body.version, "pool_ids": body.pool_ids})
    await session.commit()
    return {"id": rid, "version": body.version, "pool_ids": body.pool_ids}


@router.get("/rollouts", summary="Rollouts, with every member's version and command")
async def list_rollouts(session: AsyncSession = Depends(get_session),
                        _: Principal = Depends(current_principal)) -> dict[str, Any]:
    out = []
    for r in await repo.rollouts(session):
        out.append({**r, "commands": await repo.rollout_commands(session, r["id"])})
    fleet = (await session.execute(text("""
        SELECT ci.id, ci.version, ci.state, cp.name AS pool, ci.pool_id::text AS pool_id,
               extract(epoch FROM (clock_timestamp() - ci.last_heartbeat)) AS heartbeat_age_s
          FROM collector_instance ci LEFT JOIN collector_pool cp ON cp.id = ci.pool_id
         WHERE ci.state <> 'decommissioned' ORDER BY ci.id
    """))).mappings().all()
    return {"rollouts": out, "fleet": [dict(f) for f in fleet]}


@router.post("/rollouts/{rollout_id}/cancel", summary="Stop a rollout")
async def cancel_rollout(rollout_id: str, request: Request,
                         session: AsyncSession = Depends(get_session),
                         principal: Principal = Depends(require_role("admin"))) -> dict[str, Any]:
    """Stops issuing new upgrades. A member already upgrading finishes (or
    rolls itself back) on its own - it cannot be interrupted mid-swap."""
    await repo.set_rollout_state(session, rollout_id, "cancelled",
                                 f"cancelled by {principal.username}")
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal), action="rollout.cancel",
                       target_type="collector_rollout", target_id=rollout_id, ip=ip,
                       user_agent=agent)
    await session.commit()
    return {"id": rollout_id, "state": "cancelled"}
