"""Alarms as JSM Operations alerts.

**THE WHOLE POINT: `alias` IS THE DEDUP KEY, AND IT IS SERVER-SIDE.** Everything
`IssueTarget` builds by hand - the fingerprint label, the link table, the
recovery search, the reopen window - exists because a Jira issue has no notion
of "the same problem again". An Operations alert does. A create whose alias
matches an OPEN alert de-duplicates and bumps its count; the API does the work
this platform otherwise does itself.

So this target is much smaller than the issue one, and the small parts are the
interesting ones:

* **No link table.** The alias IS the key, and it is the fingerprint. Nothing
  needs remembering between runs.
* **No recovery search.** A create that we are unsure landed can simply be
  sent again: the alias makes it idempotent by construction. This removes the
  single ugliest branch in `IssueTarget`.
* **A clear CLOSES the alert**, where a clear on an issue only comments by
  default. Not an inconsistency - an issue tracks WORK, which outlives the
  fault, while an alert tracks the CONDITION, and an alert left open after the
  condition cleared keeps paging somebody about a fault that is over.

THREE THINGS THAT BITE, all from Opsgenie's heritage:

1. **Priority must be P1-P5.** Anything else is not rejected - it SILENTLY
   BECOMES P3. Sending "CRITICAL" would page the estate's worst faults at the
   same urgency as its mildest, and nothing anywhere would say so.
2. **Ack and close against an alias that matches no OPEN alert are DROPPED.**
   Not an error, not a 404 - dropped. So a clear is resolved through the alias
   lookup first, and a miss is treated as "already closed" rather than as a
   failure to retry.
3. **A CLOSED alias creates a NEW alert.** That is the behaviour wanted - a
   condition that recurs after being resolved is a new page - and it is why
   there is no reopen window here.

MIGRATION NOTE. Opsgenie stopped being sold in June 2025 and its data must
move to JSM Operations by 5 April 2027. This targets the JSM Operations
endpoints, not the Opsgenie ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.integrations import adf
from app.integrations.jira import mapping
from app.integrations.jira.client import JiraClient, JiraError

log = get_logger("integrations.ops")

#: The only priorities the API understands. Anything else silently becomes P3.
PRIORITIES = ("P1", "P2", "P3", "P4", "P5")

#: What this platform calls itself in the alert's `source`, so an on-call
#: engineer can tell a DCIM page from a Prometheus one at a glance.
SOURCE = "DCIM Platform"


@dataclass(slots=True)
class AlertUpdate:
    """What happened, for the log. There is nothing to persist.

    Deliberately not a `LinkUpdate`: the alias is the key and the API holds
    it, so this target has no state of its own to write back.
    """

    alias: str
    action: str
    alert_id: str | None = None


class OpsAlertTarget:
    """One JSM Operations instance.

    Shares the outbox, the policy, the fingerprint, the rate limiter and the
    retry schedule with the issue target - everything operational - and shares
    none of the dedup machinery, because it does not need any.
    """

    def __init__(self, client: JiraClient, cfg: dict[str, Any], *,
                 cloud_id: str, dcim_base: str | None = None) -> None:
        self.client = client
        self.cfg = cfg
        self.cloud_id = cloud_id
        self.dcim_base = dcim_base

    @property
    def base(self) -> str:
        return f"https://api.atlassian.com/jsm/ops/api/{self.cloud_id}/v1"

    # ------------------------------------------------------------- entry

    async def handle(self, *, kind: str, alarm: dict[str, Any], print_: str,
                     link: dict[str, Any] | None = None, attempts: int = 1,
                     now: Any = None) -> AlertUpdate | None:
        """Apply one outbox row.

        Signature-compatible with `IssueTarget.handle` so the dispatcher does
        not branch on which target it holds. `link` and `now` are accepted and
        ignored: this target keeps no link and has no reopen window.
        """
        del link, now

        if kind == "alarm_cleared":
            return await self._close(alarm, print_)
        if kind == "alarm_escalated":
            # A create against an OPEN alias de-duplicates server-side and
            # bumps the count, so the escalation is sent as a create with the
            # new priority rather than as a separate update call. One request,
            # and the alert's own history records the change.
            return await self._create(alarm, print_, note="Severity escalated")
        return await self._create(alarm, print_)

    # ------------------------------------------------------------ actions

    async def _create(self, alarm: dict[str, Any], print_: str,
                      note: str | None = None) -> AlertUpdate:
        dcim_url = mapping.alarm_url(self.dcim_base, alarm)
        body: dict[str, Any] = {
            "message": mapping.summary(alarm)[:130],
            # THE dedup key, and the reason this whole target is short.
            "alias": print_,
            "description": adf.to_text(
                mapping.description(alarm, dcim_url=dcim_url))[:15_000],
            "source": SOURCE,
            # What is broken, as one string. Opsgenie's `entity` is what the
            # on-call UI groups and searches on, and a device name is the
            # thing an engineer actually types.
            "entity": str(alarm.get("device_name") or "platform"),
            "tags": mapping.labels(alarm, self.cfg, print_),
        }
        priority = self.priority(alarm)
        if priority:
            body["priority"] = priority
        if note:
            body["note"] = note
        if dcim_url:
            # Rendered as a button in the on-call app, which is the only
            # interface somebody has at 03:00 on a phone.
            body["actions"] = ["Open in DCIM"]
            body["details"] = {"dcim_url": dcim_url,
                               "fingerprint": print_,
                               "device": str(alarm.get("device_name") or ""),
                               "location": mapping.location(alarm)}

        responders = self.cfg.get("ops_responders") or []
        if responders:
            body["responders"] = [{"type": "team", "name": name}
                                  for name in responders]

        await self.client.post(f"{self.base}/alerts", json_body=body)
        log.info("ops alert sent", alias=print_, priority=priority,
                 device=alarm.get("device_name"))
        return AlertUpdate(print_, "created")

    async def _close(self, alarm: dict[str, Any], print_: str
                     ) -> AlertUpdate | None:
        """Close the alert this condition opened.

        Resolved through the alias first, because a close against an alias
        that matches no OPEN alert is DROPPED rather than refused - so acting
        blind would report success for a page that is still ringing.
        """
        alert_id = await self._find(print_)
        if alert_id is None:
            # Already closed, or never created because the policy did not
            # match when it was raised. Both are normal and neither is worth
            # a retry.
            log.debug("no open ops alert for this condition", alias=print_)
            return None
        await self.client.post(
            f"{self.base}/alerts/{alert_id}/close",
            json_body={"source": SOURCE,
                       "note": "The condition cleared on the plane."})
        log.info("ops alert closed", alias=print_, alert=alert_id)
        return AlertUpdate(print_, "closed", alert_id=alert_id)

    async def acknowledge(self, print_: str, note: str) -> AlertUpdate | None:
        """Used by the manual path, not by the alarm lifecycle.

        An alarm acknowledged in the DCIM should stop paging, and this is how.
        """
        alert_id = await self._find(print_)
        if alert_id is None:
            return None
        await self.client.post(f"{self.base}/alerts/{alert_id}/acknowledge",
                               json_body={"source": SOURCE, "note": note})
        return AlertUpdate(print_, "acknowledged", alert_id=alert_id)

    async def _find(self, alias: str) -> str | None:
        """The OPEN alert for one alias, or None.

        A miss is not an error here. A closed alias legitimately matches
        nothing, and a condition whose alert was never created - because the
        policy did not match at raise time - is the ordinary case.
        """
        try:
            found = await self.client.get(f"{self.base}/alerts/alias",
                                          params={"alias": alias})
        except JiraError as exc:
            if exc.status == 404:
                return None
            raise
        data = (found or {}).get("data") or found or {}
        alert_id = data.get("id") if isinstance(data, dict) else None
        return str(alert_id) if alert_id else None

    # ------------------------------------------------------------ mapping

    def priority(self, alarm: dict[str, Any]) -> str | None:
        """P1-P5, or nothing at all.

        Never a guess. An unrecognised priority is not rejected by the API -
        it SILENTLY BECOMES P3 - so sending a severity this map does not cover
        would page the estate's worst faults at the same urgency as its
        mildest, and nothing anywhere would say so. Omitting the field lets
        the team's own default apply, which is at least a decision somebody
        made.
        """
        value = self.cfg["ops_priority_map"].get(
            str(alarm.get("severity") or ""))
        return value if value in PRIORITIES else None
