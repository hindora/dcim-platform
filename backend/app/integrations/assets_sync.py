"""Running one Assets export.

**NOT VERIFIED AGAINST A REAL TENANT.** Assets needs JSM Premium or
Enterprise, which no environment here has. The flow is written from
Atlassian's published Imports workflow and exercised against a recorded
double; the structure below is deliberately shaped so that a surprise in the
protocol is a change to `jira/assets.py` and nothing else.

THE ORDER OF OPERATIONS IS THE WHOLE DESIGN:

1. Discover the workspace. Absent means the site is on Standard, which is a
   LICENCE fact and is reported as one.
2. Build or reuse the type map - name to numeric attribute id - because
   everything in the Assets API is addressed by id and rediscovering it per
   object turns one sync into thousands of calls.
3. Check our attributes against the schema BEFORE sending anything. An import
   naming an attribute the schema lacks fails the whole batch, so a
   1,500-device run would otherwise die on the first chunk over one field
   somebody renamed in Jira.
4. Push parents whole, devices incrementally, in schema order.
5. Advance the cursor only on a completed run.

WHY A FAILED RUN RE-SENDS. The cursor is a high-water mark on
`device.updated_at` and it does not move unless the run reports completion.
Re-sending is free - the import matches on the object key and updates in place
- whereas advancing early loses a machine until somebody happens to touch it
again, which on a decommissioned rack is never.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.integrations.jira import assets
from app.integrations.jira import assets_schema as schema
from app.integrations.jira.client import JiraClient, JiraError
from app.repositories import integrations as repo

log = get_logger("integrations.assets")


class AssetsNotConfiguredError(JiraError):
    """The operator has not finished the Jira-side setup."""


async def run(session_factory: Any, integration: dict[str, Any], *,
              full: bool = False) -> dict[str, Any]:
    """One export. Returns what it did, for the log and the settings page.

    Takes a session FACTORY rather than a session: a full export of a large
    estate holds the connection for as long as the push takes, and a
    transaction open across several minutes of somebody else's HTTP is how a
    pool gets exhausted.
    """
    integration_id = integration["id"]
    async with session_factory() as session:
        state = await repo.assets_state(session, integration_id)
        blob = await repo.assets_secret(session, integration_id)

    if not state.get("schema_id"):
        raise AssetsNotConfiguredError(
            "no Assets object schema is configured - create one in Jira and "
            "name it here")
    if not blob:
        raise AssetsNotConfiguredError(
            "no import token - create an import source in the Assets schema "
            "and paste its token here")

    from app.services import integrations as service
    async with session_factory() as session:
        site = await service.build_client(session, integration)
    imports = JiraClient(base_url="", secret=decrypt_secret(blob),
                         secret_kind="pat")

    pushed, cursor = 0, state.get("last_cursor")
    try:
        cached, _workspace_id = await _type_map(
            session_factory, site, integration_id, state)

        gaps = assets.missing_attributes(cached)
        if gaps:
            # Refused BEFORE anything is sent. The alternative is a partial
            # estate in Assets and a rejection on chunk one of six.
            raise AssetsNotConfiguredError(
                "the Assets schema is missing " + ", ".join(gaps[:8])
                + (f" and {len(gaps) - 8} more" if len(gaps) > 8 else ""))

        since = None if full else state.get("last_cursor")
        async with session_factory() as session:
            snapshot = await repo.inventory_for_assets(session, since=since)
        rows = schema.rows_for(snapshot["inventory"])
        cursor = snapshot["cursor"]

        # Nothing to do is decided on DEVICES, not on rows: the parents -
        # datacentres, rooms, racks, vendors, models - are always sent whole,
        # so `rows` is never empty and an idle nightly sync would otherwise
        # run a full import of a hundred-odd objects to say nothing changed,
        # against an API that rate-limits external imports separately.
        #
        # The known cost: a room renamed with no device touched waits for the
        # next device change or a manual Resync. That is the right way round -
        # a stale room name for a day is cheaper than a nightly no-op import
        # and a run history in which every line says work was done.
        if not full and not snapshot["devices_changed"]:
            log.info("assets export: no device changed since the last run",
                     integration=integration["name"])
            await _finish(session_factory, integration_id, "ok", 0, cursor)
            return {"pushed": 0, "devices_changed": 0, "skipped": True}
        if not rows:
            await _finish(session_factory, integration_id, "ok", 0, cursor)
            return {"pushed": 0, "devices_changed": 0, "skipped": True}

        pushed = await _push(imports, cached, state["schema_id"], rows)
        await _finish(session_factory, integration_id, "ok", pushed, cursor)
        log.info("assets export complete", integration=integration["name"],
                 objects=pushed, devices=snapshot["devices_changed"],
                 full=full)
        return {"pushed": pushed,
                "devices_changed": snapshot["devices_changed"], "full": full}

    except Exception as exc:
        # The cursor deliberately does NOT move. See the module docstring.
        await _finish(session_factory, integration_id, "failed", pushed,
                      cursor=None, error=f"{type(exc).__name__}: {exc}")
        log.error("assets export failed", integration=integration["name"],
                  error=str(exc))
        raise
    finally:
        await imports.aclose()
        await site.aclose()


async def _type_map(session_factory: Any, site: JiraClient,
                    integration_id: str, state: dict[str, Any]
                    ) -> tuple[dict[str, Any], str]:
    """The cached map, or one walk to build it.

    Rebuilt whenever it is empty rather than on a timer: the expensive case is
    the first run, and an operator who has just added an attribute presses
    Resync rather than waiting for a cache to expire.
    """
    workspace_id = state.get("workspace_id")
    cached = state.get("type_map") or {}
    if cached and workspace_id:
        return cached, workspace_id

    workspace_id = workspace_id or await assets.discover_workspace(site)
    cached = await assets.type_map(site, workspace_id, state["schema_id"])
    async with session_factory() as session:
        await repo.cache_type_map(session, integration_id,
                                  workspace_id=workspace_id, type_map=cached)
    return cached, workspace_id


async def _push(imports: JiraClient, cached: dict[str, Any], schema_id: str,
                rows: list[dict[str, Any]]) -> int:
    run_ = assets.ImportRun(imports)
    await run_.begin()
    await run_.put_mapping(assets.mapping_document(cached, schema_id))
    await run_.start()

    batches = assets.chunks(rows)
    pushed = 0
    try:
        for index, batch in enumerate(batches, start=1):
            await run_.progress(step=index, steps=len(batches),
                                description=f"Sending objects "
                                            f"{pushed + 1}-{pushed + len(batch)}",
                                processed=pushed, total=len(rows))
            # One id per chunk, stable within this run: a chunk re-sent after
            # a timeout must not be applied twice.
            await run_.submit(batch, chunk_id=f"{uuid.uuid4()}-{index}")
            pushed += len(batch)
        await run_.finish()
    except Exception:
        # An import left RUNNING blocks every later one on the same source,
        # and the next nightly sync would refuse rather than recover.
        await run_.cancel()
        raise
    return pushed


async def _finish(session_factory: Any, integration_id: str, status: str,
                  pushed: int, cursor: Any = None,
                  error: str | None = None) -> None:
    async with session_factory() as session:
        await repo.finish_assets_run(session, integration_id, status=status,
                                     pushed=pushed, cursor=cursor, error=error)
