"""Reading what Jira sends back, and proving it was Jira that sent it.

**The signature is real, and the common belief that it is not is out of date.**
Jira Cloud webhooks take a secret at registration and sign the body with
HMAC-SHA256, sent as `X-Hub-Signature: sha256=<hex>` per the WebSub
convention; Jira Data Center has had the same since 8.x with the same header
and the same default algorithm. Every "Jira webhooks are unsigned, put a token
in the URL" answer predates the Cloud admin UI gaining the secret field.

So the token in the URL is kept, and it is NOT the defence. It is there so an
unsigned probe never reaches the verification code, and so that a leaked URL
by itself still cannot post a forged transition.

**The signature covers the exact bytes.** Any middleware that parses and
re-serialises JSON before verification breaks it - and worse, breaks it
intermittently, because most payloads round-trip identically and the ones with
a float or a non-ASCII device name do not. The handler reads the raw body
once, verifies that, and parses afterwards.

**What the payload is read FOR.** `changelog.items[]` carries `field`,
`fromString` and `toString`, which is how "moved to Done" is detected without
a second call back to Jira to re-read the issue. Branching on the status
CATEGORY rather than the status NAME is deliberate: names are per-workflow and
a customer who renamed Done to Resolved would break every comparison.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

SIGNATURE_HEADER = "X-Hub-Signature"
ALGORITHM = "sha256"

#: Categories Jira reports for a status. Only `done` is branched on.
DONE = "done"

#: Resolutions that mean a human DECIDED, rather than fixed. A ticket resolved
#: this way must not be reopened by the next occurrence, and must not
#: acknowledge the alarm either - nobody said the fault was handled.
DEFAULT_DECLINED = ("won't fix", "wont fix", "duplicate", "declined",
                    "cannot reproduce", "won't do")


def dedup_sha(body: bytes) -> str:
    """The redelivery key: a digest of exactly what arrived.

    Jira sends no delivery id of its own. A payload carries a millisecond
    `timestamp`, so identical bytes really do mean the same event twice.
    """
    return hashlib.sha256(body).hexdigest()


def verify(body: bytes, header: str | None, secret: bytes) -> bool:
    """Is this body signed with our secret?

    `compare_digest`, never `==`: a plain comparison leaks the prefix through
    timing, and a signature that can be guessed one byte at a time is not a
    signature.
    """
    if not header or not secret:
        return False
    algorithm, _, provided = header.strip().partition("=")
    if algorithm.lower() != ALGORITHM or not provided:
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided.lower(), expected)


def sign(body: bytes, secret: bytes) -> str:
    """The header Jira would send. Used by the tests and by nothing else."""
    return f"{ALGORITHM}={hmac.new(secret, body, hashlib.sha256).hexdigest()}"


# ----------------------------------------------------------------- reading

@dataclass(slots=True)
class Decision:
    """What one inbound event means for the DCIM.

    `action` is one of:

    * ``acknowledge`` - a human resolved the ticket and the fault is handled.
      NEVER `clear`. Only the poll clears; see `apply` in the inbox module.
    * ``declined``    - resolved as Won't Fix or similar. The LINK closes so a
      recurrence opens a fresh ticket, and the alarm is left alone, because
      nobody said the condition was dealt with.
    * ``moved``       - the status changed to something that is not Done.
      Whether that is a REOPEN or just somebody picking the ticket up depends
      on where it was before, which this module cannot see and the stored
      link can; `apply` decides.
    * ``comment``     - a human wrote something. History only.
    * ``orphaned``    - the issue was deleted out from under us.
    * ``ignore``      - anything else, which is most of what arrives.
    """

    action: str
    issue_key: str | None = None
    status: str | None = None
    status_category: str | None = None
    resolution: str | None = None
    note: str | None = None
    actor: str = "jira"


def interpret(payload: dict[str, Any], *,
              declined: tuple[str, ...] = DEFAULT_DECLINED) -> Decision:
    """Turn one webhook payload into one decision.

    Reads the changelog rather than the issue's current fields wherever it
    can. The fields describe where the issue IS; the changelog describes what
    just happened, and two events arriving out of order are told apart only by
    the second.
    """
    event = payload.get("webhookEvent") or ""
    issue = payload.get("issue") or {}
    key = issue.get("key")
    actor = _actor(payload)

    if event == "jira:issue_deleted":
        # Not ignorable. An alarm now has a link to an issue that does not
        # exist, which means a condition somebody was told about has quietly
        # stopped being tracked and nothing else would ever say so.
        return Decision("orphaned", issue_key=key, actor=actor)

    if event.startswith("comment_"):
        body = _comment_text(payload)
        if not body:
            return Decision("ignore", issue_key=key, actor=actor)
        return Decision("comment", issue_key=key, note=body, actor=actor)

    if event != "jira:issue_updated":
        return Decision("ignore", issue_key=key, actor=actor)

    moved = _status_change(payload)
    if moved is None:
        # Most updates are not status changes: a description edit, a label
        # added, a field touched by automation. Acting on those would
        # re-acknowledge an alarm every time somebody tidies a ticket.
        return Decision("ignore", issue_key=key, actor=actor)

    fields = issue.get("fields") or {}
    category = _category(fields)
    status = ((fields.get("status") or {}).get("name")) or moved[1]
    resolution = ((fields.get("resolution") or {}) or {}).get("name")

    if category != DONE:
        # Reopen or progress? That depends on where the issue WAS, and the
        # changelog carries status names rather than categories - so the
        # answer is not in this payload. It is in the link, which records the
        # category we last saw, and `apply` is where the two meet.
        return Decision("moved", issue_key=key, status=status,
                        status_category=category, actor=actor)

    if resolution and resolution.strip().casefold() in declined:
        return Decision("declined", issue_key=key, status=status,
                        status_category=category, resolution=resolution,
                        actor=actor)

    return Decision("acknowledge", issue_key=key, status=status,
                    status_category=category, resolution=resolution,
                    actor=actor)


def _status_change(payload: dict[str, Any]) -> tuple[str, str] | None:
    """(from, to) for the status item in the changelog, or None.

    The changelog is read rather than the issue's current fields because the
    fields describe where the issue IS and the changelog describes what just
    happened - and two events arriving out of order are told apart only by the
    second.
    """
    for item in (payload.get("changelog") or {}).get("items") or []:
        if (item.get("field") or item.get("fieldId") or "").casefold() != "status":
            continue
        return (item.get("fromString") or "", item.get("toString") or "")
    return None


def _category(fields: dict[str, Any]) -> str | None:
    status = fields.get("status") or {}
    category = status.get("statusCategory") or {}
    key = category.get("key")
    return key.casefold() if isinstance(key, str) else None


def _actor(payload: dict[str, Any]) -> str:
    """Who did it, as a stable string for the audit log.

    An account id rather than a display name where both exist: display names
    change and are not unique, and an audit row has to survive somebody being
    renamed.
    """
    user = payload.get("user") or {}
    account = user.get("accountId") or user.get("name")
    display = user.get("displayName")
    if account and display:
        return f"jira:{display} ({account})"
    return f"jira:{account or display or 'unknown'}"


def _comment_text(payload: dict[str, Any]) -> str | None:
    comment = payload.get("comment") or {}
    body = comment.get("body")
    if isinstance(body, str):
        return body.strip()[:2000] or None
    if isinstance(body, dict):
        # ADF. Flattened rather than dropped: a comment is the one place an
        # engineer writes what they actually found.
        from app.integrations import adf
        return adf.to_text(body)[:2000] or None
    return None
