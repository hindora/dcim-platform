"""Pushing inventory into JSM Assets, through the Imports REST API.

**THIS IS THE ONE PART OF THE INTEGRATION THAT HAS NEVER RUN AGAINST A REAL
TENANT.** Assets needs JSM Premium or Enterprise, which no environment here
has, so the flow below is written from Atlassian's published workflow and is
exercised against a recorded double. Every call is marked with what it is
believed to do; anything that turns out to differ is a bug in this file and
not in the caller, which is why the flow is isolated here.

WHY THE IMPORTS API AND NOT `object/create`. Creating objects one at a time is
the obvious route and it is wrong at this scale: 1,500 devices is 1,500
requests against an API that rate-limits external imports separately from
everything else, with no idempotency, no progress and no mapping. The Imports
API is the only route that is incremental, mapped, idempotent AND
progress-reporting, which is what a nightly sync of a live estate needs.

    GET  /jsm/assets/v1/imports/info       -> HATEOAS links, bound to the token
    GET  {getStatus}                       -> IDLE | RUNNING | DISABLED | MISSING_MAPPING
    PUT  {mapping}                         -> our schema, once
    POST {start}                           -> execution links
    PUT  {submitProgress}                  -> steps and object counts
    POST {submitResults}                   -> the objects, then {"completed": true}

THE LINKS ARE NOT URLS TO KEEP. Atlassian's guide is explicit that the URLs
returned by `/imports/info` are bound to the token that produced them and must
not be stored or hand-edited. So `assets_sync_state` caches the TYPE MAP,
which is expensive to rediscover, and never the links, which are cheap.

A SECOND CREDENTIAL. The Imports API is authenticated by a token the operator
creates against one import source inside Jira, not by the account credential
the rest of this integration uses. That is a prerequisite an operator has to
perform by hand, exactly like the webhook, and the settings page says so in
those words rather than offering a button that cannot work.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.integrations.jira import assets_schema as schema
from app.integrations.jira.client import JiraClient, JiraError

log = get_logger("integrations.jira.assets")

#: Where a workspace id is discovered. Present on Premium and Enterprise; a
#: 403 or 404 here is a LICENCE fact rather than a misconfiguration, and the
#: difference is what the connection test reports.
WORKSPACE = "/rest/servicedeskapi/assets/workspace"

#: The Imports API lives on api.atlassian.com rather than on the site host,
#: and is reached with the import configuration's own token.
IMPORTS_BASE = "https://api.atlassian.com/jsm/assets/v1/imports"

#: Objects per `submitResults` call. Atlassian does not publish a hard limit;
#: this is sized so a failed chunk is cheap to re-send and a 1,500-device
#: estate is a handful of calls rather than one enormous body.
CHUNK = 250


class AssetsUnavailableError(JiraError):
    """This site has no Assets. A licence fact, not a mistake."""


async def discover_workspace(client: JiraClient) -> str:
    """The workspace id, or a refusal that says which kind of problem it is.

    A 403 or a 404 here means the site is on Standard, where Assets does not
    exist. Reporting that as "the connection failed" would send somebody
    looking for a credential problem that is not there.
    """
    try:
        result = await client.get(WORKSPACE)
    except JiraError as exc:
        if exc.status in (401, 403, 404):
            raise AssetsUnavailableError(
                "this Jira site has no Assets - it needs JSM Premium or "
                "Enterprise", status=exc.status) from exc
        raise
    values = (result or {}).get("values") or []
    if not values:
        raise AssetsUnavailableError(
            "this Jira site reports no Assets workspace - it needs JSM "
            "Premium or Enterprise")
    return str(values[0].get("workspaceId") or values[0].get("id"))


async def type_map(client: JiraClient, workspace_id: str, schema_id: str
                   ) -> dict[str, Any]:
    """Walk the object schema once and cache name -> numeric id.

    THE reason this exists: everything in the Assets API is addressed by
    numeric attribute id, never by name, so without this every object pushed
    would need its own attribute walk first.

    Built from the schema in Jira rather than from ours, because the operator
    may have added attributes of their own and an import that names one this
    platform invented is rejected whole.
    """
    base = (f"https://api.atlassian.com/jsm/assets/workspace/{workspace_id}"
            f"/v1")
    types = await client.get(f"{base}/objectschema/{schema_id}/objecttypes/flat")
    out: dict[str, Any] = {}
    for object_type in types or []:
        name = object_type.get("name")
        type_id = object_type.get("id")
        if not name or not type_id:
            continue
        attributes = await client.get(
            f"{base}/objecttype/{type_id}/attributes")
        out[name] = {
            "id": str(type_id),
            "attributes": {a.get("name"): str(a.get("id"))
                           for a in attributes or []
                           if a.get("name") and a.get("id")},
        }
    log.info("assets type map built", schema=schema_id, types=len(out))
    return out


def missing_attributes(cached: dict[str, Any]) -> list[str]:
    """Which of the attributes this platform sends the schema does not have.

    Checked BEFORE a run rather than discovered by a rejection: an Assets
    import that names an attribute the schema lacks fails the whole batch, so
    a 1,500-device sync dies on the first chunk over one field somebody
    renamed.
    """
    gaps: list[str] = []
    for entry in schema.SCHEMA:
        known = (cached.get(entry["label"]) or {}).get("attributes") or {}
        if not known:
            gaps.append(f"{entry['label']} (object type is missing)")
            continue
        for label, _field, _kind in entry["attributes"]:
            if label not in known:
                gaps.append(f"{entry['label']}.{label}")
    return gaps


# --------------------------------------------------------------- the import

class ImportRun:
    """One execution of the Imports API's HATEOAS workflow.

    The links are followed, never constructed. Atlassian's guide is explicit
    that they are bound to the token that produced them, and a stored or
    hand-edited link is the failure that looks like an authentication problem
    a week later.
    """

    def __init__(self, client: JiraClient) -> None:
        self.client = client
        self.links: dict[str, str] = {}
        self.execution: dict[str, str] = {}

    async def begin(self) -> str:
        info = await self.client.get(IMPORTS_BASE + "/info")
        self.links = dict((info or {}).get("links") or {})
        if not self.links.get("start"):
            raise JiraError(
                "the import configuration returned no start link - is the "
                "token for an import source that still exists?")
        status = await self._follow("getStatus")
        state = str((status or {}).get("status") or "UNKNOWN")
        if state == "RUNNING":
            # Two runs against one import source interleave their objects and
            # the second one's completion ends the first. Better to skip a
            # nightly sync than to corrupt one.
            raise JiraError("an import is already running for this source")
        if state == "DISABLED":
            raise JiraError("this import source is disabled in Jira")
        return state

    async def put_mapping(self, mapping: dict[str, Any]) -> None:
        await self.client.put(self.links["mapping"], json_body=mapping)

    async def start(self) -> None:
        result = await self.client.post(self.links["start"], json_body={})
        self.execution = dict(result or {})
        for needed in ("submitResults", "submitProgress"):
            if not self.execution.get(needed):
                raise JiraError(f"the import started but returned no {needed} link")

    async def progress(self, *, step: int, steps: int, description: str,
                       processed: int, total: int) -> None:
        """Tell Jira how far along this is.

        Optional to the protocol and not to the operator: without it an import
        of a large estate shows as a spinner for several minutes and somebody
        cancels it.
        """
        try:
            await self.client.put(self.execution["submitProgress"], json_body={
                "steps": {"total": steps, "current": step,
                          "description": description},
                "objects": {"total": total, "processed": processed},
            })
        except JiraError as exc:
            # Cosmetic. Failing the run over a progress report would be
            # throwing away the work to avoid a missing progress bar.
            log.info("could not report import progress", error=str(exc))

    async def submit(self, rows: list[dict[str, Any]],
                     *, chunk_id: str) -> None:
        await self.client.post(self.execution["submitResults"], json_body={
            "data": {"objects": rows},
            # The idempotency key. A chunk re-sent after a timeout must not
            # be applied twice, and this is the only thing that prevents it.
            "clientGeneratedId": chunk_id,
        })

    async def finish(self) -> None:
        await self.client.post(self.execution["submitResults"],
                               json_body={"completed": True})

    async def cancel(self) -> None:
        link = self.execution.get("cancel")
        if not link:
            return
        try:
            await self.client.post(link, json_body={})
        except JiraError as exc:
            log.warning("could not cancel a failed import", error=str(exc))

    async def _follow(self, name: str) -> Any:
        link = self.links.get(name)
        if not link:
            raise JiraError(f"the import configuration returned no {name} link")
        return await self.client.get(link)


def mapping_document(cached: dict[str, Any], schema_id: str) -> dict[str, Any]:
    """The schema-to-Assets mapping, built from the cached type map.

    Sent once per run rather than assumed: an operator who adds an attribute
    between runs gets it populated on the next one without anybody
    reconfiguring this platform.
    """
    object_types = []
    for entry in schema.SCHEMA:
        known = (cached.get(entry["label"]) or {}).get("attributes") or {}
        attributes = [
            {"objectTypeAttributeId": known[label], "attributeName": label,
             "external": field}
            for label, field, _kind in entry["attributes"] if label in known
        ]
        if not attributes:
            continue
        object_types.append({
            "objectTypeId": cached[entry["label"]]["id"],
            "objectTypeName": entry["label"],
            "selector": entry["key"],
            "description": f"DCIM {entry['label']}",
            "attributesMapping": attributes,
            "labelAttribute": entry["label_attribute"],
        })
    return {"schemaId": schema_id, "objectTypeMappings": object_types}


def chunks(rows: list[dict[str, Any]], size: int = CHUNK
           ) -> list[list[dict[str, Any]]]:
    return [rows[i:i + size] for i in range(0, len(rows), size)]
