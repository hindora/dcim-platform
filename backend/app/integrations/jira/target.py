"""The issue lifecycle: what one outbox row does to one ticket.

The decision tree, once, because the code below is the same tree and reads
better with it in view:

    no link          + raised     -> create (after a recovery search on retry)
    no link          + cleared    -> nothing; the condition never earned one
    open link        + raised     -> re-assert: bump the occurrence field
    open link        + escalated  -> comment, and raise the priority
    open link        + cleared    -> comment, and transition if configured
    closed link      + cleared    -> nothing; it is already closed
    closed, in window, reopenable -> transition back, comment
    closed, out of window         -> create a NEW issue, linked to the old

TWO THINGS THAT LOOK LIKE DETAILS AND ARE NOT:

**The recovery search.** A dispatcher can die between Jira accepting a create
and our own commit recording the key. On the next attempt there is no link and
the naive path creates a twin. So any attempt after the first searches for the
fingerprint label first. It costs one JQL call, only on a retry, and it is the
difference between "the integration duplicated everything during the outage"
and nobody noticing there was one.

**`wont_reopen`.** An issue a human resolved as Won't Fix or Duplicate is a
decision, and re-opening it because the sensor fired again is how an
integration gets turned off. Those conditions get a fresh issue instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.logging import get_logger
from app.integrations import adf, change
from app.integrations import fingerprint as fp
from app.integrations.jira import mapping
from app.integrations.jira.client import JiraClient, JiraError

log = get_logger("integrations.jira.target")

SERVICEDESK = "/rest/servicedeskapi"

#: The REST version is a property of the DEPLOYMENT, not of this product.
#: Cloud serves v3; Data Center serves v2 and has no v3 at all, so a v3 path
#: against Data Center is a 404 on every single call.
API_FOR = {"jira_cloud": "/rest/api/3", "jira_dc": "/rest/api/2"}

#: Cloud's default. Kept as a module constant because callers outside the
#: target - the connection test - need it before an IssueTarget exists.
API = API_FOR["jira_cloud"]


def api_for(deployment: str | None) -> str:
    """Unknown deployments get Cloud, which is the only one with a working
    ticket path today."""
    return API_FOR.get(deployment or "", API)


def search_for(deployment: str | None) -> str:
    """Where a JQL search lives, which is NOT the same endpoint on both.

    `POST /rest/api/3/search` and its GET twin were removed from Cloud in
    October 2025 and answer 410 Gone; `/search/jql` is the replacement, and it
    differs in two ways that break a naive port: `fields` is no longer
    defaulted, and paging is a cursor on `nextPageToken` rather than
    startAt/total.

    Data Center had none of that. It still serves `/rest/api/2/search` and has
    no `/search/jql` at all, so pointing it at the replacement 404s.
    """
    base = api_for(deployment)
    return f"{base}/search/jql" if base == API else f"{base}/search"


#: Cloud's default search path, for the same reason `API` is kept.
SEARCH = search_for("jira_cloud")

#: Status categories Jira reports. Only `done` is branched on, because status
#: NAMES are per-workflow and a customer who renamed Done to Resolved would
#: break every comparison against the name.
DONE = "done"


def issue_key_of(link: dict[str, Any]) -> str:
    return str(link["issue_key"])


@dataclass(slots=True)
class WindowUpdate:
    """What to write back to `maintenance_window` after a successful call."""

    issue_key: str
    issue_id: str | None = None


@dataclass(slots=True)
class LinkUpdate:
    """What to write to `jira_link` after a successful call."""

    issue_key: str
    issue_id: str | None = None
    status: str | None = None
    status_category: str | None = None
    resolution: str | None = None
    closed_at: datetime | None = None
    reopened: bool = False
    wont_reopen: bool | None = None


class IssueTarget:
    """Jira issues, on either Jira Software or a JSM service project."""

    def __init__(self, client: JiraClient, cfg: dict[str, Any], *,
                 base_url: str, dcim_base: str | None = None,
                 deployment: str = "jira_cloud") -> None:
        self.client = client
        self.cfg = cfg
        self.base_url = base_url
        self.dcim_base = dcim_base
        self.deployment = deployment
        self.api = api_for(deployment)
        self.search = search_for(deployment)

    @property
    def adf_bodies(self) -> bool:
        """Whether rich text goes as ADF or as flat text.

        ADF is a v3 concept. Data Center's v2 takes `description` and
        `comment.body` as STRINGS, and handing it an ADF document there does
        not render badly - it is rejected, or stored as the literal JSON.
        """
        return self.api == API

    def rich(self, body: dict[str, Any]) -> Any:
        """Render one built document for whichever deployment this is."""
        return body if self.adf_bodies else adf.to_text(body)

    @property
    def is_servicedesk(self) -> bool:
        return bool(self.cfg.get("service_desk_id")
                    and self.cfg.get("request_type_id"))

    # ------------------------------------------------------------- entry

    async def handle(self, *, kind: str, alarm: dict[str, Any], print_: str,
                     link: dict[str, Any] | None, attempts: int = 1,
                     now: datetime | None = None) -> LinkUpdate | None:
        """Apply one outbox row. Returns what to persist, or None for a no-op.

        Raises `JiraError` on failure; scheduling the retry is the
        dispatcher's job, because only it holds the durable attempt count.
        """
        now = now or datetime.now(UTC)

        if link is None:
            if attempts > 1:
                recovered = await self._find_by_fingerprint(print_)
                if recovered:
                    log.info("recovered an issue created before a crash",
                             issue=recovered["key"], fingerprint=print_)
                    link = {"issue_key": recovered["key"],
                            "issue_id": recovered.get("id"),
                            "closed_at": None, "wont_reopen": False,
                            "last_pushed_at": None}
            if link is None:
                if kind == "alarm_cleared":
                    return None
                return await self._create(alarm, print_)

        if link.get("closed_at") is None:
            return await self._on_open(kind, alarm, print_, link, now)
        return await self._on_closed(kind, alarm, print_, link, now)

    # -------------------------------------------------------- open ticket

    async def _on_open(self, kind: str, alarm: dict[str, Any], print_: str,
                       link: dict[str, Any], now: datetime) -> LinkUpdate | None:
        issue_key = link["issue_key"]

        if kind == "alarm_cleared":
            await self._comment(issue_key, kind, alarm)
            await self._push_remote_link(alarm, print_, resolved=True,
                                         issue_key=issue_key)
            if self.cfg.get("close_on_clear") == "transition":
                moved = await self._transition_to(
                    issue_key, self.cfg.get("clear_transition") or "Done")
                if moved:
                    return LinkUpdate(issue_key, closed_at=now,
                                      status_category=DONE)
                # The transition was not offered from the issue's current
                # status - somebody moved it by hand, or the workflow has a
                # gate. Not an error: the comment landed, the fact is on the
                # ticket, and forcing a transition that a workflow refuses is
                # not this integration's business.
                log.info("clear transition not available", issue=issue_key,
                         transition=self.cfg.get("clear_transition"))
            return LinkUpdate(issue_key)

        if self._suppressed(link, kind, alarm, now):
            log.debug("update suppressed inside the quiet window",
                      issue=issue_key, kind=kind)
            return None

        if kind == "alarm_escalated":
            await self._set_priority(issue_key, alarm)
            await self._comment(issue_key, kind, alarm)
            return LinkUpdate(issue_key)

        # A re-assert on a ticket that is still open. One field write, no
        # comment: the condition being true again is what an occurrence count
        # is for, and a comment per occurrence is how a ticket becomes
        # unreadable before anybody opens it.
        fields = mapping.occurrence_fields(alarm, self.cfg)
        if fields:
            await self._update_fields(issue_key, fields)
        return LinkUpdate(issue_key)

    def _suppressed(self, link: dict[str, Any], kind: str,
                    alarm: dict[str, Any], now: datetime) -> bool:
        """Has this ticket been written to too recently to write again?

        A clear is never suppressed - it is the message that ends the
        conversation - and neither is an escalation to CRITICAL, because the
        whole point of the quiet window is to hide noise, and a condition
        becoming critical is not noise.
        """
        window = int(self.cfg.get("suppress_window_s") or 0)
        if not window or kind == "alarm_cleared":
            return False
        if str(alarm.get("severity")) == "CRITICAL":
            return False
        last = link.get("last_pushed_at")
        if not isinstance(last, datetime):
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return (now - last).total_seconds() < window

    # ------------------------------------------------------ closed ticket

    async def _on_closed(self, kind: str, alarm: dict[str, Any], print_: str,
                         link: dict[str, Any], now: datetime
                         ) -> LinkUpdate | None:
        if kind == "alarm_cleared":
            # The ticket is already closed - a human got there first - and
            # there is nothing to transition. But there IS something worth
            # saying, and it is the other half of this integration's central
            # rule: a closed ticket is not a cleared fault, so whoever closed
            # this one did it without knowing whether the condition had gone.
            # This is the confirmation that it has.
            await self._comment(issue_key_of(link), "alarm_confirmed", alarm)
            await self._push_remote_link(alarm, print_, resolved=True,
                                         issue_key=issue_key_of(link))
            return LinkUpdate(issue_key_of(link))

        if link.get("wont_reopen"):
            log.info("declined issue not reopened; opening a new one",
                     issue=link["issue_key"], fingerprint=print_)
            return await self._create(alarm, print_, supersedes=link["issue_key"])

        window_h = int(self.cfg.get("reopen_window_h") or 0)
        closed_at = link.get("closed_at")
        if isinstance(closed_at, datetime) and closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=UTC)
        within = (window_h > 0 and isinstance(closed_at, datetime)
                  and now - closed_at <= timedelta(hours=window_h))
        if not within:
            return await self._create(alarm, print_, supersedes=link["issue_key"])

        reopened = await self._reopen(link["issue_key"])
        if not reopened:
            # No transition out of Done is offered - a workflow with no way
            # back, which is common on service projects. A new issue is the
            # only honest option.
            return await self._create(alarm, print_, supersedes=link["issue_key"])
        await self._comment(link["issue_key"], "alarm_reopened", alarm)
        await self._push_remote_link(alarm, print_, resolved=False,
                                     issue_key=link["issue_key"])
        return LinkUpdate(link["issue_key"], reopened=True)

    # ------------------------------------------------------------ create

    async def _create(self, alarm: dict[str, Any], print_: str, *,
                      supersedes: str | None = None) -> LinkUpdate:
        dcim_url = mapping.alarm_url(self.dcim_base, alarm)
        body = mapping.description(alarm, dcim_url=dcim_url)

        if self.is_servicedesk:
            created = await self._create_request(alarm, print_, body)
        else:
            created = await self._create_issue(alarm, print_, body, dcim_url)

        issue_key = created["key"]
        log.info("issue created", issue=issue_key, fingerprint=print_,
                 severity=alarm.get("severity"), device=alarm.get("device_name"))

        # Best effort, all three. The ticket exists and carries the whole
        # story; failing the outbox row now would retry the CREATE and make a
        # second ticket, which is far worse than a missing back-link.
        await self._push_remote_link(alarm, print_, resolved=False,
                                     issue_key=issue_key)
        if supersedes:
            await self._relate(issue_key, supersedes)
        return LinkUpdate(issue_key, issue_id=created.get("id"))

    async def _create_issue(self, alarm: dict[str, Any], print_: str,
                            body: dict[str, Any], dcim_url: str | None
                            ) -> dict[str, Any]:
        fields = mapping.fields_for(alarm, self.cfg, print_, dcim_url=dcim_url)
        fields["project"] = {"key": self.cfg["project_key"]}
        fields["issuetype"] = {"name": self.cfg.get("issue_type") or "Task"}
        fields["description"] = self.rich(body)
        payload: dict[str, Any] = {
            "fields": fields,
            # Structured, invisible in the UI, and searchable from JQL as
            # issue.property["dcim.alarm"].fingerprint. The label carries the
            # same key for speed; this carries the context a human debugging
            # the integration actually needs.
            "properties": [{
                "key": "dcim.alarm",
                "value": {
                    "fingerprint": print_,
                    "alarm_id": alarm.get("id"),
                    "device_id": alarm.get("device_id"),
                    "alarm_type": alarm.get("alarm_type"),
                    "instance": alarm.get("instance") or "",
                    "raised": mapping.iso(alarm.get("first_seen")),
                },
            }],
        }
        prop = None
        if not self.adf_bodies:
            # Data Center's v2 create is not documented to accept `properties`
            # inline, and a create rejected over a field nobody reads loses
            # the ticket entirely. The dedicated property endpoint exists on
            # both deployments, so set it after the issue is safely created.
            prop = payload.pop("properties")[0]

        created = await self.client.post(f"{self.api}/issue",
                                         json_body=payload)

        if prop and created.get("key"):
            try:
                await self.client.put(
                    f"{self.api}/issue/{created['key']}/properties/"
                    f"{prop['key']}", json_body=prop["value"])
            except JiraError as exc:
                # The label carries the same fingerprint and the search reads
                # THAT, so the integration still de-duplicates without this.
                log.info("could not set the issue property",
                         issue=created.get("key"), error=str(exc))
        return created

    async def _create_request(self, alarm: dict[str, Any], print_: str,
                              body: dict[str, Any]) -> dict[str, Any]:
        """A JSM customer request, which is not the same thing as an issue.

        An issue created through `/rest/api/3/issue` on a service project
        lands in the project but has no request type, so it is malformed on
        the customer portal and breaks any automation that reads one. The
        request API is the only correct entry point there.
        """
        values: dict[str, Any] = {
            "summary": mapping.summary(alarm),
            "description": (body if (self.adf_bodies
                                     and self.cfg.get("jsm_description_adf"))
                            else adf.to_text(body)),
        }
        values.update(mapping.custom_fields(alarm, self.cfg))
        created = await self.client.post(f"{SERVICEDESK}/request", json_body={
            "serviceDeskId": str(self.cfg["service_desk_id"]),
            "requestTypeId": str(self.cfg["request_type_id"]),
            "requestFieldValues": values,
        })
        issue_key = created.get("issueKey") or created.get("key")

        # Labels and priority are not `requestFieldValues` - the request API
        # only accepts fields the request type exposes on its form, and these
        # two almost never are. Set them straight after, on the issue the
        # request created. Best effort: a ticket without its labels is worse
        # than one with, and much better than none.
        try:
            patch: dict[str, Any] = {
                "labels": mapping.labels(alarm, self.cfg, print_)}
            name = mapping.priority(alarm, self.cfg)
            if name:
                patch["priority"] = {"name": name}
            await self._update_fields(issue_key, patch)
        except JiraError as exc:
            log.warning("could not label a service desk request",
                        issue=issue_key, error=str(exc))
        return {"key": issue_key, "id": created.get("issueId")}


    # ------------------------------------------------------------ windows

    async def handle_window(self, *, kind: str, window: dict[str, Any],
                            payload: dict[str, Any],
                            attempts: int = 1) -> WindowUpdate | None:
        """A maintenance window's change request.

        Deliberately NOT the alarm path. A window has one change request
        rather than a deduplicated stream of occurrences, so there is no
        fingerprint to search on, no reopen window and no storm to brake - and
        pretending otherwise would have meant an `alarm` shaped like a window
        passing through five branches that do not apply to it.

        What it DOES share is everything that matters operationally: the same
        outbox, the same retry schedule, the same rate limiter and the same
        client.
        """
        issue_key = window.get("jira_issue_key")

        if kind == "window_requested":
            if issue_key:
                # Already opened. A redelivery, or somebody pressing the
                # button twice; either way a second change request for one
                # window is worse than none.
                log.info("window already has a change request",
                         window=window.get("id"), issue=issue_key)
                return None
            return await self._create_change(window, payload)

        if not issue_key:
            # Nothing to comment on. Not an error: a window can be completed
            # having never asked for a change request.
            return None

        if kind == "window_completed":
            await self._comment_raw(
                issue_key,
                change.completion_comment(
                    window, payload.get("report") or {},
                    dcim_url=change.window_url(self.dcim_base, window)))
            return WindowUpdate(issue_key)

        if kind == "window_cancelled":
            await self._comment_raw(issue_key, adf.doc(adf.paragraph(
                adf.text("The window was cancelled in the DCIM.", adf.strong()),
                adf.text(" No work was carried out and no alarms were "
                         "shelved."))))
            return WindowUpdate(issue_key)

        return None

    async def _create_change(self, window: dict[str, Any],
                             payload: dict[str, Any]) -> WindowUpdate:
        targets = payload.get("targets") or []
        preview = payload.get("preview") or {}
        dcim_url = change.window_url(self.dcim_base, window)
        body = change.description(window, preview, targets, dcim_url=dcim_url)

        if self.is_servicedesk:
            values: dict[str, Any] = {
                "summary": change.summary(window),
                "description": (body if (self.adf_bodies
                                         and self.cfg.get("jsm_description_adf"))
                                else adf.to_text(body)),
            }
            created = await self.client.post(
                f"{SERVICEDESK}/request", json_body={
                    "serviceDeskId": str(self.cfg["service_desk_id"]),
                    "requestTypeId": str(
                        self.cfg.get("change_request_type_id")
                        or self.cfg["request_type_id"]),
                    "requestFieldValues": values,
                })
            issue_key = created.get("issueKey") or created.get("key")
        else:
            fields: dict[str, Any] = {
                "project": {"key": self.cfg["project_key"]},
                "issuetype": {"name": self.cfg.get("change_issue_type")
                              or "Task"},
                "summary": change.summary(window),
                "description": body,
                "labels": [self.cfg["labels"].get("prefix") or "dcim",
                           "dcim-change"],
            }
            created = await self.client.post(f"{self.api}/issue", json_body={
                "fields": fields,
                "properties": [{"key": "dcim.window",
                                "value": {"window_id": window.get("id"),
                                          "starts_at": str(window.get("starts_at")),
                                          "ends_at": str(window.get("ends_at"))}}],
            })
            issue_key = created["key"]

        log.info("change request opened", issue=issue_key,
                 window=window.get("id"), targets=len(targets))

        # Best effort, as on the alarm path: the change request exists and
        # carries the whole case. Failing the row here would retry the CREATE
        # and put a second change request in front of the board.
        if dcim_url:
            try:
                await self.client.post(
                    f"{self.api}/issue/{issue_key}/remotelink", json_body={
                        "globalId": f"system={self.dcim_base}&window={window.get('id')}",
                        "application": {"type": "com.hindora.dcim",
                                        "name": "DCIM Platform"},
                        "relationship": "planned in",
                        "object": {"url": dcim_url,
                                   "title": f"Maintenance window - "
                                            f"{window.get('title')}",
                                   "status": {"resolved": False}},
                    }, issue_key=issue_key)
            except JiraError as exc:
                log.info("could not attach the window back-link",
                         issue=issue_key, error=str(exc))

        return WindowUpdate(issue_key, issue_id=created.get("id"))

    async def _comment_raw(self, issue_key: str,
                           body: dict[str, Any]) -> None:
        await self.client.post(f"{self.api}/issue/{issue_key}/comment",
                               json_body={"body": self.rich(body)},
                               issue_key=issue_key)

    # ------------------------------------------------------------ pieces

    async def _find_by_fingerprint(self, print_: str) -> dict[str, Any] | None:
        """Is there already an issue for this condition?

        Only ever called on a RETRY. On the first attempt the local link table
        is the authority and searching would be one JQL call per alarm against
        an hourly point quota - on exactly the code path that runs hardest
        during a storm.
        """
        project = self.cfg.get("project_key")
        jql = f'labels = "{fp.label(print_)}"'
        if project:
            jql = f'project = "{project}" AND {jql}'
        # `fields` is explicit because the replacement search endpoint no
        # longer defaults it: omitting it returns ids and almost nothing else,
        # which reads as "the issue has no status".
        payload = {"jql": f"{jql} ORDER BY created DESC", "maxResults": 1,
                   "fields": ["key", "status", "resolution"]}
        result = await self.client.post(self.search, json_body=payload)
        issues = (result or {}).get("issues") or []
        return issues[0] if issues else None

    async def _comment(self, issue_key: str, kind: str,
                       alarm: dict[str, Any]) -> None:
        body = mapping.comment_for(
            kind, alarm, dcim_url=mapping.alarm_url(self.dcim_base, alarm))
        payload: dict[str, Any] = {"body": self.rich(body)}
        await self.client.post(f"{self.api}/issue/{issue_key}/comment",
                               json_body=payload, issue_key=issue_key)

    async def _update_fields(self, issue_key: str,
                             fields: dict[str, Any]) -> None:
        await self.client.put(f"{self.api}/issue/{issue_key}",
                              json_body={"fields": fields},
                              issue_key=issue_key)

    async def _set_priority(self, issue_key: str,
                            alarm: dict[str, Any]) -> None:
        name = mapping.priority(alarm, self.cfg)
        if not name:
            return
        fields: dict[str, Any] = {"priority": {"name": name}}
        fields.update(mapping.occurrence_fields(alarm, self.cfg))
        await self._update_fields(issue_key, fields)

    async def _transitions(self, issue_key: str) -> list[dict[str, Any]]:
        """What this issue can do from where it is now.

        Always read, never cached: transition ids are per-workflow AND the
        available set depends on the issue's current status and the calling
        account's permissions. A cached id is a 400 waiting for the first
        customer who edits their workflow.
        """
        result = await self.client.get(f"{self.api}/issue/{issue_key}/transitions",
                                       issue_key=issue_key)
        return (result or {}).get("transitions") or []

    async def _transition_to(self, issue_key: str, name: str) -> bool:
        wanted = name.strip().casefold()
        for transition in await self._transitions(issue_key):
            target = (transition.get("to") or {}).get("name") or ""
            if (transition.get("name") or "").casefold() == wanted \
                    or target.casefold() == wanted:
                await self._do_transition(issue_key, transition["id"])
                return True
        return False

    async def _reopen(self, issue_key: str) -> bool:
        """Move back out of Done, by category rather than by name.

        By category because the name is whatever the customer called it -
        Reopen, Back to triage, In Progress - and there is no list of those
        worth maintaining. Anything that leaves the done category is a reopen.
        """
        for transition in await self._transitions(issue_key):
            category = (((transition.get("to") or {}).get("statusCategory")
                         or {}).get("key") or "").casefold()
            if category and category != DONE:
                await self._do_transition(issue_key, transition["id"])
                return True
        return False

    async def _do_transition(self, issue_key: str, transition_id: str) -> None:
        await self.client.post(f"{self.api}/issue/{issue_key}/transitions",
                               json_body={"transition": {"id": transition_id}},
                               issue_key=issue_key)

    async def _relate(self, issue_key: str, other: str) -> None:
        """Point a fresh issue at the one it follows.

        Best effort and deliberately quiet: "Relates" is the one link type
        Jira ships by default, and a customer who deleted it gets a 400 that
        must not cost them the ticket.
        """
        try:
            await self.client.post(f"{self.api}/issueLink", json_body={
                "type": {"name": "Relates"},
                "inwardIssue": {"key": issue_key},
                "outwardIssue": {"key": other},
            })
        except JiraError as exc:
            log.info("could not link to the previous issue",
                     issue=issue_key, other=other, error=str(exc))

    async def _push_remote_link(self, alarm: dict[str, Any], print_: str, *,
                                resolved: bool,
                                issue_key: str | None = None) -> None:
        """Upsert the link back to this DCIM.

        Keyed by `globalId`, so posting it again updates rather than
        duplicates. Best effort: the ticket is the deliverable and a missing
        back-link is a cosmetic loss, whereas failing the row here would
        retry the whole action.
        """
        key = issue_key
        dcim_url = mapping.alarm_url(self.dcim_base, alarm)
        if not key or not dcim_url:
            return
        payload = mapping.remote_link(alarm, print_, base_url=self.dcim_base or "",
                                      dcim_url=dcim_url, resolved=resolved)
        try:
            await self.client.post(f"{self.api}/issue/{key}/remotelink",
                                   json_body=payload, issue_key=key)
        except JiraError as exc:
            log.info("could not attach the back-link", issue=key,
                     error=str(exc))
