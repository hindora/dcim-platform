"""The inbound callback. Unauthenticated by JWT, authenticated by HMAC.

Its own router, outside `integrations.py`, because it is the one endpoint in
this application that a stranger can reach. Keeping it in a file of its own
means the auth dependency that every other route carries cannot be added here
by a careless edit, and cannot be REMOVED from the others by one either.

THREE RULES, and the first two are what the handler is shaped around:

1. **Verify the exact bytes.** The signature covers the body Jira sent. Read
   `await request.body()` and HMAC that; anything that parses and re-
   serialises first breaks the signature, and breaks it intermittently -
   most payloads round-trip identically and the ones with a float or a
   non-ASCII device name do not.

2. **Answer fast.** Jira gives a receiver about 30 seconds and retries up to
   five times on anything that is not a 2xx, with a randomised 5-15 minute
   backoff and at most 20 concurrent deliveries per tenant. So: verify, write
   one row, return 204. No correlation, no alarm mutation, no outbound call.

3. **Say as little as possible when refusing.** A bad token and a bad
   signature both answer 401 with the same body. Distinguishing them tells an
   attacker which half they have.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import get_session
from app.integrations.jira import webhook
from app.repositories import integrations as repo
from app.services import integrations as service

router = APIRouter(tags=["integrations"])
log = get_logger("integrations.webhook")

#: Jira's own limit is smaller than this; anything larger is not a webhook.
#: Read before parsing so a hostile body cannot be decoded at all.
MAX_BODY = 1_000_000


@router.post("/integrations/jira/webhook/{token}", include_in_schema=False,
             status_code=status.HTTP_204_NO_CONTENT,
             summary="Inbound Jira webhook")
async def jira_webhook(
    token: str,
    request: Request,
    signature: str | None = Header(None, alias=webhook.SIGNATURE_HEADER),
    session: AsyncSession = Depends(get_session),
) -> Response:
    # Raw bytes FIRST, and only once. FastAPI caches the body, so the parse
    # below reads the same object - but the HMAC is computed over what arrived
    # and nothing downstream can change that.
    body = await request.body()
    if len(body) > MAX_BODY:
        return _refuse(status.HTTP_413_CONTENT_TOO_LARGE)

    integration = await repo.integration_by_webhook_token(session, token)
    if integration is None or not integration["enabled"]:
        # Deliberately the same answer as a bad signature. A 404 here would
        # turn the token into an oracle for which tokens exist.
        log.warning("webhook rejected", reason="unknown or disabled token")
        return _refuse(status.HTTP_401_UNAUTHORIZED)

    secret = await service.webhook_secret(integration)
    if secret is None or not webhook.verify(body, signature, secret):
        log.warning("webhook rejected", reason="signature",
                    integration=integration["name"])
        return _refuse(status.HTTP_401_UNAUTHORIZED)

    try:
        payload: Any = json.loads(body)
    except ValueError:
        # Signed by us and still not JSON. Worth a line, because it means
        # something in front of Jira is rewriting bodies.
        log.warning("webhook body was signed but unparseable",
                    integration=integration["name"])
        return _refuse(status.HTTP_400_BAD_REQUEST)
    if not isinstance(payload, dict):
        return _refuse(status.HTTP_400_BAD_REQUEST)

    fresh = await repo.enqueue_inbound(
        session, integration_id=integration["id"],
        event=str(payload.get("webhookEvent") or "unknown"),
        issue_key=((payload.get("issue") or {}).get("key")),
        payload=payload, dedup_sha=webhook.dedup_sha(body))
    await session.commit()

    if not fresh:
        # A redelivery of a body we already hold. Still a 204: telling Jira
        # anything else invites it to keep trying.
        log.debug("webhook redelivery ignored", integration=integration["name"])

    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _refuse(code: int) -> Response:
    """One body for every refusal, so nothing can be inferred from the shape."""
    return Response(status_code=code, content=None)
